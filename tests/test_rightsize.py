"""Unit tests for the pure rightsizing policy.

No cluster: exercises the sizing math, the per-resource verdicts, the
instance-level decision, and the join. Safe in CI.
"""

from __future__ import annotations

from sre.asterix_io import InstanceRequest, InstanceUsage
from sre.rightsize import (
    DOWNSIZE,
    OK,
    UNKNOWN,
    UPSIZE,
    RightsizeConfig,
    build_recommendations,
    overall_decision,
    size_resource,
    summarize,
)

CFG = RightsizeConfig(cpu_margin=1.15, mem_margin=1.25, downsize_threshold=0.30)


def test_downsize_when_peak_far_below_request() -> None:
    # request 1.0, peak 0.2 -> recommended 0.23 -> 77% slack, well past 30%.
    v = size_resource(1.0, 0.2, margin=1.15, downsize_threshold=0.30)
    assert v.action == DOWNSIZE
    assert v.recommended == 0.2 * 1.15
    assert v.reclaimable > 0.0


def test_upsize_when_peak_exceeds_request() -> None:
    v = size_resource(0.5, 0.8, margin=1.15, downsize_threshold=0.30)
    assert v.action == UPSIZE
    assert v.reclaimable == 0.0  # nothing to reclaim when hot


def test_ok_when_slack_below_threshold() -> None:
    # request 1.0, peak 0.8 -> recommended 0.92 -> 8% slack < 30%.
    v = size_resource(1.0, 0.8, margin=1.15, downsize_threshold=0.30)
    assert v.action == OK


def test_unknown_when_request_missing() -> None:
    v = size_resource(0.0, 0.5, margin=1.15, downsize_threshold=0.30)
    assert v.action == UNKNOWN
    assert v.reclaimable == 0.0


def test_overall_upsize_wins_over_downsize() -> None:
    hot = size_resource(0.5, 0.8, margin=1.15, downsize_threshold=0.30)  # upsize
    cold = size_resource(1.0, 0.1, margin=1.15, downsize_threshold=0.30)  # downsize
    assert overall_decision(cold, hot) == UPSIZE


def test_overall_downsize_requires_both_dimensions() -> None:
    cold = size_resource(1.0, 0.1, margin=1.15, downsize_threshold=0.30)  # downsize
    tight = size_resource(1.0, 0.8, margin=1.15, downsize_threshold=0.30)  # ok
    # One shrinkable, one merely OK -> hold, don't claim a reclaim.
    assert overall_decision(cold, tight) == OK
    assert overall_decision(cold, cold) == DOWNSIZE


def test_overall_unknown_only_when_both_unknown() -> None:
    unk = size_resource(0.0, 0.5, margin=1.15, downsize_threshold=0.30)
    cold = size_resource(1.0, 0.1, margin=1.15, downsize_threshold=0.30)
    assert overall_decision(unk, unk) == UNKNOWN
    assert overall_decision(unk, cold) == OK  # not unknown, not a clean downsize


def _req(c: str, i: str, cpu: float, mem: float) -> InstanceRequest:
    return InstanceRequest(collection_id=c, instance_index=i, cpu_request=cpu, mem_request=mem)


def _use(c: str, i: str, cpu_pk: float, mem_pk: float, n: int = 10) -> InstanceUsage:
    return InstanceUsage(
        collection_id=c,
        instance_index=i,
        cpu_avg=cpu_pk / 2,
        cpu_peak=cpu_pk,
        mem_avg=mem_pk / 2,
        mem_peak=mem_pk,
        n_windows=n,
    )


def test_build_joins_on_instance_key_and_skips_unmatched() -> None:
    requests = [_req("c1", "0", 1.0, 1.0), _req("c1", "1", 1.0, 1.0)]
    usages = [_use("c1", "0", 0.1, 0.1)]  # only instance 0 has usage
    recs = build_recommendations(requests, usages, cfg=CFG)
    assert len(recs) == 1
    assert recs[0].instance_index == "0"
    assert recs[0].decision == DOWNSIZE


def test_build_ranks_by_total_reclaimable_desc() -> None:
    requests = [_req("c", "big", 4.0, 4.0), _req("c", "small", 1.0, 1.0)]
    usages = [_use("c", "big", 0.1, 0.1), _use("c", "small", 0.1, 0.1)]
    recs = build_recommendations(requests, usages, cfg=CFG)
    assert [r.instance_index for r in recs] == ["big", "small"]


def test_summarize_totals_match_rows() -> None:
    requests = [_req("c", "a", 1.0, 1.0), _req("c", "b", 0.5, 0.5)]
    usages = [_use("c", "a", 0.1, 0.1), _use("c", "b", 0.9, 0.9)]  # b is hot
    recs = build_recommendations(requests, usages, cfg=CFG)
    stats = summarize(recs)
    assert stats["instances"] == 2.0
    assert stats["downsize"] + stats["upsize"] + stats["ok"] + stats["unknown"] == 2.0
    assert stats["upsize"] >= 1.0  # instance b


def test_recommendation_serializes_flat_for_asterix() -> None:
    recs = build_recommendations([_req("c", "0", 1.0, 1.0)], [_use("c", "0", 0.1, 0.2)], cfg=CFG)
    obj = recs[0].to_object()
    for key in ("collection_id", "instance_index", "decision", "cpu_recommended"):
        assert key in obj
    assert "reclaimable_mem" in obj
    assert isinstance(obj["decision"], str)
