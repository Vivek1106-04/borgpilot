"""Predictive reliability analytics over Google Borg 2019 cluster traces.

This package turns raw `machine_events` history into a per-machine failure
risk score — the probability that a machine leaves the fleet (emits a REMOVE
event) within a bounded horizon. Scores are persisted back into AsterixDB
(`borg.machine_risk`) so the investigating agent reads them through the same
MCP surface as every other dataset, with no out-of-band state.

Modeled after Microsoft Azure's predictive failure-mitigation work (Narya,
OSDI 2020): learn failure precursors from operational history, then act
before the interruption instead of paging after it.
"""
