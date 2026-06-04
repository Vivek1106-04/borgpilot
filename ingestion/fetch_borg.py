"""Fetch a bounded subset of Google Borg 2019 traces from the public GCS mirror.

We deliberately avoid the BigQuery path: the goal is to keep the data plane
local to the AsterixDB cluster. `gsutil` is used as a thin subprocess so we
do not pull in google-cloud-storage just for anonymous reads.

The Borg 2019 release lives at:
    gs://clusterdata_2019_a/   (cell a; cells b..h also published)

Tables of interest:
    collection_events, instance_events, instance_usage, machine_events,
    machine_attributes

Each file is a gzipped JSON-lines shard.  Records use INT64 microseconds
since the Unix epoch for `time`.

Usage:
    borgpilot-fetch --table machine_events --shards 1
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("borgpilot.fetch")

DEFAULT_BUCKET = os.environ.get("BORG_GCS_BUCKET", "gs://clusterdata_2019_a")
DEFAULT_CACHE = Path(os.environ.get("BORG_LOCAL_CACHE", "./data/borg"))
DEFAULT_CELL = os.environ.get("BORG_CELL", "a")

ALLOWED_TABLES = {
    "collection_events",
    "instance_events",
    "instance_usage",
    "machine_events",
    "machine_attributes",
}


def _gsutil_available() -> bool:
    return shutil.which("gsutil") is not None


def list_shards(bucket: str, table: str) -> list[str]:
    """Return the GCS URIs that make up a Borg table, sorted lexically.

    The 2019 release is flat: every shard lives at the bucket root as
    `<table>-NNNNNNNNNNNN.json.gz` (and some tables also as `.parquet.gz`).
    We prefer JSON since AsterixDB's localfs adapter loads it natively.
    """
    glob = f"{bucket.rstrip('/')}/{table}-*.json.gz"
    proc = subprocess.run(
        ["gsutil", "ls", glob],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gsutil ls {glob!r} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return sorted(line.strip() for line in proc.stdout.splitlines() if line.strip())


def fetch(
    table: str,
    *,
    bucket: str = DEFAULT_BUCKET,
    cache_dir: Path = DEFAULT_CACHE,
    max_shards: int = 1,
) -> list[Path]:
    """Download up to `max_shards` shards of `table` into the local cache.

    Returns the list of downloaded local paths.  Idempotent: existing files
    are skipped.
    """
    if table not in ALLOWED_TABLES:
        raise ValueError(f"unknown Borg table: {table!r}; allowed: {sorted(ALLOWED_TABLES)}")
    if not _gsutil_available():
        raise RuntimeError(
            "gsutil not found on PATH.  Install via `gcloud components install gsutil` "
            "or `brew install --cask google-cloud-sdk`."
        )
    if max_shards < 1:
        raise ValueError("max_shards must be >= 1")

    target_dir = cache_dir / table
    target_dir.mkdir(parents=True, exist_ok=True)

    shards = list_shards(bucket, table)
    if not shards:
        raise RuntimeError(f"no shards found at {bucket}/{table}/ — check bucket name")

    selected = shards[:max_shards]
    downloaded: list[Path] = []
    for uri in selected:
        name = uri.rsplit("/", 1)[-1]
        local = target_dir / name
        if local.exists() and local.stat().st_size > 0:
            log.info("skip (cached): %s", local)
            downloaded.append(local)
            continue
        log.info("downloading %s -> %s", uri, local)
        subprocess.run(["gsutil", "cp", uri, str(local)], check=True)
        downloaded.append(local)

    return downloaded


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fetch a Borg 2019 subset from public GCS")
    parser.add_argument("--table", required=True, choices=sorted(ALLOWED_TABLES))
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--shards", type=int, default=1, help="Number of shards to pull")
    args = parser.parse_args()

    try:
        files = fetch(
            args.table,
            bucket=args.bucket,
            cache_dir=Path(args.cache_dir),
            max_shards=args.shards,
        )
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        log.error("%s", e)
        sys.exit(1)

    print(f"\nDownloaded {len(files)} shard(s) into {args.cache_dir}/{args.table}/")
    for f in files:
        print(f"  - {f}")


if __name__ == "__main__":
    main()
