"""M1.5 audit regressions and feature-only publication. No ER decisions."""
import json
import unicodedata
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fnb_platform import comparison_features as f
from fnb_platform import google_silver as g
from fnb_platform.overture_incremental import _digest


@pytest.mark.parametrize('name',[
    'Quán Ba Hồng', 'Highlands Coffee Ben Van Don HCM', 'Hồng Trà Ngô Gia H103',
    'Pizza Hong Kong 301B', 'Phúc Kiến Milk Tea', 'ABC-01 / Chi nhánh 12/3',
])
def test_shared_name_policy_preserves_brand_and_branch_tokens(name):
    nfd=unicodedata.normalize('NFD',name)
    assert f.comparison_text(name)==f.comparison_text('  '+nfd.upper().replace(' ','\t')+'  ')
    assert f.comparison_text(name)
    assert f.comparison_text('Hồng Trà Ngô Gia H103').endswith('h103')
    assert '12/3' in f.comparison_text('ABC-01 / Chi nhánh 12/3')
    assert 'milk tea' in f.comparison_text('Phúc Kiến Milk Tea')


def test_unicode_name_comparison_never_uses_generic_word_removal():
    assert f.comparison_text('ＣＡＦＥ ĐỨC')=='cafe duc'
    assert f.comparison_text('Trà Phúc Kiến')=='tra phuc kien'
    assert f.comparison_text('茶店 ABC')=='茶店 abc'
    assert f.comparison_text(None) is None


@pytest.mark.parametrize('address,identifier,base,suffix,path',[
    ('122 Vĩnh Khánh','122','122',None,['122']),
    ('122A Vĩnh Khánh','122a','122','a',['122a']),
    ('122/14 Vĩnh Khánh','122/14','122',None,['122','14']),
    ('128 Vĩnh Khánh','128','128',None,['128']),
    ('301B Đ. Tôn Đản','301b','301','b',['301b']),
    ('134/41 Đoàn Văn Bơ','134/41','134',None,['134','41']),
])
def test_house_components_remain_distinct(address,identifier,base,suffix,path):
    p=f.parse_address(address)
    assert p['house_identifier']==identifier and p['house_number']==base
    assert p['house_suffix']==suffix and p['house_path']==path
    assert p['parse_status']=='PARSED' and p['address_raw']==address


def test_spacing_in_house_path_does_not_change_the_number_or_raw_address():
    raw='134 / 41 Đoàn Văn Bơ'
    p=f.parse_address(raw)
    assert p['house_identifier']=='134/41' and p['street_comparison']=='doan van bo'
    assert p['address_raw']==raw


