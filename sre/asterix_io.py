"""AsterixDB read/write for the failure-risk pipeline.

Reads raw `machine_events` out of the cluster and writes scored risk rows
back into a dedicated `borg.machine_risk` dataset. The write path is a
deliberate design choice: the cluster stays the single source of truth, and
the agent reads predictions through the same MCP tools it uses for the raw
telemetry — no side-channel files, no second store to keep consistent.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from ingestion.load_to_asterix import DATAVERSE, run_sqlpp
from sre.features import MachineEvent

log = logging.getLogger("borgpilot.sre.io")

RISK_DATASET = "machine_risk"
RISK_TYPE = "MachineRiskType"

# Batch size for UPSERT — keeps each statement well under the CC's request
# limits while still amortizing round-trips across thousands of machines.
UPSERT_BATCH = 500


@dataclass(frozen=True)
class RiskRow:
    """A scored machine, ready to persist and to rank in the UI/agent."""

    machine_id: str
    cutoff_us: int
    risk_score: float
    remove_count: int
    flap_count: int
    time_since_last_us: int
    in_fleet: int


def fetch_events() -> list[MachineEvent]:
    """Pull every `machine_events` row and decode the string-encoded INT64s.

    `time` and `type` are reserved words and JSON-string-encoded per the Borg
    2019 convention, so they are backticked and cast at the source.
    """
    statement = (
        "SELECT VALUE {"
        '  "m": me.machine_id,'
        "  \"t\": INT64(me.`time`),"
        "  \"y\": INT64(me.`type`)"
        f"}} FROM {DATAVERSE}.{'machine_events'} me;"
    )
    body = run_sqlpp(statement, timeout=300.0)
    rows = body.get("results", [])
    events: list[MachineEvent] = []
    for r in rows:
        machine_id = r.get("m")
        time_us = r.get("t")
        event_type = r.get("y")
        if machine_id is None or time_us is None or event_type is None:
            continue
        events.append(
            MachineEvent(
                machine_id=str(machine_id),
                time_us=int(time_us),
                event_type=int(event_type),
            )
        )
    log.info("fetched %d machine_events rows", len(events))
    return events


def provision_risk_dataset() -> None:
    """Idempotently create the columnar dataset that holds risk scores."""
    run_sqlpp(f"CREATE DATAVERSE {DATAVERSE} IF NOT EXISTS;")
    run_sqlpp(
        f"CREATE TYPE {DATAVERSE}.{RISK_TYPE} IF NOT EXISTS AS OPEN {{ machine_id: string }};"
    )
    run_sqlpp(
        f"CREATE DATASET {DATAVERSE}.{RISK_DATASET} ({DATAVERSE}.{RISK_TYPE}) "
        f"IF NOT EXISTS PRIMARY KEY machine_id "
        f"WITH {{'storage-format': {{'format': 'column'}}}};"
    )
    log.info("dataset %s.%s ready", DATAVERSE, RISK_DATASET)


def _row_to_object(row: RiskRow) -> dict[str, object]:
    # INT64 fields are stored as JSON strings to match the trace convention and
    # dodge float coercion of 64-bit values.
    return {
        "machine_id": row.machine_id,
        "cutoff_time": str(row.cutoff_us),
        "risk_score": row.risk_score,
        "remove_count": row.remove_count,
        "flap_count": row.flap_count,
        "time_since_last": str(row.time_since_last_us),
        "in_fleet": row.in_fleet,
    }


def upsert_risk(rows: list[RiskRow]) -> int:
    """UPSERT scored rows in batches. Returns the number of rows written."""
    if not rows:
        log.warning("no risk rows to upsert")
        return 0
    written = 0
    for start in range(0, len(rows), UPSERT_BATCH):
        batch = rows[start : start + UPSERT_BATCH]
        payload = json.dumps([_row_to_object(r) for r in batch])
        run_sqlpp(f"UPSERT INTO {DATAVERSE}.{RISK_DATASET} ({payload});", timeout=300.0)
        written += len(batch)
        log.info("upserted %d/%d risk rows", written, len(rows))
    return written


def group_by_machine(events: list[MachineEvent]) -> dict[str, list[MachineEvent]]:
    """Bucket a flat event list by machine_id (order within a bucket is arbitrary;
    feature builders sort internally)."""
    by_machine: dict[str, list[MachineEvent]] = {}
    for e in events:
        by_machine.setdefault(e.machine_id, []).append(e)
    return by_machine


def trace_span(events: list[MachineEvent]) -> tuple[int, int]:
    """(min_time_us, max_time_us) across all events. Raises on empty input."""
    if not events:
        raise ValueError("no events to derive a trace span from")
    times = [e.time_us for e in events]
    return min(times), max(times)


# Allow overriding the query endpoint host purely via env for parity with the
# rest of the codebase; nothing here hardcodes a URL.
def endpoint() -> str:
    return os.environ.get("ASTERIX_URL", "http://localhost:19002")
