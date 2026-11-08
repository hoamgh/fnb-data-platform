from copy import deepcopy

from shapely.geometry import Point, Polygon

from experiments.address_reference_pilot.run import extract, make_registry, street_key


def fixture():
    polygon=Polygon([(106.69,10.75),(106.71,10.75),(106.71,10.77),(106.69,10.77)])
    def way(i,name,lat,nodes):
        return dict(type='way',id=i,nodes=nodes,timestamp='2026-11-01T00:00:00Z',
            tags={'name':name},geometry=[{'lon':106.699,'lat':lat},{'lon':106.701,'lat':lat}])
    raw={'elements':[way(1,'Đường Số 41',10.76,[1,2]),way(2,'Tôn Đản',10.761,[3,4]),
                     way(3,'Hoàng Diệu',10.762,[5,6])]}
    registry,_=make_registry(raw,polygon)
    return polygon,raw,registry,{'admin_area_id':'test-ward','admin_area_name':'Phường Khánh Hội, Thành phố Hồ Chí Minh'}


def test_numbered_street_is_not_house_or_alley_number():
    p,_,r,a=fixture()
    good=extract('79 Đường Số 41',r,Point(106.70,10.76),p,a)
    assert good['street_name']=='Đường Số 41' and good['house']=='79'
    for text in ['41 Tôn Đản','Hẻm 41 Tôn Đản','Số 41 Tôn Đản']:
        out=extract(text,r,Point(106.70,10.761),p,a)
        assert not any(c['name_key']=='so 41' for c in out['candidate_evidence'])


def test_whole_tokens_not_substrings_and_no_nearest_road_fallback():
    p,_,r,a=fixture()
    assert extract('84 Tôn Đảnxyz',r,Point(106.70,10.761),p,a)['street_id'] is None
    assert extract('84 Unknown Road',r,Point(106.70,10.761),p,a)['street_id'] is None


def test_multiple_named_streets_never_just_pick_nearest():
    p,_,r,a=fixture()
    out=extract('Tôn Đản/84 Hoàng Diệu',r,Point(106.70,10.761),p,a)
    assert out['street_id'] is None and out['status']=='AMBIGUOUS_MULTIPLE_STREETS'


def test_geographic_disagreement_not_overridden_by_exact_name():
    p,_,r,a=fixture()
    out=extract('79 Đường Số 41',r,Point(106.70,10.769),p,a)
    assert out['street_id'] is None and out['status']=='LEXICAL_CANDIDATE_GEOMETRY_DISAGREES'


def test_missing_and_outside_coordinates_do_not_infer_ward():
    p,_,r,a=fixture()
    for point in [None,Point(107,11)]:
        out=extract('79 Đường Số 41',r,point,p,a)
        assert out['admin_area_id'] is None and out['district_id'] is None


def test_parent_road_alias_not_ward_suffix():
    p,raw,_,a=fixture()
    raw['elements'][0]['tags']['name']='Khánh Hội'
    r,_=make_registry(raw,p)
    out=extract('84 Khánh Hội, Khánh Hội, Hồ Chí Minh, Việt Nam',r,Point(106.70,10.76),p,a)
    assert out['street_name']=='Khánh Hội'
    assert len({c['position'] for c in out['candidate_evidence']})==1


def test_reference_version_not_old_name_effective_date():
    p,raw,_,a=fixture();raw['elements'][0]['tags']['old_name']='Đường Cũ'
    r,_=make_registry(raw,p)
    out=extract('84 Đường Cũ',r,Point(106.70,10.76),p,a)
    assert out['street_id'] is None
    assert out['candidate_evidence'] and all(c['alias_kind']=='old_name' for c in out['candidate_evidence'])
    assert r[0]['valid_from'] is None


def test_same_name_disconnected_roads_are_distinct():
    p,raw,_,_=fixture();raw['elements'][1]['tags']['name']='Đường Số 41'
    r,_=make_registry(raw,p)
    assert sum(row['name_key']=='so 41' for row in r)==2


def test_registry_deterministic_when_source_order_reversed():
    p,raw,_,_=fixture()
    extra=deepcopy(raw['elements'][0]);extra['id']=4;extra['nodes']=[2,7]
    raw['elements'].append(extra)
    a,_=make_registry(raw,p);raw['elements'].reverse();b,_=make_registry(raw,p)
    assert a==b


def test_structured_values_preserve_suffix_paths_ranges_and_lots():
    p,_,r,a=fixture()
    for literal in ['122','122a','122/14']:
        out=extract(literal+' Tôn Đản',r,Point(106.70,10.761),p,a)
        assert out['house']==literal
    out=extract('158-160 Hoàng Diệu',r,Point(106.70,10.762),p,a)
    assert out['house_range']=='158-160'
    assert extract('46 Lô I, Tôn Đản',r,Point(106.70,10.761),p,a)['lot']==['i']
    assert street_key('Đường 41')==street_key('Đ. Số 41')
