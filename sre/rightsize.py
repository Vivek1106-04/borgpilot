"""Autopilot-style resource rightsizing for Borg instances.

For every instance, compare the resource *request* (from `instance_events`)
against observed *usage* (from `instance_usage`) and recommend a limit that
tracks a safety margin above the peak. Over-provisioned instances become
reclaim candidates; hot instances (usage above request) become upsize
candidates before they throttle or OOM.

Modeled after Google's Autopilot (EuroSys 2020): size the limit to a margin
above a spike-tolerant estimate of observed usage rather than to the static
request. We use the mean of the per-window `maximum_usage` as the sizing
signal — a tractable stand-in for Autopilot's usage-histogram percentile that
ignores rare cross-window bursts — and treat CPU and memory independently,
since Borg limits are per-resource.

Recommendations are written back to `borg.rightsizing_recs` so the agent
consumes them through the same MCP surface as everything else.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

from sre.asterix_io import (
    InstanceRequest,
    InstanceUsage,
    fetch_instance_requests,
    fetch_instance_usage,
    provision_rightsizing_dataset,
    upsert_rightsizing,
)

load_dotenv()

log = logging.getLogger("borgpilot.sre.rightsize")

DOWNSIZE = "downsize"
UPSIZE = "upsize"
OK = "ok"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class RightsizeConfig:
    """Tuning for the sizing policy.

    Memory carries a larger margin than CPU: undersizing memory triggers an
    OOM kill, while undersizing CPU only throttles.
    """

    cpu_margin: float = 1.15
    mem_margin: float = 1.25
    downsize_threshold: float = 0.30  # reclaim only when >= this share is slack

    @classmethod
    def from_env(cls) -> RightsizeConfig:
        return cls(
            cpu_margin=float(os.environ.get("BORGPILOT_CPU_MARGIN", "1.15")),
            mem_margin=float(os.environ.get("BORGPILOT_MEM_MARGIN", "1.25")),
            downsize_threshold=float(os.environ.get("BORGPILOT_DOWNSIZE_THRESHOLD", "0.30")),
        )


@dataclass(frozen=True)
class ResourceVerdict:
    """Sizing verdict for one resource dimension (cpu or memory)."""

    request: float
    peak: float
    recommended: float
    slack_ratio: float  # (request - recommended) / request; negative when hot
    action: str

    @property
    def reclaimable(self) -> float:
        """Request units freed if the recommendation is applied (>= 0)."""
        return max(0.0, self.request - self.recommended)


@dataclass(frozen=True)
class RightsizingRec:
    """Per-instance recommendation across both resource dimensions."""

    collection_id: str
    instance_index: str
    cpu: ResourceVerdict
    mem: ResourceVerdict
    decision: str
    n_windows: int

    def to_object(self) -> dict[str, object]:
        return {
            "collection_id": self.collection_id,
            "instance_index": self.instance_index,
            "decision": self.decision,
            "n_windows": self.n_windows,
            "cpu_request": self.cpu.request,
            "cpu_peak": self.cpu.peak,
            "cpu_recommended": self.cpu.recommended,
            "cpu_slack_ratio": self.cpu.slack_ratio,
            "cpu_action": self.cpu.action,
            "mem_request": self.mem.request,
            "mem_peak": self.mem.peak,
            "mem_recommended": self.mem.recommended,
            "mem_slack_ratio": self.mem.slack_ratio,
            "mem_action": self.mem.action,
            "reclaimable_cpu": self.cpu.reclaimable,
            "reclaimable_mem": self.mem.reclaimable,
        }


def size_resource(
    request: float, peak: float, *, margin: float, downsize_threshold: float
) -> ResourceVerdict:
    """Recommend a limit for one resource dimension.

    recommended = peak * margin. A downsize needs the slack to clear the
    threshold; any usage above request is an upsize regardless of slack.
    """
    if request <= 0.0:
        return ResourceVerdict(request, peak, 0.0, 0.0, UNKNOWN)

    recommended = peak * margin
    slack_ratio = (request - recommended) / request

    if peak > request:
        action = UPSIZE
    elif slack_ratio >= downsize_threshold:
        action = DOWNSIZE
    else:
        action = OK
    return ResourceVerdict(request, peak, recommended, slack_ratio, action)


def overall_decision(cpu: ResourceVerdict, mem: ResourceVerdict) -> str:
    """Collapse the two per-resource verdicts into one instance-level decision.

    Reliability first: if either dimension is hot, the instance must grow.
    Reclaim is claimed only when both dimensions can safely shrink.
    """
    actions = {cpu.action, mem.action}
    if UPSIZE in actions:
        return UPSIZE
    if actions == {UNKNOWN}:
        return UNKNOWN
    if cpu.action == DOWNSIZE and mem.action == DOWNSIZE:
        return DOWNSIZE
    return OK


def build_recommendations(
    requests: list[InstanceRequest],
    usages: list[InstanceUsage],
    *,
    cfg: RightsizeConfig,
) -> list[RightsizingRec]:
    """Join requests to usage and produce a ranked recommendation per instance.

    Ranked by total reclaimable resource (cpu + mem) descending, so the biggest
    waste surfaces first.
    """
    usage_by_key = {(u.collection_id, u.instance_index): u for u in usages}

    recs: list[RightsizingRec] = []
    for req in requests:
        usage = usage_by_key.get((req.collection_id, req.instance_index))
        if usage is None:
            continue  # no observed usage — nothing to size against
        cpu = size_resource(
            req.cpu_request,
            usage.cpu_peak,
            margin=cfg.cpu_margin,
            downsize_threshold=cfg.downsize_threshold,
        )
        mem = size_resource(
            req.mem_request,
            usage.mem_peak,
            margin=cfg.mem_margin,
            downsize_threshold=cfg.downsize_threshold,
        )
        recs.append(
            RightsizingRec(
                collection_id=req.collection_id,
                instance_index=req.instance_index,
                cpu=cpu,
                mem=mem,
                decision=overall_decision(cpu, mem),
                n_windows=usage.n_windows,
            )
        )

    recs.sort(key=lambda r: r.cpu.reclaimable + r.mem.reclaimable, reverse=True)
    return recs


def summarize(recs: list[RightsizingRec]) -> dict[str, float]:
    """Fleet-level rollup: decision counts and total reclaimable capacity."""
    counts = {DOWNSIZE: 0, UPSIZE: 0, OK: 0, UNKNOWN: 0}
    reclaim_cpu = 0.0
    reclaim_mem = 0.0
    for r in recs:
        counts[r.decision] += 1
        reclaim_cpu += r.cpu.reclaimable
        reclaim_mem += r.mem.reclaimable
    return {
        "instances": float(len(recs)),
        "downsize": float(counts[DOWNSIZE]),
        "upsize": float(counts[UPSIZE]),
        "ok": float(counts[OK]),
        "unknown": float(counts[UNKNOWN]),
        "reclaimable_cpu": reclaim_cpu,
        "reclaimable_mem": reclaim_mem,
    }


def run(*, top_preview: int = 10, persist: bool = True) -> list[RightsizingRec]:
    """End-to-end: fetch requests + usage, recommend, persist. Returns recs."""
    cfg = RightsizeConfig.from_env()
    requests = fetch_instance_requests()
    if not requests:
        raise RuntimeError("no instance requests — ingest instance_events first")
    usages = fetch_instance_usage()
    if not usages:
        raise RuntimeError("no instance usage — ingest instance_usage first")

    recs = build_recommendations(requests, usages, cfg=cfg)
    if not recs:
        raise RuntimeError("no instances had both a request and observed usage")

    _print_report(recs, cfg=cfg, top_preview=top_preview)

    if persist:
        provision_rightsizing_dataset()
        written = upsert_rightsizing([r.to_object() for r in recs])
        log.info("persisted %d rightsizing rows to borg.rightsizing_recs", written)

    return recs


def _print_report(recs: list[RightsizingRec], *, cfg: RightsizeConfig, top_preview: int) -> None:
    stats = summarize(recs)
    print("\nInstance rightsizing (Autopilot-style)")
    print(f"  cpu margin={cfg.cpu_margin}  mem margin={cfg.mem_margin}  "
          f"downsize>= {cfg.downsize_threshold:.0%} slack")
    print(f"  instances sized : {int(stats['instances'])}")
    print(
        f"  decisions       : downsize={int(stats['downsize'])} "
        f"upsize={int(stats['upsize'])} ok={int(stats['ok'])} "
        f"unknown={int(stats['unknown'])}"
    )
    print(
        f"  reclaimable     : cpu={stats['reclaimable_cpu']:.3f}  "
        f"mem={stats['reclaimable_mem']:.3f} (normalized Borg units)"
    )
    print(f"\nTop {min(top_preview, len(recs))} rightsizing candidates (by reclaimable):")
    print(f"  {'collection_id':>14} {'idx':>5}  {'decision':>8}  "
          f"{'cpu_req':>7} {'cpu_pk':>7}  {'mem_req':>7} {'mem_pk':>7}")
    for r in recs[:top_preview]:
        print(
            f"  {r.collection_id:>14} {r.instance_index:>5}  {r.decision:>8}  "
            f"{r.cpu.request:>7.3f} {r.cpu.peak:>7.3f}  "
            f"{r.mem.request:>7.3f} {r.mem.peak:>7.3f}"
        )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Recommend and persist per-instance resource rightsizing."
    )
    parser.add_argument("--no-persist", action="store_true", help="Report only; no write-back.")
    parser.add_argument("--top", type=int, default=10, help="Top reclaim rows to preview.")
    args = parser.parse_args()

    try:
        run(top_preview=args.top, persist=not args.no_persist)
    except (RuntimeError, ValueError) as e:
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