@pytest.mark.parametrize('address',['158-160 Hoàng Diệu','158-160, Hoàng Diệu','158 – 160 Đường Hoàng Diệu'])
def test_actual_ba_hong_range_is_not_a_street_conflict(address):
    left=f.parse_address(address);right=f.parse_address('\ue3c8\n160 Hoàng Diệu, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert left['house_range_start']=='158' and left['house_range_end']=='160'
    assert left['street_comparison']==right['street_comparison']=='hoang dieu'
    assert f.house_relation(left,right)=='RANGE_CONTAINS_LITERAL'
    assert left['house_identifier'] is None


@pytest.mark.parametrize('separator',[', ','/'])
def test_actual_cu_xa_alley_street_are_separate(separator):
    raw='Cư Xá Vĩnh Hội'+separator+'Hẻm 183 Bến Vân Đồn'
    p=f.parse_address(raw)
    assert p['premise_context']==['Cư Xá Vĩnh Hội']
    assert p['alley_path']==['183'] and p['street_comparison']=='ben van don'
    assert p['house_number'] is None and p['parse_status']=='PARTIAL'
    assert p['address_raw']==raw


def test_location_reference_keeps_actual_number_but_marks_uncertainty():
    raw='Trước Cửa Nhà 134/41 Đoàn Văn Bơ'
    p=f.parse_address(raw)
    assert p['house_identifier']=='134/41' and p['street_comparison']=='doan van bo'
    assert p['parse_status']=='AMBIGUOUS' and p['house_is_reference']
    assert f.house_relation(p,f.parse_address('134/121 Đoàn Văn Bơ'))=='UNDETERMINED_REFERENCE'


def test_missing_and_unparseable_and_street_only_have_different_states():
    assert f.parse_address(None)['parse_status']=='MISSING'
    assert f.parse_address(' \t ')['parse_status']=='MISSING'
    p=f.parse_address('Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert p['parse_status']=='AMBIGUOUS' and p['street_comparison'] is None
    p=f.parse_address('L26 cư xá Vĩnh Hội, Hoàng Diệu, Q4')
    assert p['parse_status']=='PARTIAL' and p['house_identifier'] is None
    assert f.parse_address('Đường Vĩnh Khánh')['parse_status']=='PARTIAL'
    assert f.component_relation(None,'vinh khanh')=='MISSING_COMPONENT'
    assert f.house_relation(f.parse_address(None),f.parse_address('122 Vĩnh Khánh'))=='MISSING_COMPONENT'


def test_alley_and_house_roots_are_not_silently_conflated():
    p=f.parse_address('576/75 Hẻm 124 Đường Xóm Chiếu')
    assert p['house_identifier']=='576/75' and p['alley_path']==['124']
    assert p['street_comparison']=='xom chieu' and p['parse_status']=='AMBIGUOUS'
    assert f.house_relation(f.parse_address('122/37/9 Vĩnh Khánh'),f.parse_address('37/9 Vĩnh Khánh'))=='HIERARCHICAL_SUFFIX_OVERLAP'


def test_prefix_variants_and_numbered_streets_are_preserved():
    assert f.parse_address('301B Đ. Tôn Đản')['street_comparison']==f.parse_address('301b Đường Tôn Đản')['street_comparison']
    assert f.parse_address('79 Đường Số 41')['street_comparison']=='so 41'
    assert f.parse_address('Đường 20 Thước Đoàn Văn Bơ')['street_comparison']=='20 thuoc doan van bo'
    assert f.parse_address('TTTM Vincom, 216 Đ. Võ Văn Ngân, Thủ Đức')['house_identifier']=='216'
    assert f.parse_address('TTTM Vincom, 216 Đ. Võ Văn Ngân, Thủ Đức')['street_comparison']=='vo van ngan'


def test_actual_lot_locators_are_not_invented_streets():
    for raw in ['54 Lô O, Nguyễn Hữu Hào','Chung cư, 016 Lô A, Tôn Thất Thuyết','46 lô I cư xá vĩnh hội']:
        p=f.parse_address(raw)
        assert p['house_identifier'] and p['street_comparison'] is None
        assert p['parse_status']=='PARTIAL' and p['premise_context']


def test_ambiguous_ranges_and_multiple_numbers_never_establish_equality():
    for raw in ['160-158 Hoàng Diệu','122A-128B Hoàng Diệu','122 và 128 Hoàng Diệu']:
        p=f.parse_address(raw)
        assert p['parse_status']=='AMBIGUOUS'
        assert f.house_relation(p,f.parse_address('160 Hoàng Diệu'))=='UNDETERMINED'
    assert f.house_relation(f.parse_address('122 Vĩnh Khánh'),f.parse_address('122A Vĩnh Khánh'))=='DIFFERENT_LITERAL'
    assert f.house_relation(f.parse_address('122/14 Vĩnh Khánh'),f.parse_address('122 Vĩnh Khánh'))=='DIFFERENT_LITERAL'


@pytest.fixture
def inputs(tmp_path):
    ov=tmp_path/'overture/current_state.parquet'
    ov.parent.mkdir()
    pq.write_table(pa.Table.from_pylist([dict(source_id='ov1',name='Quán Ba Hồng',name_normalized='quán ba hồng',
        address='158-160, Hoàng Diệu',address_normalized='158-160, hoàng diệu')]),ov)
    root=tmp_path/'m1';release=root/'releases'/('a'*24);release.mkdir(parents=True)
    (release/'silver').mkdir()
    places=pa.Table.from_pylist([dict(google_id_kind='data_id',google_identity_value='0x1:0x2',
        name_display='Quán Ba Hồng',name_normalized='quan ba hong',address_display='160 Hoàng Diệu',
        address_normalized='160 hoang dieu',field_provenance_json='{}')],schema=g.PLACE_SCHEMA)
    links=pa.Table.from_pylist([dict(overture_id='ov1',google_id_kind='data_id',google_identity_value='0x1:0x2',
        dq_flags=['STREET_CONFLICT'],source_observation_refs=['raw.sqlite3#dispatch:1:attempt:0'],
        selection_policies=['moderate-45-40-15-v1'])],schema=g.LINK_SCHEMA)
    artifacts={}
    for name,table in [('google_places.parquet',places),('source_links.parquet',links)]:
        p=release/'silver'/name;pq.write_table(table,p)
        artifacts['silver/'+name]=dict(sha256=g.sha(p),rows=table.num_rows,logical_sha256=_digest(table.to_pylist()))
    manifest=dict(signature=dict(inputs_sha256={str(ov.resolve()):g.sha(ov)}),artifacts=artifacts)
    (release/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    (root/'current.json').write_text(json.dumps(dict(release_id='a'*24,manifest_sha256=g.sha(release/'manifest.json'))),encoding='utf-8')
    return ov,root


def test_historical_flags_do_not_become_current_component_conclusions(inputs,tmp_path):
    release,summary=f.publish(*inputs,tmp_path/'features')
    pairs=pq.ParquetFile(release/'comparison/address_pair_evidence.parquet').read().to_pylist()
    row=pairs[0]
    assert row['historical_m1_dq_flags']==['STREET_CONFLICT']
    assert row['street_component_relation']=='SAME_LITERAL'
    assert row['house_component_relation']=='RANGE_CONTAINS_LITERAL'
    assert row['historical_source_observation_refs']==['raw.sqlite3#dispatch:1:attempt:0']
    assert row['historical_flag_usage']=='AUDIT_ONLY_NOT_CURRENT_ER_CONCLUSIONS'
    evidence=json.loads(row['address_evidence_json'])
    assert evidence['overture']['address_raw']=='158-160, Hoàng Diệu'
    assert evidence['google']['address_raw']=='160 Hoàng Diệu'
    assert evidence['source_refs']['overture']['parquet_sha256']==g.sha(inputs[0])
    assert 'er_status' not in row and 'score' not in row and 'decision' not in row
    assert summary['pair_rows']==1


def test_deterministic_rerun_preserves_published_m1_and_overture(inputs,tmp_path):
    ov,root=inputs
    protected={p:g.sha(p) for p in root.rglob('*') if p.is_file()};protected[ov]=g.sha(ov)
    output=tmp_path/'features'
    a,one=f.publish(ov,root,output)
    hashes={p.name:g.sha(p) for p in a.rglob('*') if p.is_file()}
    pointer=(output/'current.json').read_bytes()
    b,two=f.publish(ov,root,output)
    assert a==b and one==two and pointer==(output/'current.json').read_bytes()
    assert hashes=={p.name:g.sha(p) for p in b.rglob('*') if p.is_file()}
    assert all(g.sha(p)==digest for p,digest in protected.items())


def test_wrong_pinned_overture_input_fails_before_publication(inputs,tmp_path):
    ov,root=inputs
    pq.write_table(pa.table({'source_id':['different']}),ov)
    with pytest.raises(ValueError,match='pinned'):
        f.publish(ov,root,tmp_path/'features')
    assert not (tmp_path/'features/current.json').exists()


def test_failed_stage_never_exposes_partial_comparison_output(inputs,tmp_path):
    def fail(stage):
        if stage=='after_stage_validation':raise RuntimeError('injected')
    output=tmp_path/'features'
    with pytest.raises(RuntimeError):f.publish(*inputs,output,fault=fail)
    assert not (output/'current.json').exists()
    assert not (output/'.publish.lock').exists()
