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


RIGHTSIZE_DATASET = "rightsizing_recs"
RIGHTSIZE_TYPE = "RightsizingRecType"


@dataclass(frozen=True)
class InstanceRequest:
    """Per-instance resource request, aggregated from instance_events."""

    collection_id: str
    instance_index: str
    cpu_request: float
    mem_request: float


@dataclass(frozen=True)
class InstanceUsage:
    """Per-instance observed usage, aggregated across instance_usage windows.

    `*_peak` is the mean of the per-window `maximum_usage` — a "typical peak"
    that tolerates rare cross-window spikes the way Autopilot's usage-percentile
    does, rather than letting one 5-minute burst over the whole ~31-day trace
    dictate the limit. `*_avg` is the mean of the per-window `average_usage`.
    """

    collection_id: str
    instance_index: str
    cpu_avg: float
    cpu_peak: float
    mem_avg: float
    mem_peak: float
    n_windows: int


def fetch_instance_requests() -> list[InstanceRequest]:
    """Aggregate the resource request per (collection_id, instance_index).

    An instance's request can appear on several event rows; we take the max so
    the sizing baseline is the largest request the scheduler ever honored.
    """
    statement = (
        "SELECT ie.collection_id AS c, ie.instance_index AS i, "
        "MAX(ie.resource_request.cpus) AS cpu, "
        "MAX(ie.resource_request.memory) AS mem "
        f"FROM {DATAVERSE}.instance_events ie "
        "WHERE ie.resource_request IS NOT UNKNOWN "
        "GROUP BY ie.collection_id, ie.instance_index;"
    )
    body = run_sqlpp(statement, timeout=600.0)
    out: list[InstanceRequest] = []
    for r in body.get("results", []):
        if r.get("c") is None or r.get("i") is None:
            continue
        if r.get("cpu") is None or r.get("mem") is None:
            continue
        out.append(
            InstanceRequest(
                collection_id=str(r["c"]),
                instance_index=str(r["i"]),
                cpu_request=float(r["cpu"]),
                mem_request=float(r["mem"]),
            )
        )
    log.info("fetched %d instance requests", len(out))
    return out


def fetch_instance_usage() -> list[InstanceUsage]:
    """Aggregate observed usage per (collection_id, instance_index).

    Peak = mean of the per-window `maximum_usage` (a spike-tolerant "typical
    peak"); avg = mean of the per-window `average_usage`. This collapses
    millions of 5-minute windows to one row per instance before it crosses the
    wire.
    """
    statement = (
        "SELECT iu.collection_id AS c, iu.instance_index AS i, "
        "AVG(iu.average_usage.cpus) AS cpu_avg, "
        "AVG(iu.maximum_usage.cpus) AS cpu_peak, "
        "AVG(iu.average_usage.memory) AS mem_avg, "
        "AVG(iu.maximum_usage.memory) AS mem_peak, "
        "COUNT(iu.collection_id) AS n "
        f"FROM {DATAVERSE}.instance_usage iu "
        "GROUP BY iu.collection_id, iu.instance_index;"
    )
    body = run_sqlpp(statement, timeout=600.0)
    out: list[InstanceUsage] = []
    for r in body.get("results", []):
        if r.get("c") is None or r.get("i") is None:
            continue
        peak_cpu = r.get("cpu_peak")
        peak_mem = r.get("mem_peak")
        if peak_cpu is None or peak_mem is None:
            continue
        out.append(
            InstanceUsage(
                collection_id=str(r["c"]),
                instance_index=str(r["i"]),
                cpu_avg=float(r.get("cpu_avg") or 0.0),
                cpu_peak=float(peak_cpu),
                mem_avg=float(r.get("mem_avg") or 0.0),
                mem_peak=float(peak_mem),
                n_windows=int(r.get("n") or 0),
            )
        )
    log.info("fetched %d instance usage aggregates", len(out))
    return out


def provision_rightsizing_dataset() -> None:
    """Idempotently create the columnar dataset that holds rightsizing recs."""
    run_sqlpp(f"CREATE DATAVERSE {DATAVERSE} IF NOT EXISTS;")
    run_sqlpp(
        f"CREATE TYPE {DATAVERSE}.{RIGHTSIZE_TYPE} IF NOT EXISTS AS OPEN "
        "{ collection_id: string, instance_index: string };"
    )
    run_sqlpp(
        f"CREATE DATASET {DATAVERSE}.{RIGHTSIZE_DATASET} ({DATAVERSE}.{RIGHTSIZE_TYPE}) "
        f"IF NOT EXISTS PRIMARY KEY collection_id, instance_index "
        f"WITH {{'storage-format': {{'format': 'column'}}}};"
    )
    log.info("dataset %s.%s ready", DATAVERSE, RIGHTSIZE_DATASET)


def upsert_rightsizing(rows: list[dict[str, object]]) -> int:
    """UPSERT rightsizing recommendation rows in batches."""
    if not rows:
        log.warning("no rightsizing rows to upsert")
        return 0
    written = 0
    for start in range(0, len(rows), UPSERT_BATCH):
        batch = rows[start : start + UPSERT_BATCH]
        payload = json.dumps(batch)
        run_sqlpp(f"UPSERT INTO {DATAVERSE}.{RIGHTSIZE_DATASET} ({payload});", timeout=300.0)
        written += len(batch)
        log.info("upserted %d/%d rightsizing rows", written, len(rows))
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
