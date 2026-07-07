"""Feature engineering for machine failure risk.

Pure, deterministic transforms from a machine's raw `machine_events` sequence
into a fixed-width feature vector and a point-in-time label. No I/O here so the
logic is trivially unit-testable and free of leakage: every feature is computed
strictly from events at or before a cutoff, and the label strictly from events
after it.

Borg `machine_events` event types:
    1 = ADD     machine enters the fleet / becomes schedulable
    2 = REMOVE  machine leaves the fleet (failure, drain, or decommission)
    3 = UPDATE  machine metadata/capacity changes in place
"""

from __future__ import annotations

from dataclasses import astuple, dataclass

ADD = 1
REMOVE = 2
UPDATE = 3

# Ordered feature names — the single source of truth for column order. The
# model and the write-back schema both derive from this, so they never drift.
FEATURE_NAMES: tuple[str, ...] = (
    "total_events",
    "add_count",
    "remove_count",
    "update_count",
    "flap_count",
    "events_in_window",
    "removes_in_window",
    "time_since_last_us",
    "mean_interval_us",
    "in_fleet",
)


@dataclass(frozen=True)
class MachineEvent:
    """A single lifecycle event for one machine."""

    machine_id: str
    time_us: int
    event_type: int


@dataclass(frozen=True)
class MachineFeatures:
    """Fixed-width feature row for one machine at a cutoff instant.

    Field order MUST match FEATURE_NAMES so `to_vector` and the persisted
    schema stay aligned.
    """

    total_events: int
    add_count: int
    remove_count: int
    update_count: int
    flap_count: int
    events_in_window: int
    removes_in_window: int
    time_since_last_us: int
    mean_interval_us: float
    in_fleet: int  # 1 if the machine's last event <= cutoff was an ADD/UPDATE

    def to_vector(self) -> list[float]:
        return [float(v) for v in astuple(self)]


def _sorted_upto(events: list[MachineEvent], cutoff_us: int) -> list[MachineEvent]:
    """Events at or before the cutoff, ascending by time (stable, immutable)."""
    upto = [e for e in events if e.time_us <= cutoff_us]
    return sorted(upto, key=lambda e: e.time_us)


def build_features(
    events: list[MachineEvent],
    *,
    cutoff_us: int,
    window_us: int,
) -> MachineFeatures:
    """Summarize one machine's history as of `cutoff_us`.

    `window_us` bounds the trailing window used for the recency features
    (event count and REMOVE count in the last `window_us` before the cutoff).
    """
    history = _sorted_upto(events, cutoff_us)
    if not history:
        # A machine with no events by the cutoff is inert: zeros, and a
        # maximal time-since-last so the model treats it as long-silent.
        return MachineFeatures(
            total_events=0,
            add_count=0,
            remove_count=0,
            update_count=0,
            flap_count=0,
            events_in_window=0,
            removes_in_window=0,
            time_since_last_us=cutoff_us,
            mean_interval_us=0.0,
            in_fleet=0,
        )

    add_count = sum(1 for e in history if e.event_type == ADD)
    remove_count = sum(1 for e in history if e.event_type == REMOVE)
    update_count = sum(1 for e in history if e.event_type == UPDATE)

    # Flap = an ADD that re-introduces a machine after it was REMOVE'd. Counts
    # the churn signal that a plain add/remove tally would miss.
    flap_count = 0
    seen_remove = False
    for e in history:
        if e.event_type == REMOVE:
            seen_remove = True
        elif e.event_type == ADD and seen_remove:
            flap_count += 1
            seen_remove = False

    window_start = cutoff_us - window_us
    in_window = [e for e in history if e.time_us > window_start]
    events_in_window = len(in_window)
    removes_in_window = sum(1 for e in in_window if e.event_type == REMOVE)

    last = history[-1]
    time_since_last_us = cutoff_us - last.time_us
    in_fleet = 0 if last.event_type == REMOVE else 1

    if len(history) >= 2:
        span = history[-1].time_us - history[0].time_us
        mean_interval_us = span / (len(history) - 1)
    else:
        mean_interval_us = 0.0

    return MachineFeatures(
        total_events=len(history),
        add_count=add_count,
        remove_count=remove_count,
        update_count=update_count,
        flap_count=flap_count,
        events_in_window=events_in_window,
        removes_in_window=removes_in_window,
        time_since_last_us=time_since_last_us,
        mean_interval_us=mean_interval_us,
        in_fleet=in_fleet,
    )


def label_removed_within(
    events: list[MachineEvent],
    *,
    cutoff_us: int,
    horizon_us: int,
) -> int:
    """1 if the machine emits a REMOVE in `(cutoff_us, cutoff_us + horizon_us]`.

    This is the supervised target: did the machine leave the fleet in the
    horizon immediately following the observation cutoff.
    """
    end = cutoff_us + horizon_us
    return int(
        any(
            e.event_type == REMOVE and cutoff_us < e.time_us <= end
            for e in events
        )
    )
