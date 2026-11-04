"""Offline, localized place-pane snapshots and failure-path persistence."""

import json

import pytest
from bs4 import BeautifulSoup

from crawl_experiment.core.models import Store
from crawl_experiment.core.errors import ReviewSurfaceError
from crawl_experiment.observability.run_artifacts import CrawlArtifacts, build_summary
from crawl_experiment.sources.google_maps import crawler as crawler_module
from crawl_experiment.sources.google_maps.place_details import extract_place_details


class _Element:
    def __init__(self, node):
        self.node = node

    @property
    def text(self):
        return self.node.get_text(" ", strip=True)

    def get_attribute(self, key):
        return self.node.get(key)

    def find_elements(self, by, selector):
        assert by == "css selector"
        return [_Element(node) for node in self.node.select(selector)]


class _Driver:
    current_url = "https://www.google.com/maps/place/Quan+An"

    def __init__(self, html):
        self.soup = BeautifulSoup(html, "html.parser")

    def find_elements(self, by, selector):
        assert by == "css selector"
        return [_Element(node) for node in self.soup.select(selector)]


def test_vietnamese_storefront_fields_and_explicit_missing():
    driver = _Driver("""
        <div role="main">
            <h1 class="DUwDvf">Quán Ăn Bình Minh</h1>
            <button class="DkEaL">Quán ăn Việt Nam</button>
            <div class="F7nice"><span aria-hidden="true">4,6</span>
                <span aria-hidden="true">(1.234)</span></div>
            <button data-item-id="address">12 Tôn Đản, Khánh Hội, TP.HCM</button>
            <button data-item-id="phone:tel:+842812345678" aria-label="Sao chép số điện thoại"></button>
            <a data-item-id="authority" href="https://example.com/quan-an">Website</a>
            <button data-item-id="oh" aria-label="Giờ mở cửa: 8:00–22:00">Đang mở cửa</button>
            <table class="eK4R0e"><tr><td>Thứ Hai</td><td>08:00–22:00</td></tr></table>
            <button data-item-id="oloc">ABCD+12, Quận 4</button>
            <span aria-label="Giá mỗi người: 50.000–100.000 ₫"></span>
        </div>
    """)

    details = extract_place_details(driver, "overture-123")
    assert details["store_id"] == "overture-123"
    assert details["source"] == "google_maps_dom"
    assert details["name"] == "Quán Ăn Bình Minh"
    assert details["address"].startswith("12 Tôn Đản")
    assert details["category"] == "Quán ăn Việt Nam"
    assert details["rating"] == 4.6
    assert details["review_count"] == 1234
    assert details["phone"] == "+842812345678"
    assert details["website"] == "https://example.com/quan-an"
    assert details["opening_hours"]["weekly"] == [{"day": "Thứ Hai", "hours": "08:00–22:00"}]
    assert details["price_range"].startswith("Giá mỗi người")
    assert details["plus_code"] == "ABCD+12, Quận 4"
    assert any(item["item_id"] == "oloc" for item in details["visible_detail_items"])
    assert details["menu_url"] is None
    assert "menu_url" in details["missing_fields"]
    assert "collected_at" in details


def test_missing_fields_stay_null_and_no_review_ui_is_scraped():
    driver = _Driver("""
        <div role="main"><h1 class="DUwDvf">Một quán khác</h1>
            <button aria-label="Viết bài đánh giá">Viết bài đánh giá</button>
            <div data-review-id="1"><span role="img" aria-label="5 sao"></span></div>
            <a data-item-id="authority" href="javascript:alert(1)">Website</a>
        </div>
    """)
    details = extract_place_details(driver, "other")
    assert details["name"] == "Một quán khác"
    assert details["rating"] is None
    assert details["review_count"] is None
    assert details["website"] is None
    assert details["opening_hours"] is None
    assert details["missing_fields"]


def test_english_review_total_and_rating_are_separate():
    driver = _Driver("""
        <div role="main"><h1 class="DUwDvf">Coffee Shop</h1>
            <div class="F7nice"><span aria-hidden="true">4.8</span></div>
            <button aria-label="1,567 reviews">1,567 reviews</button>
            <a data-item-id="menu" href="https://example.com/menu">Menu</a>
        </div>
    """)
    details = extract_place_details(driver, "cafe-123")
    assert details["rating"] == 4.8
    assert details["review_count"] == 1567
    assert details["menu_url"] == "https://example.com/menu"
    missing_rating = extract_place_details(_Driver("""
        <div role="main"><div class="F7nice"><span aria-hidden="true">(5)</span></div></div>
    """), "unknown-rating")
    assert missing_rating["rating"] is None
    assert missing_rating["review_count"] == 5


