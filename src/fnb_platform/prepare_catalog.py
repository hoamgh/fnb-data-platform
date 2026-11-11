"""Prepare one reusable ward catalog from a cached snapshot or explicit Overture release."""
from __future__ import annotations

import argparse
import importlib.metadata
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from .overture_snapshot import BRONZE_STAGING_PREFIX, release_identity, validate_snapshot

from .catalog import (
    DEFAULT_CATALOG, DEFAULT_SOURCE, DEFAULT_SOURCE_METADATA, DEFAULT_WARD, prepare_catalog, publication_root,
)


def _publish_bronze(destination, lineage, *, bbox, release, read_table, fault=lambda phase, path: None):
    """Commit one fresh Windows snapshot by a no-replace directory rename.

    Preserve failed staging for inspection. No locks, pointers or automatic
    recovery deletion. Windows rename refuses even an existing empty directory;
    POSIX rename can replace it, so unverified platforms fail before acquisition.
    This is process-crash consistency, not a power-loss durability guarantee.
    """
    import pyarrow.parquet as pq
    destination = Path(destination).absolute()
    if os.name != "nt":
        raise RuntimeError("Bronze no-replace directory publication is verified only on Windows")
    if os.path.lexists(destination):
        raise FileExistsError(f"Bronze destination already exists: {destination}")
    if any(p.name.startswith(BRONZE_STAGING_PREFIX) for p in (destination, *destination.parents)):
        raise ValueError("Final Bronze destination cannot use the reserved staging prefix")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=BRONZE_STAGING_PREFIX, dir=destination.parent))
    table = read_table()
    artifact, metadata_path = stage / 'places.parquet', stage / 'source_metadata.json'
    pq.write_table(table, artifact, compression='zstd')
    fault('after_parquet_write', stage)
    if not pq.ParquetFile(artifact).read().equals(table):
        raise RuntimeError('Bronze Parquet read-back mismatch')
    metadata = dict(lineage, bbox=list(bbox), rows=table.num_rows,
                   artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    fault('after_metadata_write', stage)
    validate_snapshot(artifact, metadata_path, expected_release=release, _allow_staging=True)
    fault('before_final_rename', stage)
    # Do not use replace(): Windows rename itself closes the destination-exists race.
    stage.rename(destination)
    fault('after_final_rename', destination)
    return table


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--source-metadata", type=Path, default=DEFAULT_SOURCE_METADATA)
    parser.add_argument("--ward", type=Path, default=DEFAULT_WARD)
    parser.add_argument("--output", type=Path, default=DEFAULT_CATALOG,
                        help="Logical catalog path; load_catalog resolves its immutable active generation")
    parser.add_argument("--download", action="store_true", help="Explicit bounded bbox access, not latest-release discovery")
    parser.add_argument("--release", help="Actual release to query; required with --download")
    parser.add_argument("--bronze-dir", type=Path, help="Fresh raw snapshot directory; required with --download")
    args = parser.parse_args(argv)
    observed_at = datetime.now(timezone.utc).isoformat()
    reader = None
    if args.download:
        if not args.release or args.bronze_dir is None:
            parser.error("--download requires an explicit --release and fresh --bronze-dir")
        if args.bronze_dir.exists():
            parser.error('Bronze directory already exists; use a fresh snapshot directory')
        raw_path = args.bronze_dir/'places.parquet'
        if args.output.resolve() == raw_path.resolve():
            parser.error('Catalog output cannot overwrite its Bronze input')
        release_identity(args.release)
        from overturemaps import record_batch_reader
        lineage = dict(source="overture", source_release=args.release, client_version=importlib.metadata.version("overturemaps"),
                       retrieved_at=observed_at, source_catalog="https://stac.overturemaps.org/catalog.json",
                       license_attribution="Overture Maps source-specific attribution: https://docs.overturemaps.org/attribution/")
        def reader(*, bbox, release):
            def acquire():
                batches = record_batch_reader("place", bbox=bbox, release=release, stac=True,
                                              connect_timeout=10, request_timeout=60)
                if batches is None:
                    raise RuntimeError("Overture returned no reader")
                return batches.read_all()
            return _publish_bronze(args.bronze_dir, lineage, bbox=bbox, release=release, read_table=acquire)
    else:
        if args.output.resolve() == args.source.resolve():
            parser.error('Catalog output cannot overwrite its Bronze input')
        lineage = validate_snapshot(args.source, args.source_metadata, expected_release=args.release)
    _, manifest = prepare_catalog(args.source, args.ward, args.output, lineage,
                                 observed_at=observed_at, reader=reader)
    print(json.dumps(dict(catalog_path=str(args.output),
                         catalog_pointer=str(publication_root(args.output) / "current.json"),
                         **manifest), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
