import base64
import struct
import json
import sqlite3
from contextlib import closing

import pytest

from scripts.google_live_workers import create_queue, json_ready_place
from scripts.google_overture_workers import write_json


def test_binary_parquet_place_survives_queue_and_plan(tmp_path):
    original = {
        'place_key': 'overture:test',
        'name': 'Quán ăn',
        'geometry': struct.pack('<BIdd', 1, 1, 106.7, 10.76),
        'source_id': 'original-source-id',
        'lat': 10.76,
        'lon': 106.7,
        'sources': [{'dataset': 'Overture', 'record_id': 'original-source-id'}],
        'category_hierarchy': ['food_and_drink'],
    }
    place = json_ready_place(original)
    expected_geometry = {'__type__': 'binary', 'encoding': 'base64',
                         'data': base64.b64encode(original['geometry']).decode('ascii')}
    assert place['geometry'] == expected_geometry
    assert base64.b64decode(place['geometry']['data']) == original['geometry']

    queue = tmp_path / 'dispatch.sqlite3'
    create_queue(queue, [original])
    with closing(sqlite3.connect(queue)) as db:
        payload = json.loads(db.execute('SELECT payload FROM dispatch').fetchone()[0])
    assert payload == place
    assert original['geometry'] == struct.pack('<BIdd', 1, 1, 106.7, 10.76)
    for key in ('place_key', 'source_id', 'lat', 'lon', 'sources'):
        assert payload[key] == original[key]

    plan = tmp_path / 'plan.json'
    write_json(plan, {'places': [place]})
    assert json.loads(plan.read_text(encoding='utf-8'))['places'] == [place]


def test_unrecognized_binary_adjacent_type_is_not_silently_discarded():
    with pytest.raises(TypeError, match='not JSON serializable'):
        json_ready_place({'place_key': 'overture:test', 'unsupported': object()})
