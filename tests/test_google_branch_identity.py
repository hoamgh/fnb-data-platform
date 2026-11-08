import pytest

from crawl_experiment.core.models import Store
from crawl_experiment.sources.google_maps.navigator import GoogleMapsNavigator
from crawl_experiment.sources.google_maps.health import PageEvidence


@pytest.mark.parametrize('expected,actual,accepted', [
    ('12 Main Street', '112 Main Street', False),
    ('12 Main Street', '14 Main Street', False),
    ('12/3 Main Street', '12/4 Main Street', False),
    ('Khanh Hoi, Ho Chi Minh City', 'Khanh Hoi, Ho Chi Minh City', False),
    ('12 Main Street', '12 Main Street, City', True),
    ('12 Main Street', '12 Main Street West', False),
    ('', '', False),
])
def test_branch_address(expected, actual, accepted):
    nav = GoogleMapsNavigator(None)
    assert nav._address_matches(expected, actual) is accepted


def test_same_brand_candidates_require_unique_branch_evidence():
    nav = GoogleMapsNavigator(None)
    store = Store('source-id', 'Brand', 'query', expected_address='12 Main Street')
    wrong = dict(text='Brand', address='112 Main Street')
    right = dict(text='Brand', address='12 Main Street')
    assert nav._best_exact_candidate(store, [wrong]) is None
    assert nav._best_exact_candidate(store, [wrong, right]) is right
    assert nav._best_exact_candidate(store, [right, dict(right)]) is None
    assert nav._best_exact_candidate(store, [dict(right, text='Other')]) is None
    assert nav._best_exact_candidate(store, [dict(right, address=None)]) is None
    assert nav._best_exact_candidate(Store('id', 'Brand', 'query'), [right, wrong]) is None


def test_resolved_entity_requires_branch_address(monkeypatch):
    class Driver:
        def find_elements(self, *args):
            return [object()]
    nav = GoogleMapsNavigator(Driver())
    monkeypatch.setattr(nav, '_place_title', lambda: 'Brand')
    monkeypatch.setattr(nav, '_place_address', lambda: '112 Main Street')
    evidence = PageEvidence(has_place_shell=True)
    for expected in ('12 Main Street',):
        assert nav._resolved_place(Store('id', 'Brand', 'query', expected_address=expected),
                                   'https://google.com/maps/place/Brand', evidence, []) is None


@pytest.mark.parametrize('source,google,address,google_address', [
    ('Cui Express', 'CUI EXPRESS - Pizza , Burger & GreatFood', '237/42/5 Hoàng Diệu', '237/42/5 Hoàng Diệu, Khánh Hội'),
    ('Chuối Cười', 'Chuối Cười - Chuối chiên giòn SẠCH', '196D Xóm Chiếu', '196D Xóm Chiếu, Khánh Hội'),
    ('Highlands Coffee', 'Highlands Coffee Ben Van Don HCM', '182 Bến Vân Đồn', '182 Bến Vân Đồn, Khánh Hội'),
    ('Hồng Trà Ngô Gia', 'Hồng Trà Ngô Gia H103', '141 Tôn Đản', '141 Tôn Đản, Khánh Hội'),
    ('Katinat Saigon Kafe', 'Katinat', '195 Bến Vân Đồn', '195 Bến Vân Đồn, Khánh Hội'),
    ('Pizza Hong Kong', 'PIZZA HONGKONG', '301B Đ. Tôn Đản', '301B Tôn Đản, Khánh Hội'),
    ('Trung Nguyên E-Coffee (12 Tôn Đản)', 'Trung Nguyên E-Coffee', '12 Tôn Đản - Phường 13 Quận 4, Hồ Chí Minh 70000', '12 Tôn Đản, Xóm Chiếu'),
    ('Highlands Coffee', 'Highlands Coffe', '182 Bến Vân Đồn', '182 Ben Van Don'),
])
def test_observed_name_variants_require_matching_branch_address(monkeypatch, source, google, address, google_address):
    class Driver:
        def find_elements(self, *args): return [object()]
    nav = GoogleMapsNavigator(Driver())
    store = Store('unchanged-source-id', source, 'unchanged-query', expected_address=address)
    candidate = dict(text=google, address=google_address)
    assert nav._best_exact_candidate(store, [candidate]) is candidate
    monkeypatch.setattr(nav, '_place_title', lambda: google)
    monkeypatch.setattr(nav, '_place_address', lambda: google_address)
    result = nav._resolved_place(store, 'https://www.google.com/maps/place/Cafe', PageEvidence(has_place_shell=True), [candidate])
    assert result['place_entity_confirmed'] is True
    assert result['identity_address_match'] is True
    assert result['identity_name_match'] in {'EXACT', 'SPACING_VARIANT', 'NAME_WITH_QUALIFIER', 'HIGH_SIMILARITY'}
    assert store.id == 'unchanged-source-id' and store.query == 'unchanged-query'
    assert nav._best_exact_candidate(store, [candidate, dict(candidate)]) is None
    assert nav._resolved_place(store, 'https://www.google.com/maps/place/Cafe', PageEvidence(has_place_shell=True), [candidate, dict(candidate)]) is None


@pytest.mark.parametrize('source,google,address,google_address', [
    ('Highlands Coffee', 'Highlands Coffee Ben Van Don HCM', '12 Main Street', '112 Main Street'),
    ('152A Cafe', '152A Cafe', '152A Tôn Đản', '152B Tôn Đản'),
    ('Ốc Loan', 'Ốc Oanh', '129 Đ. Vĩnh Khánh', '129 Vĩnh Khánh'),
    ('Cafe', 'Cafe Other', '12 Main Street', '12 Main Street'),
    ('Quán Ăn', 'Quán Ăn Dì Nhàn', '12 Main Street', '12 Main Street'),
    ('Highlands Coffee', 'Other Business', '182 Bến Vân Đồn', '182 Bến Vân Đồn'),
    ('Highlands Coffee', 'Highlands Coffee', '182 Bến Vân Đồn', '182 Hoàng Diệu'),
    ('Cafe Branch 12', 'Cafe Branch 112', '12 Main Street', '12 Main Street'),
])
def test_near_name_or_address_alone_cannot_override_branch_conflict(monkeypatch, source, google, address, google_address):
    class Driver:
        def find_elements(self, *args): return [object()]
    nav = GoogleMapsNavigator(Driver())
    store = Store('id', source, 'query', expected_address=address)
    candidate = dict(text=google, address=google_address)
    assert nav._best_exact_candidate(store, [candidate]) is None
    monkeypatch.setattr(nav, '_place_title', lambda: google)
    monkeypatch.setattr(nav, '_place_address', lambda: google_address)
    assert nav._resolved_place(store, 'https://www.google.com/maps/place/Cafe', PageEvidence(has_place_shell=True), [candidate]) is None
