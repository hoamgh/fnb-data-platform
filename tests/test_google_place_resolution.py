"""Offline source-address contract for the canonical-place resolver."""
from dataclasses import asdict

import pytest

from crawl_experiment.core.errors import PlaceResolutionError
from crawl_experiment.core.place_catalog import CanonicalPlace
from crawl_experiment.sources.google_maps import place_resolution as resolution


@pytest.mark.parametrize('address,expected_status', [
    ('12 Main Street', 'RESOLVED'),
    ('112 Main Street', 'AMBIGUOUS'),
    (None, 'AMBIGUOUS'),
    ('', 'AMBIGUOUS'),
])
def test_resolver_forwards_address_without_bypassing_branch_checks(monkeypatch, address, expected_status):
    place = CanonicalPlace(
        place_key='overture:source-1', source='overture', source_place_id='source-1',
        name='Branch Cafe', lat=10.0, lng=106.0, address=address,
        primary_category='cafe', categories=['cafe'], category_hierarchy=['food_and_drink'],
        product_category_group='food_and_drink', category_source='overture', confidence=0.9,
        admin_area_id='area', admin_area_name='City', first_seen_at='2026-11-01', last_seen_at='2026-11-04')
    original = asdict(place)
    observed = []
    base_navigator = resolution.GoogleMapsNavigator

    class Navigator(base_navigator):
        def open_store(self, store):
            observed.append(store)
            # Exercise the existing branch check with a fixed offline Google candidate.
            candidate = {'text': 'Branch Cafe', 'address': '12 Main Street'}
            if self._best_exact_candidate(store, [candidate]) is None:
                raise PlaceResolutionError('branch identity unresolved')
            self.last_resolution_diagnostics = {
                'resolved_place_title': 'Branch Cafe', 'resolved_place_address': '12 Main Street',
                'resolved_url': 'https://www.google.com/maps/place/Cafe/data=!1sChIJdemo!3d10!4d106',
                'place_entity_confirmed': True,
            }

    class Driver:
        def execute_script(self, script):
            return ''

    monkeypatch.setattr(resolution, 'GoogleMapsNavigator', Navigator)
    result = resolution.resolve_place(place, Driver())
    assert observed[0].expected_address == address
    assert observed[0].id == resolution.store_id(place)
    assert observed[0].query == ' '.join(filter(None, [place.name, address, place.admin_area_name]))
    assert result['status'] == expected_status
    assert result['place_key'] == place.place_key
    assert asdict(place) == original
    if expected_status == 'RESOLVED':
        assert result['google_place_id'] == 'ChIJdemo'
        assert result['coordinate_distance_m'] == 0
        assert result['error'] is None
    else:
        assert result['error'] == 'branch identity unresolved'
        assert result['google_place_id'] is None
