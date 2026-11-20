import json
import pyarrow as pa
from fnb_platform.integration import catalog_scope as scope


def fixture(tmp_path):
    boundary=tmp_path/'boundary.geojson'
    boundary.write_text(json.dumps(dict(type='Feature',geometry=dict(type='Polygon',coordinates=[[[0,0],[1,0],[1,1],[0,1],[0,0]]]))))
    rows=[];places=[]
    for name,x,status in [('inside',.5,'AUTO_ACCEPTED'),('edge',0.,'AUTO_ACCEPTED'),
            ('outside',2.,'AUTO_ACCEPTED'),('shared',.4,'CONFLICT')]:
        rows.append(dict(overture_id=name,google_id_kind='data_id',google_identity_value=name,
            effective_status=status,final_decision='MATCH' if status=='AUTO_ACCEPTED' else 'REVIEW_REQUIRED',
            evidence_json='{}',evidence_hash='original',reason='original',reason_codes=[],
            activation_evidence_hash='original',decision_source='AUTO_POLICY',human_label=None))
        places.append(dict(google_id_kind='data_id',google_identity_value=name,latitude=x,longitude=.5))
    table=pa.Table.from_pylist(rows)
    table=table.cast(table.schema.set(table.schema.get_field_index('reason_codes'),
        pa.field('reason_codes',pa.list_(pa.string()))))
    return table,places,boundary


def test_scope_holds_outside_without_changing_identity_or_human_labels(tmp_path):
    table,places,boundary=fixture(tmp_path)
    result={r['overture_id']:r for r in scope.apply(table,places,boundary).to_pylist()}
    assert result['inside']==table.to_pylist()[0] and result['edge']==table.to_pylist()[1]
    assert result['outside']['effective_status']=='PENDING' and result['outside']['final_decision']=='REVIEW_REQUIRED'
    assert result['outside']['human_label'] is None
    assert json.loads(result['outside']['evidence_json'])['catalog_scope']['identity_decision_unchanged']
    assert result['shared']['effective_status']=='CONFLICT'
    assert table.to_pylist()[2]['effective_status']=='AUTO_ACCEPTED'


def test_scope_missing_coordinates_cannot_attach(tmp_path):
    table,places,boundary=fixture(tmp_path);places[0]['latitude']=None
    row=scope.apply(table,places,boundary).to_pylist()[0]
    assert row['effective_status']=='PENDING' and 'CATALOG_SCOPE_OUTSIDE_OR_MISSING' in row['reason_codes']
