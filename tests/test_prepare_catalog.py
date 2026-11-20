"""Reused entrypoint, fixture-only download and snapshot validation."""
import hashlib
import json
from types import SimpleNamespace
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

from fnb_platform import prepare_catalog as cli
from fnb_platform.catalog import load_catalog
from fnb_platform.overture_snapshot import validate_snapshot, validate_pair_scope


def test_acquisition_and_snapshot_share_legacy_globals(monkeypatch):
    from fnb_platform.overture import acquisition, snapshot, catalog
    from fnb_platform import overture_snapshot
    assert cli is acquisition
    assert overture_snapshot is snapshot
    assert acquisition.prepare_catalog is catalog.prepare_catalog
    sentinel = lambda *args, **kwargs: None
    monkeypatch.setattr(cli, 'validate_snapshot', sentinel)
    assert acquisition.main.__globals__['validate_snapshot'] is sentinel
    monkeypatch.setattr(overture_snapshot, 'BRONZE_STAGING_PREFIX', 'fixture-only')
    assert snapshot.validate_snapshot.__globals__['BRONZE_STAGING_PREFIX'] == 'fixture-only'


@pytest.mark.parametrize('module', ['fnb_platform.prepare_catalog',
                                   'fnb_platform.overture.acquisition'])
def test_catalog_old_new_cli_help(module):
    import os
    import subprocess
    from pathlib import Path
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    result = subprocess.run([sys.executable, '-B', '-m', module, '--help'],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '--source-metadata' in result.stdout and '--bronze-dir' in result.stdout


def inputs(tmp_path):
    ward=tmp_path/'ward.json'
    ward.write_text(json.dumps({'type':'Feature','properties':{
        'admin_area_id':'fixture','admin_area_name':'Fixture','is_current':True},
        'geometry':{'type':'Polygon','coordinates':[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}}))
    table=pa.Table.from_pylist([{'id':'fixture:1','names':{'primary':'Cafe'},
        'geometry':Point(1,1).wkb,'taxonomy':{'primary':'coffee_shop','hierarchy':['food_and_drink','coffee_shop']},
        'addresses':[{'freeform':'123 Fixture'}],'confidence':0.8}])
    return ward,table


def test_cached_cli_and_source_not_overwritten(tmp_path):
    ward,table=inputs(tmp_path)
    raw=tmp_path/'raw.parquet';pq.write_table(table,raw)
    checksum=hashlib.sha256(raw.read_bytes()).hexdigest()
    metadata=tmp_path/'source.json';metadata.write_text(json.dumps(dict(source_release='2026-09-23.1',bbox=[0,0,2,2],artifact_sha256=checksum)))
    output=tmp_path/'catalog.parquet'
    assert cli.main(['--source',str(raw),'--source-metadata',str(metadata),
                     '--ward',str(ward),'--output',str(output)])==0
    assert len(load_catalog(output).places)==1
    assert hashlib.sha256(raw.read_bytes()).hexdigest()==checksum
    with pytest.raises(SystemExit):
        cli.main(['--source',str(raw),'--output',str(raw)])
    assert hashlib.sha256(raw.read_bytes()).hexdigest()==checksum


def test_download_uses_existing_client_and_preserves_raw_bronze(tmp_path,monkeypatch):
    ward,table=inputs(tmp_path);calls=[]
    def batches(kind,**kwargs):
        calls.append((kind,kwargs))
        return SimpleNamespace(read_all=lambda:table)
    monkeypatch.setitem(sys.modules,'overturemaps',SimpleNamespace(record_batch_reader=batches))
    monkeypatch.setattr(cli.importlib.metadata,'version',lambda _: 'fixture-client')
    bronze=tmp_path/'bronze';output=tmp_path/'catalog.parquet'
    args=['--download','--release','2026-09-23.1','--bronze-dir',str(bronze),
          '--ward',str(ward),'--output',str(output)]
    assert cli.main(args)==0
    assert calls[0][0]=='place' and calls[0][1]['bbox']==(0.0,0.0,2.0,2.0)
    assert calls[0][1]['release']=='2026-09-23.1' and len(calls)==1
    assert pq.ParquetFile(bronze/'places.parquet').read().equals(table)
    info=json.loads((bronze/'source_metadata.json').read_text())
    assert info['artifact_sha256']==hashlib.sha256((bronze/'places.parquet').read_bytes()).hexdigest()
    validated = validate_snapshot(bronze/'places.parquet', bronze/'source_metadata.json')
    assert validated['source'] == 'overture'
    next_metadata = tmp_path/'next.json'
    next_metadata.write_text(json.dumps(dict(info, source_release='2026-10-21.0')))
    validate_pair_scope(validated, validate_snapshot(bronze/'places.parquet', next_metadata))
    from fnb_platform.overture_incremental import compare_releases
    comparison = compare_releases(bronze/'places.parquet', bronze/'source_metadata.json',
                                  bronze/'places.parquet', next_metadata)
    assert comparison['counts'] == dict(NEW=0, UPDATED=0, UNCHANGED=1, MISSING_FROM_RELEASE=0)
    assert len(load_catalog(output).places)==1
    with pytest.raises(SystemExit):cli.main(args)
    assert len(calls)==1  # Existing snapshots cannot trigger another request.


def test_corrupt_bronze_refuses_before_catalog_write(tmp_path):
    ward,table=inputs(tmp_path);raw=tmp_path/'raw.parquet';pq.write_table(table,raw)
    metadata=tmp_path/'source.json';metadata.write_text(json.dumps(dict(source_release='2026-09-23.1',bbox=[0,0,2,2],artifact_sha256='0'*64)))
    output=tmp_path/'catalog.parquet'
    with pytest.raises(ValueError,match='checksum'):
        cli.main(['--source',str(raw),'--source-metadata',str(metadata),'--ward',str(ward),'--output',str(output)])
    assert not output.exists()


def test_download_requires_explicit_release_and_fresh_bronze(tmp_path):
    with pytest.raises(SystemExit):cli.main(['--download'])
    with pytest.raises(SystemExit):cli.main(['--download','--release','fixture'])


def publish_fixture(destination, table, *, fault=lambda phase, path: None):
    return cli._publish_bronze(destination, dict(source='overture', source_release='2026-09-23.1'),
        bbox=[0, 0, 2, 2], release='2026-09-23.1', read_table=lambda: table, fault=fault)


@pytest.mark.parametrize('phase,published', [
    ('after_parquet_write', False), ('after_metadata_write', False),
    ('before_final_rename', False), ('after_final_rename', True),
])
def test_bronze_fault_boundaries_and_rerun(tmp_path, phase, published):
    _, table = inputs(tmp_path)
    destination = tmp_path/'bronze'
    def crash(at, path):
        if at == phase:
            raise RuntimeError('injected crash')
    with pytest.raises(RuntimeError, match='injected'):
        publish_fixture(destination, table, fault=crash)
    assert destination.exists() == published
    stages = list(tmp_path.glob(cli.BRONZE_STAGING_PREFIX+'*'))
    if published:
        assert not stages
        assert pq.read_table(destination/'places.parquet').equals(table)
        validate_snapshot(destination/'places.parquet', destination/'source_metadata.json')
        before = {p.name: p.read_bytes() for p in destination.iterdir()}
        with pytest.raises(FileExistsError):
            publish_fixture(destination, table)
        assert {p.name: p.read_bytes() for p in destination.iterdir()} == before
    else:
        assert len(stages) == 1
        with pytest.raises(ValueError, match='Unpublished Bronze staging'):
            validate_snapshot(stages[0]/'places.parquet', stages[0]/'source_metadata.json')
        before = {p.name: p.read_bytes() for p in stages[0].iterdir()}
        publish_fixture(destination, table)
        assert pq.read_table(destination/'places.parquet').equals(table)
        validate_snapshot(destination/'places.parquet', destination/'source_metadata.json')
        assert {p.name: p.read_bytes() for p in stages[0].iterdir()} == before


@pytest.mark.parametrize('damage', ['metadata', 'artifact', 'missing_metadata', 'missing_artifact'])
def test_bronze_corruption_rejected_before_commit(tmp_path, damage):
    _, table = inputs(tmp_path)
    destination = tmp_path/'bronze'
    def corrupt(phase, path):
        if phase != 'after_metadata_write':
            return
        if damage == 'metadata':
            (path/'source_metadata.json').write_text('{bad json')
        elif damage == 'artifact':
            with (path/'places.parquet').open('ab') as stream:
                stream.write(b'corrupt')
        else:
            (path/('source_metadata.json' if damage == 'missing_metadata' else 'places.parquet')).unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        publish_fixture(destination, table, fault=corrupt)
    assert not destination.exists()
    assert len(list(tmp_path.glob(cli.BRONZE_STAGING_PREFIX+'*'))) == 1


@pytest.mark.parametrize('kind', ['empty', 'nonempty', 'file'])
@pytest.mark.parametrize('race', [False, True])
def test_bronze_destination_never_overwritten_even_in_rename_race(tmp_path, kind, race):
    _, table = inputs(tmp_path)
    destination = tmp_path/'bronze'
    def create_existing():
        if kind == 'file':
            destination.write_bytes(b'keep')
        else:
            destination.mkdir()
            if kind == 'nonempty':
                (destination/'evidence').write_bytes(b'keep')
    if not race:
        create_existing()
    def before_rename(phase, path):
        if phase == 'before_final_rename':
            create_existing()
    with pytest.raises(OSError):
        publish_fixture(destination, table, fault=before_rename)
    if kind == 'file':
        assert destination.read_bytes() == b'keep'
    elif kind == 'empty':
        assert list(destination.iterdir()) == []
    else:
        assert [p.name for p in destination.iterdir()] == ['evidence']
        assert (destination/'evidence').read_bytes() == b'keep'
    assert len(list(tmp_path.glob(cli.BRONZE_STAGING_PREFIX+'*'))) == int(race)


def test_bronze_concurrent_publish_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    _, table = inputs(tmp_path)
    destination = tmp_path/'bronze'
    rendezvous = Barrier(2)
    def wait_for_both(phase, path):
        if phase == 'before_final_rename':
            rendezvous.wait(timeout=5)
    def attempt():
        try:
            publish_fixture(destination, table, fault=wait_for_both)
            return 'published'
        except FileExistsError:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == ['published', 'refused']
    assert pq.read_table(destination/'places.parquet').equals(table)
    validate_snapshot(destination/'places.parquet', destination/'source_metadata.json')
    assert len(list(tmp_path.glob(cli.BRONZE_STAGING_PREFIX+'*'))) == 1


def test_unverified_platform_refuses_before_acquisition(tmp_path, monkeypatch):
    _, table = inputs(tmp_path)
    monkeypatch.setattr(cli, 'os', SimpleNamespace(name='posix'))
    with pytest.raises(RuntimeError, match='only on Windows'):
        cli._publish_bronze(tmp_path/'bronze', {}, bbox=[0,0,2,2], release='2026-09-23.1',
                            read_table=lambda: pytest.fail('must not acquire'))
    assert not list(tmp_path.glob(cli.BRONZE_STAGING_PREFIX+'*'))
