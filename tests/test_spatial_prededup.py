"""Small offline spatial/DQ contracts, independent of real source files."""
import math

import pytest
from shapely.geometry import box

from experiments.overture_gold_contract import spatial_prededup as p


def row(key, meters=0, **values):
    return dict(source_id=key, longitude=math.degrees(meters/p.EARTH_M), latitude=0,
                name='Highlands Coffee', address='12 Example Street',
                taxonomy_primary='cafe', taxonomy_hierarchy=['food_and_drink', 'cafe'],
                **values)


def test_inclusive_distance_boundary_and_configurable_radius():
    rows = [row('a'), row('b', 49.99), row('c', 50), row('d', 50.01)]
    pairs, _ = p.candidate_pairs(rows)
    assert {r['right_id'] for r in pairs if r['left_id'] == 'a'} == {'b', 'c'}
    assert not p.candidate_pairs(rows[:2], 49)[0]


def test_unique_unordered_pairs_no_self_pairs():
    pairs, count = p.candidate_pairs([row('c'), row('b'), row('a')])
    assert [(r['left_id'], r['right_id']) for r in pairs] == [('a', 'b'), ('a', 'c'), ('b', 'c')]
    assert count == 3


@pytest.mark.parametrize('coordinates', [(None, 0), (181, 0), (0, 91), (float('nan'), 0), (0, float('inf'))])
def test_invalid_coordinates_preserved_but_not_indexed(coordinates):
    bad = row('bad'); bad.update(longitude=coordinates[0], latitude=coordinates[1])
    rows = p.annotate([row('a'), bad], {}, box(-1, -1, 1, 1), 'hash')
    assert len(rows) == 2
    assert 'INVALID_COORDINATES' in rows[1]['prededup_dq_reasons']
    assert rows[1]['ward_inferred'] is None
    assert p.candidate_pairs(rows)[0] == []


def test_same_brand_different_branch_house_numbers_not_likely_duplicate():
    a, b = row('a'), row('b', 10)
    b['address'] = '14 Example Street'
    pairs, _ = p.candidate_pairs([a, b])
    assert len(pairs) == 1 and not pairs[0]['likely_duplicate']
    assert pairs[0]['reason'] == 'HOUSE_NUMBER_CONFLICT'


def test_geographic_inference_preserves_originals_and_dq():
    a, b = row('a'), row('b')
    b.update(name='', taxonomy_hierarchy=[])
    rows = p.annotate([a, b], {'a': {'addresses': [{'locality': 'Quận 4', 'ward': 'Original Ward', 'city': 'Original City'}]}},
                      box(-1, -1, 1, 1), 'hash')
    for original, result in zip([a, b], rows):
        assert all(result[k] == v for k, v in original.items())
    assert rows[0]['ward_original'] == 'Original Ward' and rows[0]['ward_inferred'] is None
    assert rows[1]['ward_inferred'] == 'Phường Khánh Hội'
    assert 'MISSING_NAME' in rows[1]['prededup_dq_reasons']
    assert 'REVIEW_MISSING_TAXONOMY' in rows[1]['prededup_dq_reasons']


def test_missing_and_duplicate_ids_are_flagged_not_paired():
    rows = p.annotate([row('same'), row('same'), row(None)], {}, box(-1, -1, 1, 1), 'hash')
    assert len(rows) == 3 and p.candidate_pairs(rows)[0] == []
    assert rows[0]['prededup_dq_reasons'] == ['DUPLICATE_ID']
    assert 'MISSING_ID' in rows[2]['prededup_dq_reasons']


def test_missing_address_is_unknown_not_similarity_zero():
    a, b = row('a'), row('b')
    b['address'] = None
    pair = p.candidate_pairs([a, b])[0][0]
    assert pair['address_similarity'] is None and not pair['likely_duplicate']


def test_sparse_points_are_not_all_compared():
    _, count = p.candidate_pairs([row(str(i), i*1000) for i in range(100)])
    assert count == 0


@pytest.mark.parametrize('radius', [0, -1, float('nan'), float('inf')])
def test_invalid_radius(radius):
    with pytest.raises(ValueError): p.candidate_pairs([], radius)


def test_real_name_order_regression():
    # Actual names from the saved 725-POI dataset; directional ratios differ.
    a, b = 'Bánh Chưng Nam Định', 'Quán Ăn Món Huế'
    assert p.similarity(a, b) != p.similarity(b, a)
    expected = (p.similarity(a, b) + p.similarity(b, a)) / 2
    assert p.name_similarity(a, b) == p.name_similarity(b, a) == expected


def test_real_pair_id_order_does_not_change_name_score():
    a, b = row('a'), row('b', 10)
    a.update(name='Bánh Chưng Nam Định', address='198/201 Hẻm 198 Đường Đoàn Văn Bơ')
    b.update(name='Quán Ăn Món Huế', address='232/16B Đường Đoàn Văn Bơ')
    first = p.candidate_pairs([a, b])[0][0]
    a['source_id'], b['source_id'] = b['source_id'], a['source_id']
    reversed_pair = p.candidate_pairs([b, a])[0][0]
    assert first['name_similarity'] == reversed_pair['name_similarity']


def test_symmetric_name_missing_value_stays_unknown():
    assert p.name_similarity(None, 'Quán Ăn Món Huế') is None
    assert p.name_similarity('Quán Ăn Món Huế', None) is None
