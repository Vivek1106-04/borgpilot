"""BorgPilot — autonomous SRE agent loop.

Architecture
------------
    Claude (Anthropic SDK)
        │
        │  tool-use protocol
        ▼
    MCP client (this file)
        │
        │  stdio JSON-RPC
        ▼
    asterixdb-mcp-server  (sibling repo, spawned as subprocess)
        │
        │  HTTP / SQL++
        ▼
    AsterixDB Cluster Controller  (local)

The loop:
  1. Spawn the AsterixDB MCP gateway as a subprocess.
  2. Discover its tools via `session.list_tools()`.
  3. Translate them into Anthropic tool definitions.
  4. Drive a multi-turn investigation: Claude proposes tool calls, we
     dispatch them through the MCP session, append results, repeat.
  5. Append every turn to a JSONL trace file for replay / grading.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import shlex
import sys
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from agent.prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("borgpilot")
console = Console()

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TURNS = int(os.environ.get("BORGPILOT_MAX_TURNS", "20"))
LOG_DIR = Path(os.environ.get("BORGPILOT_LOG_DIR", "./agent_traces"))
MCP_COMMAND = os.environ.get("ASTERIXDB_MCP_COMMAND", "asterixdb-mcp")


class TraceWriter:
    """Append every agent turn to a JSONL file for replay and downstream grading."""

    def __init__(self, log_dir: Path, run_id: str) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self._path = log_dir / f"{run_id}.jsonl"
        self._turn = 0

    @property
    def path(self) -> Path:
        return self._path

    def write(self, role: str, content: Any) -> None:
        self._turn += 1
        record = {
            "turn": self._turn,
            "role": role,
            "timestamp": dt.datetime.now(dt.UTC).isoformat(),
            "content": content,
        }
        with self._path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")


def _mcp_to_anthropic_tool(mcp_tool: Any) -> dict[str, Any]:
    """Translate an MCP Tool object into the Anthropic tool-definition schema."""
    return {
        "name": mcp_tool.name,
        "description": mcp_tool.description or f"AsterixDB MCP tool: {mcp_tool.name}",
        "input_schema": mcp_tool.inputSchema or {"type": "object", "properties": {}},
    }


def _block_to_dict(block: Any) -> dict[str, Any]:
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.input),
        }
    return {"type": block.type}


def _extract_text(blocks: list[Any]) -> str:
    return "\n".join(b.text for b in blocks if b.type == "text")


def _mcp_result_to_text(result: Any) -> str:
    """Flatten an MCP tool result into a single string Claude can consume."""
    parts: list[str] = []
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(json.dumps(item, default=str))
    if result.isError:
        return json.dumps({"error": "mcp_tool_error", "content": "\n".join(parts) or "(empty)"})
    return "\n".join(parts) or "(empty)"


async def investigate(question: str, *, max_turns: int = MAX_TURNS) -> str:
    """Run the multi-turn investigation loop.  Returns the final assistant text."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is required")

    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    tracer = TraceWriter(LOG_DIR, run_id)
    client = Anthropic()

    command_parts = shlex.split(MCP_COMMAND)
    if not command_parts:
        raise RuntimeError("ASTERIXDB_MCP_COMMAND is empty")
    server_params = StdioServerParameters(
        command=command_parts[0],
        args=command_parts[1:],
        env=os.environ.copy(),
    )

    console.print(
        Panel.fit(
            f"[bold cyan]Run ID:[/] {run_id}\n"
            f"[bold cyan]Trace:[/] {tracer.path}\n"
            f"[bold cyan]MCP:[/]   {MCP_COMMAND}\n"
            f"[bold cyan]Model:[/] {MODEL}"
        )
    )
    console.print(Panel(question, title="Incident", border_style="yellow"))

    async with AsyncExitStack() as stack:
        read_stream, write_stream = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()

        tool_listing = await session.list_tools()
        tools = [_mcp_to_anthropic_tool(t) for t in tool_listing.tools]
        log.info("MCP exposed %d tools", len(tools))

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(question=question)}
        ]
        tracer.write("user", messages[0]["content"])

        final_text = ""
        for _ in range(max_turns):
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            assistant_blocks = [_block_to_dict(b) for b in resp.content]
            tracer.write("assistant", assistant_blocks)
            messages.append({"role": "assistant", "content": assistant_blocks})

            if resp.stop_reason == "end_turn":
                final_text = _extract_text(resp.content)
                console.print(
                    Panel(Markdown(final_text or "(no text)"), title="Root-cause summary")
                )
                break

            if resp.stop_reason != "tool_use":
                log.warning("unexpected stop_reason: %s", resp.stop_reason)
                final_text = _extract_text(resp.content)
                break

            tool_results: list[dict[str, Any]] = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                console.print(f"[dim]→ tool: {block.name}[/]")
                try:
                    result = await session.call_tool(block.name, dict(block.input))
                    text_payload = _mcp_result_to_text(result)
                except Exception as e:
                    log.exception("tool %s raised", block.name)
                    text_payload = json.dumps(
                        {"error": type(e).__name__, "message": str(e)}
                    )
                tracer.write(
                    "tool_result",
                    {"tool": block.name, "input": dict(block.input), "output": text_payload},
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text_payload,
                    }
                )

            messages.append({"role": "user", "content": tool_results})
        else:
            log.warning("hit max_turns=%d without end_turn", max_turns)

    return final_text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BorgPilot — autonomous SRE agent on AsterixDB"
    )
    parser.add_argument("question", nargs="+", help="Incident question to investigate")
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS)
    args = parser.parse_args()
    question = " ".join(args.question)
    try:
        asyncio.run(investigate(question, max_turns=args.max_turns))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
