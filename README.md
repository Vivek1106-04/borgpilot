# BorgPilot

> Autonomous SRE agent that performs root-cause analysis on Google Borg 2019
> cluster traces by bridging an LLM to a local Apache AsterixDB cluster
> through the [Model Context Protocol](https://modelcontextprotocol.io).

Hyperscaler observability stacks drown in deeply-nested telemetry. Relational
stores need expensive flattening or slow `JSONB` extraction. BorgPilot
demonstrates that a columnar nested-type database (AsterixDB) plus a
Model Context Protocol gateway gives an LLM enough surface area to act as
an autonomous Level-3 SRE — discovering schemas, writing `UNNEST` queries,
and producing evidence-backed root-cause summaries without hardcoded SQL.

## Stack

```
┌────────────────┐                ┌──────────────────────────┐                ┌──────────────────────┐
│   Claude       │  tool-use      │  asterixdb-mcp-server    │   HTTP /       │  Apache AsterixDB    │
│   (Anthropic)  │ ─────────────▶ │  (sibling repo)          │ ─────────────▶ │  local cluster       │
│                │   protocol     │  19 tools, 11 resources  │   SQL++        │  CC :19002           │
└────────────────┘                └──────────────────────────┘                └──────────────────────┘
        ▲                                     ▲
        │                                     │ stdio (subprocess)
        │                                     │
        └─────────────  agent/loop.py (this repo) ────────────┘
                                  │
                                  │  ingestion/  (this repo)
                                  ▼
                         gs://clusterdata_2019_*  (Google public mirror)
```

The companion MCP gateway lives in
[`asterixdb-mcp-server`](https://github.com/Vivek1106-04/asterixdb-mcp-server).
BorgPilot spawns it as an stdio subprocess; you do not need to run it
separately.

## Project layout

```
borgpilot/
├── agent/
│   ├── loop.py            # MCP-client driven Anthropic tool-use loop
│   └── prompts.py         # BorgPilot system + user prompts
├── ingestion/
│   ├── fetch_borg.py      # gsutil-based pull from gs://clusterdata_2019_*
│   └── load_to_asterix.py # provisions dataverse, types, datasets; bulk LOAD
├── tests/
│   └── test_ingest_dryrun.py
└── pyproject.toml
```

## Why AsterixDB

Borg 2019 records are deeply nested (`resource_request`, `constraint`,
`labels`, repeated event sub-records). Three design choices encode the
hypothesis we want to demonstrate:

1. **Open types** — Borg's schema drifts across cells and tables. Open
   records accept unknown fields without DDL churn.
2. **Columnar storage** (`'storage-format': {'format': 'column'}`) — most
   investigations project a handful of leaves out of wide nested records.
   Columnar pruning is essential at hyperscaler volume.
3. **First-class `UNNEST`** — the agent treats span / event arrays as
   regular tables. No flattening ETL, no `jsonb_path_query` gymnastics.

## Prerequisites

- macOS or Linux
- Python 3.11+
- A local AsterixDB cluster reachable at `http://localhost:19002`
  (see <https://nightlies.apache.org/asterixdb/install.html>)
- `gsutil` on `PATH` (ships with `google-cloud-sdk`); no auth needed for
  anonymous reads of `gs://clusterdata_2019_*`
- The companion
  [`asterixdb-mcp-server`](https://github.com/Vivek1106-04/asterixdb-mcp-server)
  installed and on `PATH` (or referenced via `ASTERIXDB_MCP_COMMAND`)
- An Anthropic API key

## Setup

```bash
git clone https://github.com/Vivek1106-04/borgpilot.git
cd borgpilot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# edit .env: ANTHROPIC_API_KEY, ASTERIXDB_MCP_COMMAND, ASTERIX_URL
```

Confirm the offline test suite is green (no cluster needed):

```bash
pytest tests/ -v
```

## Ingest a Borg subset

The full 2019 release is ~2.4 TiB. Start tiny — one shard of one table on
one cell is enough to drive the agent:

```bash
borgpilot-ingest --table machine_events --shards 1
```

The command:

1. Creates the `borg` dataverse (if missing).
2. Declares an `OPEN` type and a `COLUMNAR` dataset for the table.
3. Pulls one shard from `gs://clusterdata_2019_a/machine_events/` via
   `gsutil` into `./data/borg/machine_events/`.
4. Issues `LOAD DATASET borg.machine_events USING localfs (...)` against
   the local Cluster Controller.

All DDL is idempotent (`IF NOT EXISTS`), so the command is safe to rerun.

## Run an investigation

```bash
borgpilot "On cell A, which 3 machine_ids accumulated the most REMOVE \
           events in the first 15 minutes of the trace?"
```

What happens under the hood:

1. `agent/loop.py` spawns the AsterixDB MCP gateway as a subprocess.
2. It calls `session.list_tools()` and translates them into Anthropic
   tool definitions.
3. Claude lists dataverses, inspects the `machine_events` schema, writes
   SQL++, and iterates until it can answer.
4. Every turn — user message, assistant blocks, tool result — is appended
   to `./agent_traces/{timestamp}-{hex}.jsonl` for replay and grading.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ASTERIX_URL` | `http://localhost:19002` | Cluster Controller endpoint used by ingestion. |
| `ASTERIX_DATAVERSE` | `borg` | Target dataverse. |
| `ASTERIXDB_MCP_COMMAND` | `asterixdb-mcp` | Command (shell-split) to spawn the MCP gateway. |
| `ANTHROPIC_API_KEY` | — | Required for `borgpilot`. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Model name. |
| `BORG_GCS_BUCKET` | `gs://clusterdata_2019_a` | Source bucket for `borgpilot-fetch`. |
| `BORG_LOCAL_CACHE` | `./data/borg` | Where downloaded shards are cached. |
| `BORGPILOT_MAX_TURNS` | `20` | Hard cap on the agent's tool-use loop. |
| `BORGPILOT_LOG_DIR` | `./agent_traces` | Where JSONL investigation traces are written. |

## Roadmap

- [x] AsterixDB-only data plane (no BigQuery in the agent path)
- [x] Open-typed columnar Borg datasets
- [x] MCP-client agent loop with JSONL traces
- [ ] Eval harness with synthetic fault injection + grader
- [ ] Multi-table investigations (joins across `*_events` + `instance_usage`)
- [ ] Index recommendations from real query workload

## License

Apache-2.0.
