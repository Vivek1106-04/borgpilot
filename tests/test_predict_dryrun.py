"""Offline tests for the modeling + write-back layer.

These run the training/scoring math and the persistence serialization on
synthetic events — no AsterixDB cluster required.
"""

from __future__ import annotations

import json

from sre import asterix_io
from sre.asterix_io import RiskRow, _row_to_object
from sre.features import ADD, REMOVE, MachineEvent
from sre.predict import build_training_matrix, score_fleet, train_model

DAY = 86_400_000_000


def _machine(m: str, events: list[tuple[int, int]]) -> tuple[str, list[MachineEvent]]:
    return m, [MachineEvent(machine_id=m, time_us=t, event_type=y) for t, y in events]


def _separable_fleet() -> dict[str, list[MachineEvent]]:
    """Flappers that REMOVE after the cutoff vs. stable machines that don't.

    Gives the model a learnable signal so training and AUC exercise real paths.
    """
    by_machine: dict[str, list[MachineEvent]] = {}
    for i in range(30):
        # Flappers: lots of churn, then a REMOVE in the horizon.
        m, evs = _machine(
            f"flap{i}",
            [(0, ADD), (DAY, REMOVE), (2 * DAY, ADD), (3 * DAY, REMOVE), (9 * DAY, REMOVE)],
        )
        by_machine[m] = evs
    for i in range(30):
        # Stable: single ADD, never leaves.
        m, evs = _machine(f"stable{i}", [(0, ADD)])
        by_machine[m] = evs
    return by_machine


def test_training_matrix_shape_and_labels() -> None:
    by_machine = _separable_fleet()
    x, y, ids = build_training_matrix(
        by_machine, cutoff_us=5 * DAY, window_us=5 * DAY, horizon_us=5 * DAY
    )
    assert x.shape[0] == len(ids) == 60
    assert y.sum() == 30  # only the flappers REMOVE in the horizon


def test_model_trains_and_scores_in_unit_interval() -> None:
    by_machine = _separable_fleet()
    x, y, _ = build_training_matrix(
        by_machine, cutoff_us=5 * DAY, window_us=5 * DAY, horizon_us=5 * DAY
    )
    model, auc = train_model(x, y)
    assert model is not None
    assert auc is not None and 0.0 <= auc <= 1.0

    rows = score_fleet(
        by_machine, cutoff_us=5 * DAY, window_us=5 * DAY, model=model, base_rate=y.mean()
    )
    assert len(rows) == 60
    assert all(0.0 <= r.risk_score <= 1.0 for r in rows)
    # Ranked descending.
    assert rows == sorted(rows, key=lambda r: r.risk_score, reverse=True)
    # Flappers should outrank stable machines.
    assert rows[0].machine_id.startswith("flap")


def test_single_class_label_falls_back_to_base_rate() -> None:
    stable = {m: e for m, e in _separable_fleet().items() if m.startswith("stable")}
    x, y, _ = build_training_matrix(
        stable, cutoff_us=5 * DAY, window_us=5 * DAY, horizon_us=5 * DAY
    )
    model, auc = train_model(x, y)
    assert model is None and auc is None

    rows = score_fleet(stable, cutoff_us=5 * DAY, window_us=5 * DAY, model=None, base_rate=0.0)
    assert all(r.risk_score == 0.0 for r in rows)


def test_risk_row_serializes_int64_as_strings() -> None:
    row = RiskRow(
        machine_id="123",
        cutoff_us=999,
        risk_score=0.5,
        remove_count=3,
        flap_count=2,
        time_since_last_us=42,
        in_fleet=1,
    )
    obj = _row_to_object(row)
    # 64-bit fields ride as strings to avoid float coercion, matching the trace.
    assert obj["cutoff_time"] == "999"
    assert obj["time_since_last"] == "42"
    assert isinstance(obj["risk_score"], float)
    # Round-trips through JSON (the UPSERT payload path).
    assert json.loads(json.dumps(obj))["machine_id"] == "123"


def test_risk_dataset_names_are_stable() -> None:
    assert asterix_io.RISK_DATASET == "machine_risk"
    assert asterix_io.RISK_TYPE.endswith("Type")
