import json
from pathlib import Path

import pyarrow as pa
import pytest

from fnb_platform import publication
from fnb_platform.google import places_silver as silver
from fnb_platform.overture_incremental import _digest


@pytest.mark.parametrize('case',['input_inside_output','output_inside_input_directory','equal','file_as_output_ancestor'])
def test_real_overlap_rejected_with_resolved_pair(tmp_path,case):
    root=tmp_path/'root';root.mkdir()
    if case=='input_inside_output':output,source=root,root/'input.json';source.write_text('{}')
    elif case=='output_inside_input_directory':source,output=root,root/'publication'
    elif case=='equal':source=output=root
    else:source=root/'input.json';source.write_text('{}');output=source/'publication'
    with pytest.raises(ValueError,match='Output overlaps an input directory') as error:
        publication.validate_publication_paths(output,[source])
    assert str(output.resolve()) in str(error.value) and str(source.resolve()) in str(error.value)


def test_campaign_metadata_and_publication_are_independent(tmp_path):
    root=tmp_path/'campaign';root.mkdir()
    boundary=root/'boundary.geojson';boundary.write_text('{}')
    receipt=root/'transport-receipt.json';receipt.write_text('{}')
    publication.validate_publication_paths(root/'mapping',[boundary,receipt])
    publication.validate_publication_paths(root/'mapping',[root/'mapping-inputs/data.parquet'])


def test_resolved_symlink_overlap_rejected(tmp_path):
    target=tmp_path/'actual';target.mkdir()
    source=target/'input.json';source.write_text('{}')
    alias=tmp_path/'alias'
    try:alias.symlink_to(target,target_is_directory=True)
    except OSError:pytest.skip('Symlinks unavailable')
    with pytest.raises(ValueError,match='Output overlaps'):
        publication.validate_publication_paths(alias,[source])


def test_sibling_publication_atomic_replay_never_overwrites_inputs(tmp_path):
    source=tmp_path/'transport-receipt.json';source.write_text('{"original":true}')
    original=source.read_bytes();output=tmp_path/'mapping'
    def writer(stage,rid):return {'rows.parquet':pa.table({'id':['fixture']})},{'fixture':True},[]
    def publish(revision=1,fault=lambda stage:None):
        signature=dict(rule_version='path-fixture-v1',publication_kind='FIXTURE',revision=revision,
            inputs_sha256={str(source):silver.sha(source)},code_sha256={})
        return publication.publish_release(output,signature,writer,read_active=silver.read_active,
            verify_release=silver.verify_release,digest=_digest,dump=silver.dump,fault=fault)
    release,_=publish();pointer=(output/'current.json').read_bytes()
    files={str(p):silver.sha(p) for p in release.rglob('*') if p.is_file()}
    assert publish()[0]==release and (output/'current.json').read_bytes()==pointer
    def fail(phase):
        if phase=='before_pointer_replace':raise RuntimeError('injected failure')
    with pytest.raises(RuntimeError,match='injected'):
        publish(2,fail)
    assert (output/'current.json').read_bytes()==pointer
    assert source.read_bytes()==original
    assert all(silver.sha(p)==h for p,h in files.items())
    recovered,_=publish(2)
    assert recovered!=release and json.loads((output/'current.json').read_text())['release_id']==recovered.name
    assert source.read_bytes()==original


def test_overlapping_publication_rejected_before_writer_or_directory_creation(tmp_path):
    source=tmp_path/'root/input.json';source.parent.mkdir();source.write_text('{}')
    original=source.read_bytes()
    def forbidden(*args):raise AssertionError('Writer must not run')
    signature=dict(inputs_sha256={str(source):silver.sha(source)},code_sha256={})
    with pytest.raises(ValueError,match='Output overlaps'):
        publication.publish_release(source.parent,signature,forbidden,read_active=forbidden,
            verify_release=forbidden,digest=_digest,dump=silver.dump)
    assert source.read_bytes()==original and not (source.parent/'current.json').exists()
