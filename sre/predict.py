"""Train and serve per-machine failure risk from Borg `machine_events`.

Pipeline:
  1. Pull the full event history from AsterixDB.
  2. Fit a gradient-boosted classifier on a point-in-time snapshot: features
     from events up to a training cutoff, label = "machine emitted REMOVE
     within the trailing horizon of the trace".
  3. Report held-out ROC-AUC so the model's discriminative power is visible.
  4. Score every machine as of the end of the trace and persist the ranked
     risk back into `borg.machine_risk` for the agent to act on.

The label is a proxy: the trace does not separate an unplanned failure from a
planned drain, so REMOVE stands in for "left the fleet". The recommendation
layer treats a high score as "investigate / drain candidate", not "will fail".
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import numpy as np
from dotenv import load_dotenv
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

from sre.asterix_io import (
    RiskRow,
    fetch_events,
    group_by_machine,
    provision_risk_dataset,
    trace_span,
    upsert_risk,
)
from sre.features import (
    MachineEvent,
    MachineFeatures,
    build_features,
    label_removed_within,
)

load_dotenv()

log = logging.getLogger("borgpilot.sre.predict")

# Fractions of the total trace span. Defaults are chosen so the training label
# window and the trailing feature window are both a meaningful slice of a
# ~31-day trace without starving either the history or the horizon.
HORIZON_FRAC = float(os.environ.get("BORGPILOT_HORIZON_FRAC", "0.15"))
WINDOW_FRAC = float(os.environ.get("BORGPILOT_WINDOW_FRAC", "0.10"))
TEST_FRAC = float(os.environ.get("BORGPILOT_TEST_FRAC", "0.20"))
RANDOM_STATE = int(os.environ.get("BORGPILOT_SEED", "0"))


def build_training_matrix(
    by_machine: dict[str, list[MachineEvent]],
    *,
    cutoff_us: int,
    window_us: int,
    horizon_us: int,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Assemble (X, y, machine_ids) at a single training cutoff."""
    feature_rows: list[list[float]] = []
    labels: list[int] = []
    ids: list[str] = []
    for machine_id, events in by_machine.items():
        feats = build_features(events, cutoff_us=cutoff_us, window_us=window_us)
        y = label_removed_within(events, cutoff_us=cutoff_us, horizon_us=horizon_us)
        feature_rows.append(feats.to_vector())
        labels.append(y)
        ids.append(machine_id)
    return np.asarray(feature_rows, dtype=float), np.asarray(labels, dtype=int), ids


def train_model(
    x: np.ndarray, y: np.ndarray
) -> tuple[GradientBoostingClassifier | None, float | None]:
    """Fit the classifier and return it with held-out ROC-AUC.

    Degenerate single-class targets can't train or score a classifier; in that
    case return (None, None) and let the caller fall back to the base rate.
    """
    if len(np.unique(y)) < 2:
        log.warning("only one label class present (positive rate=%.4f); skipping model", y.mean())
        return None, None

    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=TEST_FRAC, random_state=RANDOM_STATE, stratify=y
    )
    model = GradientBoostingClassifier(random_state=RANDOM_STATE)
    model.fit(x_train, y_train)

    auc: float | None = None
    if len(np.unique(y_test)) == 2:
        proba = model.predict_proba(x_test)[:, 1]
        auc = float(roc_auc_score(y_test, proba))
        log.info("held-out ROC-AUC=%.4f (n_test=%d)", auc, len(y_test))
    return model, auc


def score_fleet(
    by_machine: dict[str, list[MachineEvent]],
    *,
    cutoff_us: int,
    window_us: int,
    model: GradientBoostingClassifier | None,
    base_rate: float,
) -> list[RiskRow]:
    """Score every machine as of `cutoff_us`, ranked by risk descending."""
    ids: list[str] = []
    matrix: list[list[float]] = []
    feature_index: dict[str, MachineFeatures] = {}
    for machine_id, events in by_machine.items():
        feats = build_features(events, cutoff_us=cutoff_us, window_us=window_us)
        feature_index[machine_id] = feats
        matrix.append(feats.to_vector())
        ids.append(machine_id)

    if model is None:
        scores = np.full(len(ids), base_rate, dtype=float)
    else:
        scores = model.predict_proba(np.asarray(matrix, dtype=float))[:, 1]

    rows = [
        RiskRow(
            machine_id=machine_id,
            cutoff_us=cutoff_us,
            risk_score=float(score),
            remove_count=feature_index[machine_id].remove_count,
            flap_count=feature_index[machine_id].flap_count,
            time_since_last_us=feature_index[machine_id].time_since_last_us,
            in_fleet=feature_index[machine_id].in_fleet,
        )
        for machine_id, score in zip(ids, scores, strict=True)
    ]
    rows.sort(key=lambda r: r.risk_score, reverse=True)
    return rows


def run(*, top_preview: int = 10, persist: bool = True) -> list[RiskRow]:
    """End-to-end: fetch, train, score, persist. Returns the ranked risk rows."""
    events = fetch_events()
    if not events:
        raise RuntimeError("machine_events is empty — ingest a shard first")

    t_min, t_max = trace_span(events)
    span = t_max - t_min
    if span <= 0:
        raise RuntimeError("degenerate trace span; cannot derive cutoffs")

    horizon_us = int(span * HORIZON_FRAC)
    window_us = int(span * WINDOW_FRAC)
    train_cutoff = t_max - horizon_us  # label window is the fully-observed tail

    by_machine = group_by_machine(events)
    log.info(
        "machines=%d span=%.2f days horizon=%.2f days window=%.2f days",
        len(by_machine),
        span / 8.64e10,
        horizon_us / 8.64e10,
        window_us / 8.64e10,
    )

    x, y, _ = build_training_matrix(
        by_machine, cutoff_us=train_cutoff, window_us=window_us, horizon_us=horizon_us
    )
    base_rate = float(y.mean())
    model, auc = train_model(x, y)

    # Score forward: features as of the end of the trace, predicting the next
    # horizon beyond what we've observed.
    ranked = score_fleet(
        by_machine, cutoff_us=t_max, window_us=window_us, model=model, base_rate=base_rate
    )

    _print_report(ranked, base_rate=base_rate, auc=auc, top_preview=top_preview)

    if persist:
        provision_risk_dataset()
        written = upsert_risk(ranked)
        log.info("persisted %d risk rows to borg.machine_risk", written)

    return ranked


def _print_report(
    ranked: list[RiskRow], *, base_rate: float, auc: float | None, top_preview: int
) -> None:
    auc_str = f"{auc:.4f}" if auc is not None else "n/a (single-class label)"
    print("\nMachine failure-risk model")
    print(f"  training positive rate : {base_rate:.4f}")
    print(f"  held-out ROC-AUC       : {auc_str}")
    print(f"  machines scored        : {len(ranked)}")
    print(f"\nTop {min(top_preview, len(ranked))} drain candidates:")
    print(f"  {'machine_id':>14}  {'risk':>6}  {'removes':>7}  {'flaps':>5}  in_fleet")
    for r in ranked[:top_preview]:
        print(
            f"  {r.machine_id:>14}  {r.risk_score:>6.3f}  "
            f"{r.remove_count:>7}  {r.flap_count:>5}  {r.in_fleet:>8}"
        )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Train and persist per-machine failure risk from Borg machine_events."
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Train and report only; do not write borg.machine_risk.",
    )
    parser.add_argument(
        "--top", type=int, default=10, help="How many top-risk machines to preview."
    )
    args = parser.parse_args()

    try:
        run(top_preview=args.top, persist=not args.no_persist)
    except (RuntimeError, ValueError) as e:
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
