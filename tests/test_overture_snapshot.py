"""Offline acquisition/legacy metadata and comparison-runner contracts."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

from experiments.overture_incremental import run
from fnb_platform.overture_incremental import compare_releases
from fnb_platform.overture_snapshot import validate_snapshot, validate_pair_scope


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_new(snapshots, table):
    pq.write_table(table, snapshots['new_path'])
    metadata = json.loads(snapshots['new_metadata'].read_text())
    metadata['artifact_sha256'] = digest(snapshots['new_path'])
    write_json(snapshots['new_metadata'], metadata)


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('change', ['nested_add', 'nested_remove', 'type', 'nullable'])
def test_selected_schema_drift_fails_before_writes(snapshots, tmp_path, change, existing):
    table = pq.read_table(snapshots['new_path'])
    rows = table.to_pylist()
    if change == 'nested_add':
        for row in rows:
            row['names']['alternate'] = 'New evidence'
        table = pa.Table.from_pylist(rows)
    elif change == 'nested_remove':
        for row in rows:
            del row['taxonomy']['hierarchy']
        table = pa.Table.from_pylist(rows)
    elif change == 'type':
        i = table.schema.get_field_index('confidence')
        table = table.set_column(i, 'confidence', table['confidence'].cast(pa.float32()))
    else:
        table = table.cast(pa.schema([f.with_nullable(False) if f.name == 'id' else f for f in table.schema]))
    replace_new(snapshots, table)
    if existing:
        (tmp_path/'result').mkdir()
        (tmp_path/'result/evidence').write_text('keep')
    before = {p: digest(p) for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='Selected raw schema mismatch'):
        run.main(tmp_path/'result', **snapshots)
    assert before == {p: digest(p) for p in tmp_path.rglob('*') if p.is_file()}
    assert (tmp_path/'result').exists() == existing
    assert not (tmp_path/'.result.stage').exists()


def test_unselected_addition_preserves_raw_and_records_policy(snapshots, tmp_path):
    table = pq.read_table(snapshots['new_path'])
    table = table.append_column('outside_comparison', pa.array(['evidence'] * table.num_rows))
    replace_new(snapshots, table)
    before = digest(snapshots['new_path'])
    run.main(tmp_path/'result', **snapshots)
    manifest = json.loads((tmp_path/'result/manifest.json').read_text())
    assert manifest['comparison_schema_policy']['ignored_top_level_fields']['new'] == ['outside_comparison']
    assert pq.read_schema(tmp_path/'result/new.parquet').names == list(run.FIELDS)
    assert digest(snapshots['new_path']) == before


def test_nullable_values_with_declared_types_remain_compatible(snapshots, tmp_path):
    table = pq.read_table(snapshots['new_path'])
    i = table.schema.get_field_index('confidence')
    replace_new(snapshots, table.set_column(i, table.schema.field(i), pa.nulls(table.num_rows, pa.float64())))
    run.main(tmp_path/'result', **snapshots)
    assert pq.read_table(tmp_path/'result/new.parquet')['confidence'].null_count == table.num_rows


def test_pinned_boundary_preserves_covers_edge_and_buffer(snapshots):
    from experiments.overture_silver import consumer as c
    polygon = run.boundary_polygon(snapshots['ward_path'].read_bytes())
    template = pq.read_table(snapshots['new_path']).to_pylist()[0]
    points = [Point(106.7, 10.755), Point(106.699, 10.758), Point(106.704, 10.758)]
    rows = {str(i): dict(template, id=str(i), geometry=point.wkb) for i, point in enumerate(points)}
    result = c.make_silver(rows, '2026-10-21.0', 'fixture', {'spatial_filter': 'fixture'}, ward=polygon)
    assert [r['inside_ward'] for r in result] == [True, False, True]
    assert [r['on_ward_boundary'] for r in result] == [True, False, False]
    assert polygon.buffer(run.BUFFER_DEGREES).covers(points[1])


@pytest.mark.parametrize('legacy', [False, True])
def test_boundary_pinned_through_silver_without_current_checkout(snapshots, tmp_path, monkeypatch, legacy):
    from experiments.overture_silver import consumer as c
    source, publication, silver = (tmp_path/name for name in ('comparison', 'publication', 'silver'))
    boundary = snapshots['ward_path'].read_bytes()
    run.main(source, **snapshots)
    manifest = json.loads((source/'manifest.json').read_text())
    assert (source/run.BOUNDARY_ARTIFACT).read_bytes() == boundary
    assert manifest['input_sha256']['polygon'] == digest(source/run.BOUNDARY_ARTIFACT)
    if legacy:
        del manifest['boundary_artifact']
        del manifest['output_sha256'][run.BOUNDARY_ARTIFACT]
        write_json(source/'manifest.json', manifest)
        (source/run.BOUNDARY_ARTIFACT).unlink()
        monkeypatch.setattr(c, 'WARD_PATH', snapshots['ward_path'])
    else:
        snapshots['ward_path'].unlink()
        monkeypatch.setattr(c, 'WARD_PATH', tmp_path/'absent-ward')
    c.bronze.publish(source, publication)
    directory, result = c.publish(source, publication, silver)
    assert (directory/run.BOUNDARY_ARTIFACT).read_bytes() == boundary
    assert pq.read_table(directory/'current_state.parquet')['inside_ward'].to_pylist() == [True] * 5
    monkeypatch.setattr(c, 'WARD_PATH', tmp_path/'absent-ward')
    monkeypatch.setattr(c, 'WARD_METADATA', tmp_path/'absent-metadata')
    assert c.verify_silver(directory) == result
    (directory/run.BOUNDARY_ARTIFACT).write_bytes(boundary + b' ')
    with pytest.raises(c.SilverError, match='boundary checksum mismatch'):
        c.verify_silver(directory)


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'legacy_wrong', 'legacy_no_hash'])
def test_unavailable_boundary_fails_before_silver_publication(snapshots, tmp_path, monkeypatch, failure):
    from experiments.overture_silver import consumer as c
    source, publication, silver = (tmp_path/name for name in ('comparison', 'publication', 'silver'))
    run.main(source, **snapshots)
    if failure.startswith('legacy'):
        manifest = json.loads((source/'manifest.json').read_text())
        del manifest['boundary_artifact']
        del manifest['output_sha256'][run.BOUNDARY_ARTIFACT]
        if failure == 'legacy_no_hash':
            del manifest['input_sha256']['polygon']
        write_json(source/'manifest.json', manifest)
        monkeypatch.setattr(c, 'WARD_PATH', snapshots['ward_path'])
        snapshots['ward_path'].write_bytes(snapshots['ward_path'].read_bytes() + b' ')
    c.bronze.publish(source, publication)
    if failure == 'missing':
        (source/run.BOUNDARY_ARTIFACT).unlink()
    elif failure == 'corrupt':
        (source/run.BOUNDARY_ARTIFACT).write_bytes(b'corrupt')
    before = {p: digest(p) for p in publication.rglob('*') if p.is_file()}
    with pytest.raises((ValueError, FileNotFoundError)):
        c.publish(source, publication, silver)
    assert not silver.exists()
    assert before == {p: digest(p) for p in publication.rglob('*') if p.is_file()}


@pytest.fixture
def snapshots(tmp_path):
    rows = [dict(id=str(i), names={"primary": "Cafe"}, basic_category="food_and_drink",
                 taxonomy={"primary": "cafe", "hierarchy": ["food_and_drink", "cafe"]},
                 operating_status="open", addresses=[{"freeform": "Fixture"}],
                 geometry=Point(106.704, 10.758).wkb, confidence=.8) for i in range(5)]
    new_rows = [dict(row) for row in rows]
    new_rows[0] = dict(new_rows[0], names={"primary": "Changed"})
    new_rows[1] = dict(new_rows[1], confidence=.9)
    new_rows[4] = dict(new_rows[4], id="new")
    paths = {}
    for label, values, release in (("old", rows, "2026-09-23.1"), ("new", new_rows, "2026-10-21.0")):
        path, metadata = tmp_path/(label+'.parquet'), tmp_path/(label+'.json')
        pq.write_table(pa.Table.from_pylist(values), path)
        if label == 'old':
            info = dict(release=release, sha256=digest(path), raw_count=len(values),
                        hcm_bbox=dict(west=106.69, south=10.74, east=106.72, north=10.78))
        else:
            info = dict(source_release=release, artifact_sha256=digest(path), rows=len(values),
                        bbox=[106.69,10.74,106.72,10.78], source='overture')
        write_json(metadata, info)
        paths[label+'_path'], paths[label+'_metadata'] = path, metadata
    ward = tmp_path/'ward.json'
    write_json(ward, dict(type='Feature', geometry=dict(type='Polygon', coordinates=[[
        [106.7,10.755],[106.708,10.755],[106.708,10.763],[106.7,10.763],[106.7,10.755]]])))
    return dict(paths, ward_path=ward)


def test_legacy_and_acquisition_aliases_compare_without_changing_sources(snapshots, tmp_path):
    before = {p: digest(p) for p in snapshots.values()}
    result = compare_releases(*(snapshots[k] for k in ('old_path','old_metadata','new_path','new_metadata')))
    assert result['counts'] == dict(NEW=1, UPDATED=2, UNCHANGED=2, MISSING_FROM_RELEASE=1)
    assert 'not confirmed closure' in result['interpretation']
    assert before == {p: digest(p) for p in before}


@pytest.mark.parametrize('side', ['old','new'])
@pytest.mark.parametrize('existing', [False, True])
def test_checksum_failure_precedes_any_output_write(snapshots, tmp_path, side, existing):
    output = tmp_path/'result'
    if existing:
        output.mkdir()
        (output/'evidence').write_text('keep')
    path = snapshots[side+'_path']
    with path.open('ab') as stream:
        stream.write(b'corrupt')
    before = {p: digest(p) for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='checksum mismatch'):
        run.main(output, **snapshots)
    assert before == {p: digest(p) for p in tmp_path.rglob('*') if p.is_file()}
    assert output.exists() == existing
    assert not (tmp_path/'.result.stage').exists()


@pytest.mark.parametrize('update,remove,error', [
    ({}, 'artifact_sha256', 'Missing snapshot metadata'),
    ({'sha256':'0'*64}, None, 'Conflicting'),
    ({'release':'2026-10-22.0'}, None, 'Conflicting'),
    ({'hcm_bbox':[-1,-1,4,4]}, None, 'Conflicting'),
    ({'source':'google'}, None, 'Source identity mismatch'),
    ({'bbox':[0,0,float('nan'),2]}, None, 'Malformed bbox'),
    ({'source_release':'not-a-release'}, None, 'Malformed Overture release'),
    ({'snapshot_path':'different.parquet'}, None, 'Snapshot path mismatch'),
    ({'raw_count':99}, None, 'Conflicting'),
    ({'spatial_filter':None}, None, 'Malformed extraction scope'),
])
def test_rejects_missing_ambiguous_or_malformed_metadata(snapshots, update, remove, error):
    path = snapshots['new_metadata']
    info = json.loads(path.read_text())
    if remove:
        del info[remove]
    info.update(update)
    write_json(path, info)
    with pytest.raises(ValueError, match=error):
        validate_snapshot(snapshots['new_path'], path)


def test_equivalent_aliases_and_relative_declared_path(snapshots):
    path = snapshots['new_metadata']
    info = json.loads(path.read_text())
    info.update(release=info['source_release'], sha256=info['artifact_sha256'].upper(),
                source_name='Overture Maps', snapshot_path='new.parquet')
    write_json(path, info)
    assert validate_snapshot(snapshots['new_path'], path)['source'] == 'overture'


@pytest.mark.parametrize('case', ['bbox','scope','same_release','reverse'])
def test_pair_scope_rejected_before_output(snapshots, tmp_path, case):
    path = snapshots['new_metadata']
    info = json.loads(path.read_text())
    info.update({'bbox': [0,0,1,1]} if case == 'bbox' else
                {'spatial_filter':'only cafes'} if case == 'scope' else
                {'source_release':'2026-09-23.1' if case == 'same_release' else '2026-08-01.0'})
    write_json(path, info)
    with pytest.raises(ValueError, match='scope mismatch|distinct|sequence'):
        run.main(tmp_path/'result', **snapshots)
    assert not (tmp_path/'result').exists()
    assert not (tmp_path/'.result.stage').exists()


def test_explicit_runner_derives_labels_and_reuses_only_matching_inputs(snapshots, tmp_path):
    for side, release in (('old','2027-01-01.0'), ('new','2027-02-01.0')):
        path = snapshots[side+'_metadata']
        info = json.loads(path.read_text())
        info['release' if side == 'old' else 'source_release'] = release
        write_json(path, info)
    output = tmp_path/'result'
    run.main(output, **snapshots)
    manifest = json.loads((output/'manifest.json').read_text())
    assert (manifest['old_release'], manifest['new_release']) == ('2027-01-01.0','2027-02-01.0')
    assert json.loads((output/'new.json').read_text())['release'] == '2027-02-01.0'
    assert manifest['rows']['metadata_only_updates'] == 1
    assert manifest['rows']['substantive_updates'] == 1
    before = {p: digest(p) for p in output.iterdir()}
    run.main(output, **snapshots)
    assert before == {p: digest(p) for p in before}
    path = snapshots['new_metadata']
    info = json.loads(path.read_text()); info['source_release'] = '2027-03-01.0'
    write_json(path, info)
    with pytest.raises(ValueError, match='input/release mismatch'):
        run.main(output, **snapshots)
    assert before == {p: digest(p) for p in before}


def test_frozen_benchmark_preserves_labels_and_rejects_relabelled_input(snapshots, tmp_path, monkeypatch):
    for constant, argument in (('OLD','old_path'),('OLD_META','old_metadata'),('NEW','new_path'),
                               ('NEW_META','new_metadata'),('WARD','ward_path')):
        monkeypatch.setattr(run, constant, snapshots[argument])
    output = tmp_path/'historical'
    run.main(output)
    manifest = json.loads((output/'manifest.json').read_text())
    assert manifest['old_release'] == '2026-09-23.1'
    assert manifest['new_release'] == '2026-10-21.0'
    assert manifest['filter_scope']['extraction_bbox'] == [106.693,10.749,106.715,10.769]
    path = snapshots['new_metadata']
    info = json.loads(path.read_text()); info['source_release'] = '2026-11-18.0'
    write_json(path, info)
    before = {p: digest(p) for p in output.iterdir()}
    with pytest.raises(ValueError, match='Release mismatch'):
        run.main(output)
    assert before == {p: digest(p) for p in before}
