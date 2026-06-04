"""Offline tests for the ingestion module.

These exercise pure-function validators (allow-lists, DDL generation) without
spawning `gsutil` or hitting an AsterixDB cluster.  They are safe to run in
CI without credentials.
"""

from __future__ import annotations

import pytest

from ingestion import fetch_borg, load_to_asterix


def test_unknown_table_rejected_by_fetch() -> None:
    with pytest.raises(ValueError, match="unknown Borg table"):
        fetch_borg.fetch("nonsense_table", max_shards=1)


def test_zero_shards_rejected() -> None:
    with pytest.raises(ValueError, match="max_shards must be >= 1"):
        fetch_borg.fetch("machine_events", max_shards=0)


def test_provision_rejects_unknown_table() -> None:
    with pytest.raises(ValueError, match="unknown table"):
        load_to_asterix.provision_table("nonsense_table")


def test_dataset_registry_consistent() -> None:
    """Every TYPES_SQLPP entry must have a matching DATASETS entry, and vice versa."""
    assert set(load_to_asterix.TYPES_SQLPP) == set(load_to_asterix.DATASETS)
    assert set(load_to_asterix.DATASETS) <= fetch_borg.ALLOWED_TABLES


def test_types_use_open_records() -> None:
    """Open types are required so Borg's evolving schema does not break ingest."""
    for table, ddl in load_to_asterix.TYPES_SQLPP.items():
        assert "AS OPEN" in ddl, f"{table} type must be OPEN"
        assert "IF NOT EXISTS" in ddl, f"{table} DDL must be idempotent"


def test_datasets_are_columnar() -> None:
    """COLUMNAR storage is the entire point of using AsterixDB here."""
    for table, (pk_cols, type_name) in load_to_asterix.DATASETS.items():
        assert pk_cols, f"{table} must declare a primary key"
        assert type_name.endswith("Type"), f"{table} type name convention"