def test_snapshot_survives_review_surface_failure(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    (run_dir / "metadata").mkdir(parents=True)
    (run_dir / "checkpoints").mkdir()
    store = Store("store-01", "Quán", "Quán, Khánh Hội")
    artifacts = CrawlArtifacts(run_dir, "2026-11-03T14:00:00+00:00", stores=[store],
                               state_database=run_dir / "missing.sqlite3")

    class Navigator:
        def __init__(self, driver):
            self.last_resolution_diagnostics = {"resolved_url": driver.current_url, "place_entity_confirmed": True}

        def open_store(self, store):
            return "https://www.google.com/maps/place/Quan+An"

    def fail_reviews(self):
        raise RuntimeError("sort/reviews interface unavailable")

    monkeypatch.setattr(crawler_module, "GoogleMapsNavigator", Navigator)
    monkeypatch.setattr(crawler_module.ReviewSurface, "prepare_and_classify", fail_reviews)
    crawler = crawler_module.GoogleMapsCrawler(
        object(), object(), activity_observer=artifacts.on_activity,
        timestamp_enrichment=False,
    )
    monkeypatch.setattr(crawler, "_checkpoint", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="interface unavailable"):
        crawler.crawl(store, _Driver("<div role='main'><h1 class='DUwDvf'>Quán</h1></div>"), warm_up=False)

    snapshot_path = run_dir / "metadata" / "store-01.place_details.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["name"] == "Quán"
    metadata = json.loads((run_dir / "metadata" / "store-01.json").read_text(encoding="utf-8"))
    assert metadata["google_place_details_path"] == str(snapshot_path)
    assert metadata["google_place_details_field_count"] == 1
    summary = build_summary(run_dir, started_at="2026-11-03T14:00:00+00:00",
                            finished_at="2026-11-03T14:01:00+00:00", stores=[store],
                            timeout_seconds=120, max_scrolls=5)
    assert summary["stores"][0]["google_place_details_path"] == str(snapshot_path)


@pytest.mark.parametrize('evidence,expected', [
    ({'open_error': 'Reviews tab not found', 'review_tabs': 0, 'cards': 0,
      'auth_dialog_evidence': [], 'limited_evidence': []}, True),
    ({'open_error': 'Reviews tab not found', 'review_tabs': 0, 'cards': 0,
      'auth_dialog_evidence': ['Đăng nhập để xem'], 'limited_evidence': []}, False),
])
def test_missing_review_tab_is_only_local_without_access_restriction(monkeypatch, evidence, expected):
    class Navigator:
        def __init__(self, driver):
            self.last_resolution_diagnostics = {}

        def open_store(self, store):
            return 'https://www.google.com/maps/place/Quan+An'

    monkeypatch.setattr(crawler_module, 'GoogleMapsNavigator', Navigator)
    monkeypatch.setattr(crawler_module.ReviewSurface, 'prepare_and_classify',
                        lambda self: (crawler_module.ReviewSurfaceState.UNKNOWN, None, evidence))
    crawler = crawler_module.GoogleMapsCrawler(object(), object(), timestamp_enrichment=False)
    monkeypatch.setattr(crawler, '_checkpoint', lambda *args, **kwargs: None)
    with pytest.raises(ReviewSurfaceError) as captured:
        crawler.crawl(Store('store-01', 'Quán', 'Khánh Hội'),
                      _Driver('<div role="main"><h1>Quán</h1></div>'), warm_up=False)
    assert getattr(captured.value, 'local_missing_review_tab', False) is expected


@pytest.mark.parametrize('restriction,expected', [(None, True), ('auth_dialog_evidence', False), ('limited_evidence', False)])
def test_intercepted_review_tab_is_local_only_without_access_restriction(monkeypatch, restriction, expected):
    evidence = {"open_error_code": "REVIEW_TAB_CLICK_INTERCEPTED",
                "open_error": "element click intercepted: search combobox",
                "auth_dialog_evidence": [], "limited_evidence": []}
    if restriction:
        evidence[restriction] = ["access restriction"]
    class Navigator:
        def __init__(self, driver):
            self.last_resolution_diagnostics = {}

        def open_store(self, store):
            return 'https://www.google.com/maps/place/Quan+An'

    monkeypatch.setattr(crawler_module, 'GoogleMapsNavigator', Navigator)
    monkeypatch.setattr(crawler_module.ReviewSurface, 'prepare_and_classify',
                        lambda self: (crawler_module.ReviewSurfaceState.UNKNOWN, None, evidence))
    crawler = crawler_module.GoogleMapsCrawler(object(), object(), timestamp_enrichment=False)
    monkeypatch.setattr(crawler, '_checkpoint', lambda *args, **kwargs: None)
    with pytest.raises(ReviewSurfaceError) as captured:
        crawler.crawl(Store('store-01', 'Quán', 'Khánh Hội'),
                      _Driver('<div role="main"><h1>Quán</h1></div>'), warm_up=False)
    assert getattr(captured.value, 'local_review_tab_click_intercepted', False) is expected


@pytest.mark.parametrize('labels,status,open_now', [
    (['Đang mở cửa'], 'OPERATIONAL', True),
    (['Closed'], 'UNKNOWN', False),
    (['Đang đóng cửa'], 'UNKNOWN', False),
    (['Closed ⋅ Opens 9 AM'], 'OPERATIONAL', False),
    (['Đã đóng cửa ⋅ Mở cửa lúc 09:00'], 'OPERATIONAL', False),
    (['Temporarily closed'], 'CLOSED_TEMPORARILY', False),
    (['Tạm thời đóng cửa'], 'CLOSED_TEMPORARILY', False),
    (['Permanently closed'], 'CLOSED_PERMANENTLY', False),
    (['Đã đóng cửa vĩnh viễn'], 'CLOSED_PERMANENTLY', False),
    (['Open', 'Closed'], 'UNKNOWN', None),
    (['Permanently closed', 'Temporarily closed'], 'UNKNOWN', None),
    (['Permanently closed', 'Open'], 'UNKNOWN', None),
    ([], 'UNKNOWN', None),
])
def test_schema_v2_business_status_is_not_hours_closure(labels, status, open_now):
    driver = _Driver('<div role="main"><h1 class="DUwDvf">Cafe</h1>' +
                     ''.join(f'<span>{label}</span>' for label in labels) + '</div>')
    details = extract_place_details(driver, 'same-source-id')
    assert details['schema_version'] == 2
    assert details['business_status'] == status
    assert details['open_now'] is open_now
    assert details['raw_status_labels'] == labels
    assert details['observed_at'] == details['collected_at']
    assert details['source_url'] == details['maps_url']
    if status in {'CLOSED_PERMANENTLY', 'CLOSED_TEMPORARILY', 'UNKNOWN'}:
        assert details['next_opening_time'] is None


def test_v2_status_ignores_review_and_search_result_text():
    driver = _Driver('''<div role="main"><h1 class="DUwDvf">Cafe</h1>
        <span>Open</span>
        <div data-review-id="1"><span>Permanently closed</span></div>
        <div role="feed"><span>Temporarily closed</span></div></div>''')
    details = extract_place_details(driver, 'id')
    assert details['business_status'] == 'OPERATIONAL'
    assert details['raw_status_labels'] == ['Open']


def test_weekly_intervals_preserve_split_overnight_closed_and_unknown_days():
    values = [('Thứ Hai', '08:00–12:00, 13:00–22:00'), ('Thứ Ba', '18:00–02:00'),
              ('Thứ Tư', 'Mở cửa 24 giờ'), ('Thứ Năm', 'Đóng cửa'),
              ('Thứ Sáu', '9–5'), ('Thứ Bảy', '9 AM–5 PM')]
    html = '<div role="main"><button data-item-id="oh">Giờ mở cửa</button><span>Holiday hours may differ</span></div>'
    html += '<table class="eK4R0e">' + ''.join(f'<tr><td>{d}</td><td>{h}</td></tr>' for d, h in values) + '</table>'
    result = extract_place_details(_Driver(html), 'id')
    rows = result['weekly_opening_hours']
    assert rows[0]['intervals'] == [dict(opens='08:00', closes='12:00', closes_day_offset=0),
                                   dict(opens='13:00', closes='22:00', closes_day_offset=0)]
    assert rows[1]['intervals'] == [dict(opens='18:00', closes='02:00', closes_day_offset=1)]
    assert rows[2]['status'] == 'OPEN_24_HOURS'
    assert rows[3]['status'] == 'CLOSED' and rows[3]['intervals'] == []
    assert rows[4]['status'] == 'UNKNOWN' and rows[4]['intervals'] is None
    assert rows[4]['raw_hours'] == '9–5'
    assert rows[5]['intervals'][0]['closes'] == '17:00'
    assert len(rows) == 6  # No invented Sunday.
    assert result['metadata_completeness']['weekly_hours_complete'] is False
    assert result['special_hours_warnings'] == ['Holiday hours may differ']
    assert result['opening_hours']['weekly'][0] == {'day': 'Thứ Hai', 'hours': values[0][1]}


@pytest.mark.parametrize('labels,opening,closing', [
    (['Closed ⋅ Opens 9 AM'], '09:00', None),
    (['Open ⋅ Closes 10:30 PM'], None, '22:30'),
    (['Đóng cửa vào 23:00'], None, '23:00'),
    (['Đang mở cửa · Đóng cửa vào 23:00', '· Đóng cửa vào 23:00'], None, '23:00'),
    (['Đã đóng cửa ⋅ Mở cửa lúc 08:30'], '08:30', None),
    (['Closed ⋅ Opens 9 AM tomorrow'], '09:00', None),
    (['Closed ⋅ Opens 9'], None, None),
    (['Closed ⋅ Opens 9 AM', 'Closed ⋅ Opens 10 AM'], None, None),
])
def test_next_changes_preserve_local_time_without_inventing_timestamp(labels, opening, closing):
    details = extract_place_details(_Driver('<div role="main">' +
        ''.join(f'<span>{label}</span>' for label in labels) + '</div>'), 'id')
    for key, expected in [('next_opening_time', opening), ('next_closing_time', closing)]:
        value = details[key]
        if expected is None:
            assert value is None
        else:
            assert value['local_time'] == expected
            assert value['at'] is None
            assert value['raw_label'] in labels
    if 'tomorrow' in labels[0]:
        assert details['next_opening_time']['day_label'] == 'tomorrow'


@pytest.mark.parametrize('suffix,lat,lng,identifier,kind', [
    ('!1sChIJtest!3d10.76!4d106.7', 10.76, 106.7, 'ChIJtest', 'place_id'),
    ('!1s0x123:0x456!3d10.76!4d106.7', 10.76, 106.7, '0x123:0x456', 'data_id'),
    ('@10.76,106.7,15z', None, None, None, None),
    ('!3d91!4d106', None, None, None, None),
    ('!3d10!4d106!3d11!4d107', None, None, None, None),
    ('!1sChIJone!1sChIJtwo!', None, None, None, None),
])
def test_entity_location_and_identifier_are_explicit_not_camera(suffix, lat, lng, identifier, kind):
    driver = _Driver('<div role="main"><h1>Cafe</h1></div>')
    driver.current_url = 'https://www.google.com/maps/place/Cafe/data=' + suffix
    details = extract_place_details(driver, 'unchanged-source-id')
    assert details['store_id'] == 'unchanged-source-id'
    assert (details['latitude'], details['longitude']) == (lat, lng)
    assert details['google_identifier'] == identifier
    assert details['google_identifier_type'] == kind
    assert details['google_place_id'] == (identifier if kind == 'place_id' else None)


def test_v2_publication_requires_identity_and_is_atomic(tmp_path):
    root = tmp_path/'run'
    (root/'metadata').mkdir(parents=True)
    store = Store('id', 'Cafe', 'Cafe')
    artifacts = CrawlArtifacts(root, '2026-11-04T00:00:00+00:00', stores=[store])
    snapshot = extract_place_details(_Driver('<div role="main"><h1>Cafe</h1></div>'), 'id')
    with pytest.raises(ValueError, match='confirmed place identity'):
        artifacts.on_activity('place_details', dict(store_id='id', google_place_details=snapshot))
    sidecar = root/'metadata/id.place_details.json'
    assert not sidecar.exists()
    artifacts.on_activity('place_resolution', dict(store_id='id', place_entity_confirmed=True))
    artifacts.on_activity('place_details', dict(store_id='id', google_place_details=snapshot))
    assert json.loads(sidecar.read_text()) == snapshot
    assert not sidecar.with_suffix('.json.tmp').exists()


def test_complete_week_and_duplicate_days_are_distinguished():
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    def extract(days):
        html = '<table class="eK4R0e">' + ''.join(
            f'<tr><td>{day}</td><td>08:00–22:00</td></tr>' for day in days) + '</table>'
        return extract_place_details(_Driver(html), 'id')
    assert extract(days)['metadata_completeness']['weekly_hours_complete'] is True
    assert extract(days[:-1] + ['Monday'])['metadata_completeness']['weekly_hours_complete'] is False


def test_failed_atomic_snapshot_replacement_preserves_previous_snapshot(tmp_path, monkeypatch):
    from pathlib import Path
    root = tmp_path/'run'
    (root/'metadata').mkdir(parents=True)
    store = Store('id', 'Cafe', 'Cafe')
    artifacts = CrawlArtifacts(root, '2026-11-04T00:00:00+00:00', stores=[store])
    artifacts.on_activity('place_resolution', dict(store_id='id', place_entity_confirmed=True))
    first = extract_place_details(_Driver('<div role="main"><h1>Cafe</h1></div>'), 'id')
    artifacts.on_activity('place_details', dict(store_id='id', google_place_details=first))
    path = root/'metadata/id.place_details.json'
    original_replace = Path.replace
    def fail_sidecar_replace(self, target):
        if target == path:
            raise OSError('injected atomic replacement failure')
        return original_replace(self, target)
    monkeypatch.setattr(Path, 'replace', fail_sidecar_replace)
    second = dict(first, observed_at='2026-11-04T01:00:00+00:00')
    with pytest.raises(OSError, match='injected'):
        artifacts.on_activity('place_details', dict(store_id='id', google_place_details=second))
    assert json.loads(path.read_text()) == first


def test_missing_optional_snapshot_does_not_prevent_review_stage(monkeypatch):
    class Navigator:
        def __init__(self, driver):
            self.last_resolution_diagnostics = {'place_entity_confirmed': True}
        def open_store(self, store):
            return 'https://www.google.com/maps/place/Cafe'
    reached = []
    def failed_optional_metadata(*args):
        raise ValueError('unsupported optional DOM')
    def reached_reviews(self):
        reached.append(True)
        raise ReviewSurfaceError('review stage reached')
    monkeypatch.setattr(crawler_module, 'GoogleMapsNavigator', Navigator)
    monkeypatch.setattr(crawler_module, 'extract_place_details', failed_optional_metadata)
    monkeypatch.setattr(crawler_module.ReviewSurface, 'prepare_and_classify', reached_reviews)
    crawler = crawler_module.GoogleMapsCrawler(object(), object(), timestamp_enrichment=False)
    monkeypatch.setattr(crawler, '_checkpoint', lambda *args, **kwargs: None)
    with pytest.raises(ReviewSurfaceError, match='review stage reached'):
        crawler.crawl(Store('id', 'Cafe', 'Cafe'), _Driver('<div role="main"><h1>Cafe</h1></div>'), warm_up=False)
    assert reached == [True]


@pytest.mark.parametrize('mode', ['existing_control', 'observed_label', 'already_available',
                                 'missing_control', 'unknown_expansion', 'click_failure', 'no_table'])
def test_hours_expansion_is_bounded_and_restores_control(monkeypatch, mode):
    import selenium.webdriver.support.ui as ui
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    values = ['08:00–12:00, 13:00–22:00', '18:00–02:00', 'Open 24 hours',
              'Closed', '08:00–22:00', '08:00–22:00', '08:00–22:00']
    table = '<table class="eK4R0e">' + ''.join(
        f'<tr><td>{day}</td><td>{hours}</td></tr>' for day, hours in zip(days, values)) + '</table>'
    waits = []
    class ImmediateWait:
        def __init__(self, driver, timeout, poll_frequency):
            assert timeout == 2 and poll_frequency == 0.1
            self.driver = driver
        def until(self, condition):
            waits.append(True)
            result = condition(self.driver)
            if not result:
                raise TimeoutError('no weekly table')
            return result
    monkeypatch.setattr(ui, 'WebDriverWait', ImmediateWait)
    clicks = []

    class Control:
        tag_name = 'button'
        text = 'Đang mở cửa · Đóng cửa vào 23:00'
        expanded = 'false' if mode != 'unknown_expansion' else None
        def get_attribute(self, name):
            return {'aria-expanded': self.expanded, 'aria-label': 'Giờ mở cửa'}.get(name)
        def is_displayed(self): return True
        def click(self):
            clicks.append(self.expanded)
            if mode == 'click_failure':
                raise RuntimeError('unavailable optional control')
            self.expanded = 'true' if self.expanded == 'false' else 'false'
            if self.expanded == 'true' and mode != 'no_table':
                driver.soup.append(BeautifulSoup(table, 'html.parser'))
                driver.soup.select_one('div[role=main]').append(BeautifulSoup('<span>Holiday hours may differ</span>', 'html.parser'))
            elif self.expanded == 'false':
                for node in driver.soup.select('table.eK4R0e'):
                    node.decompose()
    control = Control()

    class HoursLabel(_Element):
        def find_elements(self, by, selector):
            assert by == 'xpath' and selector.startswith('ancestor-or-self::')
            return [control]

    class Driver(_Driver):
        def find_elements(self, by, selector):
            if selector == "[data-item-id='oh']":
                return [] if mode in {'observed_label', 'missing_control'} else [control]
            elements = super().find_elements(by, selector)
            if mode == 'observed_label' and selector.startswith("div[role='main'] span"):
                return [HoursLabel(e.node) if e.text == 'Hiện giờ mở cửa trong tuần' else e for e in elements]
            return elements
    driver = Driver('<div role="main"><span>Đang mở cửa · Đóng cửa vào 23:00</span>' +
        ('<span>Hiện giờ mở cửa trong tuần</span>' if mode == 'observed_label' else '') + '</div>' +
        (table if mode == 'already_available' else ''))
    details = extract_place_details(driver, 'id')
    assert details['next_closing_time']['local_time'] == '23:00'
    assert details['next_closing_time']['at'] is None
    assert 'Đang mở cửa · Đóng cửa vào 23:00' in details['raw_status_labels']
    success = mode in {'existing_control', 'observed_label', 'already_available'}
    assert details['metadata_completeness']['weekly_hours_complete'] is success
    if success:
        weekly = details['weekly_opening_hours']
        assert len(weekly) == 7
        assert len(weekly[0]['intervals']) == 2
        assert weekly[1]['intervals'][0]['closes_day_offset'] == 1
        assert weekly[2]['status'] == 'OPEN_24_HOURS'
        assert weekly[3]['status'] == 'CLOSED'
    else:
        assert details['weekly_opening_hours'] == []
    if mode in {'existing_control', 'observed_label', 'no_table'}:
        assert clicks == ['false', 'true']
        assert control.expanded == 'false'
        if success:
            assert 'Holiday hours may differ' in details['special_hours_warnings']
        assert details['extraction_evidence']['hours_expansion']['restored'] is True
    elif mode == 'click_failure':
        assert clicks == ['false']
        assert details['extraction_evidence']['hours_expansion']['error_type'] == 'RuntimeError'
    else:
        assert clicks == []
    assert len(waits) <= 1



def test_future_closing_label_does_not_mean_currently_closed():
    details = extract_place_details(_Driver('<div role="main"><span>Đóng cửa vào 23:00</span></div>'), 'id')
    assert details['next_closing_time']['local_time'] == '23:00'
    assert details['next_closing_time']['at'] is None
    assert details['open_now'] is None
    assert details['business_status'] == 'UNKNOWN'



@pytest.mark.parametrize('hours', ['08:00–12:00<br>13:00–22:00', '9 AM–12 PM<br>1 PM–5 PM'])
def test_split_shifts_separated_by_dom_line_breaks(hours):
    details = extract_place_details(_Driver('<table class="eK4R0e"><tr><td>Monday</td><td>' + hours + '</td></tr></table>'), 'id')
    row = details['weekly_opening_hours'][0]
    assert row['status'] == 'OPEN'
    assert len(row['intervals']) == 2
    assert details['metadata_completeness']['weekly_hours_complete'] is False


@pytest.mark.parametrize('initial', ['false', 'true'])
@pytest.mark.parametrize('language', ['vi', 'en'])
@pytest.mark.parametrize('schedule', ['captured', 'varied', 'missing_day', 'duplicate_day'])
def test_verified_hours_dropdown_rows_and_state(initial, language, schedule):
    # Sanitized captured DOM: no data-item-id=oh; tooltip is an aria-label on an icon.
    days = (['Thứ Tư', 'Thứ Năm', 'Thứ Sáu', 'Thứ Bảy', 'Chủ<br>Nhật', 'Thứ Hai', 'Thứ Ba']
            if language == 'vi' else
            ['Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday', 'Monday', 'Tuesday'])
    shifts = [['06:00–21:00'] for _ in days]
    if schedule == 'varied':
        shifts = [['06:00–21:00'], ['06:00–12:00', '14:00–21:00'], ['18:00–02:00'],
                  ['Mở cửa 24 giờ' if language == 'vi' else 'Open 24 hours'],
                  ['Đóng cửa' if language == 'vi' else 'Closed'], ['08:00–17:00'], ['09:00–19:00']]
    pairs = list(zip(days, shifts))
    if schedule == 'missing_day': pairs.pop()
    elif schedule == 'duplicate_day': pairs.append(pairs[0])
    rows = ''.join('<tr class="y0skZc"><td class="ylH6lf"><div>' + day +
                   '</div></td><td class="mxowUb" role="text"><ul>' +
                   ''.join('<li class="G8aQO">' + shift + '</li>' for shift in periods) +
                   '</ul></td><td class="HuudEc"><button jsaction="pane.openhours.wfvdle58.copy" '
                   'aria-label="Sao chép giờ mở cửa">copy</button></td></tr>'
                   for day, periods in pairs)
    html = '''<div role="main"><h1 class="DUwDvf">Fixture Cafe</h1>
      <div class="OqCZI fontBodyMedium VrynGf WVXvdc">
        <div class="OMl5r hH0dDd jBYmhd" role="button" tabindex="0" aria-expanded="''' + initial + '''"
             jsaction="pane.openhours.wfvdle58.dropdown; keydown:pane.openhours.wfvdle58.dropdown">
          <span class="LT79uc" aria-label="Giờ" role="img"></span>
          <div class="MkV9"><div class="o0Svhf"><span class="ZDu9vd"><span class="aSftqf">Đang đóng cửa</span></span>
            <span class="puWIL" aria-label="Hiện giờ mở cửa trong tuần" role="img"></span>
          </div></div>
        </div>
        <div class="t39EBf GUrTXd"><div><table class="eK4R0e fontBodyMedium"><tbody>''' + rows + '''</tbody></table>
          <span>Holiday hours may differ</span>
          <div class="onOnO"><button class="uj73Ce" aria-label="Đề xuất chỉnh sửa giờ mở cửa"
            jsaction="pane.wfvdle59"><span>Đề xuất giờ khác</span></button></div>
        </div></div>
      </div></div>
      <table class="eK4R0e"><tbody><tr><td>Unrelated day</td><td>00:00–01:00</td></tr></tbody></table>'''
    if language == 'en':
        html = html.replace('wfvdle58', 'wfvdle99')  # Dynamic action ID is not a stable selector.
    html = html.replace('<h1 class="DUwDvf">Fixture Cafe</h1>',
        '<h1 class="DUwDvf">Fixture Cafe</h1>'
        '<button data-item-id="address">12 Main Street</button><button class="DkEaL">Cafe</button>'
        '<div class="F7nice"><span aria-hidden="true">4.5</span><span aria-hidden="true">(42)</span></div>'
        '<button data-item-id="phone:tel:+842812345678"></button>'
        '<a data-item-id="authority" href="https://example.com/cafe">Website</a>'
        '<a data-item-id="menu" href="https://example.com/menu">Menu</a>'
        '<span aria-label="Giá mỗi người: 50.000–100.000 ₫"></span>'
        '<button data-item-id="oloc">ABCD+12</button>')
    clicks = []
    class ActualElement(_Element):
        @property
        def text(self):
            return self.node.get_text('\n', strip=True)
        @property
        def tag_name(self): return self.node.name
        def find_elements(self, by, selector):
            if by == 'xpath':
                assert selector == '..'
                return [ActualElement(self.node.parent)]
            assert by == 'css selector'
            return query(self.node, selector)
        def is_displayed(self): return True
        def click(self):
            action = self.node.get('jsaction', '')
            assert 'pane.openhours.' in action and '.dropdown' in action
            assert '.copy' not in action and 'pane.wfvdle59' not in action
            clicks.append(action)
            self.node['aria-expanded'] = 'true' if self.node['aria-expanded'] == 'false' else 'false'
    class ActualDriver(_Driver):
        def find_elements(self, by, selector):
            assert by == 'css selector'
            return query(self.soup, selector)
    driver = ActualDriver(html)
    driver.current_url = 'https://www.google.com/maps/place/Fixture/data=!1s0x123:0x456!3d10.76!4d106.7'
    toggle = driver.soup.select_one('[role=button]')
    def query(node, selector):
        results = []
        for found in node.select(selector):
            if found.find_parent('div', class_='t39EBf') is not None and toggle.get('aria-expanded') != 'true':
                continue
            results.append(ActualElement(found))
        return results
    details = extract_place_details(driver, 'unchanged-id')
    weekly = details['weekly_opening_hours']
    assert details['store_id'] == 'unchanged-id'
    assert details['name'] == 'Fixture Cafe'
    assert details['address'] == '12 Main Street'
    assert details['category'] == 'Cafe'
    assert details['rating'] == 4.5 and details['review_count'] == 42
    assert details['phone'] == '+842812345678'
    assert details['website'] == 'https://example.com/cafe'
    assert details['menu_url'] == 'https://example.com/menu'
    assert details['price_range'] == 'Giá mỗi người: 50.000–100.000 ₫'
    assert details['plus_code'] == 'ABCD+12'
    assert (details['latitude'], details['longitude']) == (10.76, 106.7)
    assert details['google_identifier'] == '0x123:0x456'
    assert details['google_identifier_type'] == 'data_id'
    assert details['source_url'] == details['maps_url'] == driver.current_url
    assert details['observed_at'] == details['collected_at']
    assert details['business_status'] == 'UNKNOWN'  # Outside-hours label is not permanent closure.
    assert details['open_now'] is False
    assert len(weekly) == len(pairs)
    assert toggle['aria-expanded'] == initial
    assert len(clicks) == (2 if initial == 'false' else 0)
    assert details['metadata_completeness']['weekly_hours_complete'] is (schedule in {'captured','varied'})
    assert weekly[4]['iso_weekday'] == 7
    assert weekly[4]['day'] == ('Chủ Nhật' if language == 'vi' else 'Sunday')
    assert 'Holiday hours may differ' in details['special_hours_warnings']
    assert len(details['opening_hours']['raw_rows']) == len(pairs)
    if language == 'vi':
        assert details['opening_hours']['raw_rows'][4]['day_text'] == 'Chủ\nNhật'
    if schedule == 'varied':
        assert len(weekly[1]['intervals']) == 2
        assert details['opening_hours']['raw_rows'][1]['shifts'] == shifts[1]
        assert weekly[2]['intervals'][0]['closes_day_offset'] == 1
        assert weekly[3]['status'] == 'OPEN_24_HOURS'
        assert weekly[4]['status'] == 'CLOSED'
        assert weekly[5]['intervals'][0]['opens'] == '08:00'
        assert weekly[6]['intervals'][0]['opens'] == '09:00'
    if initial == 'false':
        assert details['extraction_evidence']['hours_expansion']['result'] == 'EXPANDED'
        assert details['extraction_evidence']['hours_expansion']['restored'] is True
    else:
        assert details['extraction_evidence']['hours_expansion']['result'] == 'ALREADY_AVAILABLE'


@pytest.mark.parametrize('mode,expected', [('multiple','AMBIGUOUS_CONTROL'), ('missing_parent','CONTROL_SCOPE_UNAVAILABLE')])
def test_verified_control_never_falls_back_to_unrelated_schedule(mode, expected):
    toggle = '<div role="button" aria-expanded="false" jsaction="pane.openhours.wfvdle58.dropdown"></div>'
    html = '<div role="main">' + toggle * (2 if mode == 'multiple' else 1) + '</div>'
    html += '<table class="eK4R0e"><tbody>' + ''.join(
        f'<tr><td>{day}</td><td>06:00–21:00</td></tr>' for day in
        ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']) + '</tbody></table>'
    # Basic _Element has no XPath support: scope cannot be verified.
    details = extract_place_details(_Driver(html), 'id')
    assert details['weekly_opening_hours'] == []
    assert details['metadata_completeness']['weekly_hours_complete'] is False
    assert details['extraction_evidence']['hours_expansion']['result'] == expected
    assert details['extraction_evidence']['hours_expansion']['attempted'] is False
