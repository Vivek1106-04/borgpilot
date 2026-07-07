"""Unit tests for the pure feature/label logic.

No cluster, no model — just the deterministic transforms. Safe in CI.
"""

from __future__ import annotations

from sre.features import (
    ADD,
    FEATURE_NAMES,
    REMOVE,
    UPDATE,
    MachineEvent,
    MachineFeatures,
    build_features,
    label_removed_within,
)

DAY = 86_400_000_000  # microseconds


def _ev(t: int, y: int, m: str = "m1") -> MachineEvent:
    return MachineEvent(machine_id=m, time_us=t, event_type=y)


def test_feature_vector_matches_declared_names() -> None:
    feats = build_features([_ev(0, ADD)], cutoff_us=DAY, window_us=DAY)
    assert len(feats.to_vector()) == len(FEATURE_NAMES)


def test_empty_history_is_inert_but_silent_for_full_cutoff() -> None:
    feats = build_features([], cutoff_us=5 * DAY, window_us=DAY)
    assert feats.total_events == 0
    assert feats.in_fleet == 0
    assert feats.time_since_last_us == 5 * DAY  # treated as long-silent


def test_add_remove_add_counts_one_flap() -> None:
    events = [_ev(0, ADD), _ev(DAY, REMOVE), _ev(2 * DAY, ADD)]
    feats = build_features(events, cutoff_us=3 * DAY, window_us=10 * DAY)
    assert feats.add_count == 2
    assert feats.remove_count == 1
    assert feats.flap_count == 1
    assert feats.in_fleet == 1  # last event was an ADD


def test_repeated_flapping_accumulates() -> None:
    events = [
        _ev(0, ADD),
        _ev(DAY, REMOVE),
        _ev(2 * DAY, ADD),
        _ev(3 * DAY, REMOVE),
        _ev(4 * DAY, ADD),
    ]
    feats = build_features(events, cutoff_us=5 * DAY, window_us=100 * DAY)
    assert feats.flap_count == 2


def test_cutoff_excludes_future_events() -> None:
    events = [_ev(0, ADD), _ev(10 * DAY, REMOVE)]
    feats = build_features(events, cutoff_us=DAY, window_us=DAY)
    # The REMOVE at day 10 is after the cutoff and must not be seen.
    assert feats.remove_count == 0
    assert feats.in_fleet == 1


def test_trailing_window_bounds_recency_features() -> None:
    events = [_ev(0, REMOVE), _ev(9 * DAY, ADD), _ev(9 * DAY + 1, REMOVE)]
    feats = build_features(events, cutoff_us=10 * DAY, window_us=2 * DAY)
    # Only the two events inside the last 2 days count toward the window.
    assert feats.events_in_window == 2
    assert feats.removes_in_window == 1


def test_mean_interval_is_uniform_for_regular_spacing() -> None:
    events = [_ev(0, ADD), _ev(DAY, UPDATE), _ev(2 * DAY, UPDATE)]
    feats = build_features(events, cutoff_us=2 * DAY, window_us=10 * DAY)
    assert feats.mean_interval_us == float(DAY)


def test_label_positive_when_remove_in_horizon() -> None:
    events = [_ev(0, ADD), _ev(3 * DAY, REMOVE)]
    assert label_removed_within(events, cutoff_us=2 * DAY, horizon_us=2 * DAY) == 1


def test_label_negative_when_remove_outside_horizon() -> None:
    events = [_ev(0, ADD), _ev(10 * DAY, REMOVE)]
    assert label_removed_within(events, cutoff_us=2 * DAY, horizon_us=2 * DAY) == 0


def test_label_ignores_remove_at_or_before_cutoff() -> None:
    events = [_ev(2 * DAY, REMOVE)]
    # A REMOVE exactly at the cutoff is history, not future.
    assert label_removed_within(events, cutoff_us=2 * DAY, horizon_us=2 * DAY) == 0


def test_features_are_frozen() -> None:
    feats = build_features([_ev(0, ADD)], cutoff_us=DAY, window_us=DAY)
    assert isinstance(feats, MachineFeatures)
    try:
        feats.total_events = 99  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("MachineFeatures must be immutable")
