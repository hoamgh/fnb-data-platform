"""Offline Google Maps review UI contracts for English and Vietnamese labels."""

from bs4 import BeautifulSoup
import pytest

from crawl_experiment.sources.google_maps import selectors
from crawl_experiment.sources.google_maps.extractor import ReviewExtractor
from crawl_experiment.sources.google_maps.review_surface import (
    ReviewSurface,
    ReviewSurfaceState,
    _normalized,
)


class _Element:
    def __init__(self, node):
        self.node = node
        self.tag_name = node.name

    @property
    def text(self):
        return self.node.get_text(" ", strip=True)

    def get_attribute(self, name):
        return self.node.get(name)

    def click(self):
        self.node["data-clicked"] = "true"

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def find_elements(self, by, selector):
        assert by == "css selector"
        return [_Element(node) for node in self.node.select(selector)]


class _Driver:
    def __init__(self, markup):
        self.soup = BeautifulSoup(markup, "html.parser")

    def find_elements(self, by, selector):
        if by == "xpath":
            return []
        assert by == "css selector"
        return [_Element(node) for node in self.soup.select(selector)]


@pytest.mark.parametrize("label", ["Bài đánh giá", "Đánh giá", "Reviews"])
def test_open_reviews_uses_localized_control_not_write_review(label):
    driver = _Driver(
        "<main role='main'>"
        "<button role='tab' aria-label='Viết bài đánh giá'>Viết bài đánh giá</button>"
        "<button role='tab' aria-label='Write a review'>Write a review</button>"
        f"<button role='tab' aria-label='{label}'>{label}</button>"
        "</main>"
    )
    surface = ReviewSurface(driver)
    surface.resolve_review_pane = lambda: "resolved"

    assert surface.open_reviews() == "resolved"
    assert driver.soup.select("[data-clicked=true]")[0]["aria-label"] == label


def test_vietnamese_review_count_button_and_rating():
    driver = _Driver(
        "<main role='main'>"
        "<button aria-label='Viết bài đánh giá'>Viết bài đánh giá</button>"
        "<button aria-label='126 bài đánh giá'>126 bài đánh giá</button>"
        "<div data-review-id='review-1'>"
        "<span role='img' aria-label='5 sao'></span>"
        "<span class='wiI7pd'>Món ăn ngon</span>"
        "</div></main>"
    )
    surface = ReviewSurface(driver)
    surface.resolve_review_pane = lambda: "resolved"

    assert surface.open_reviews() == "resolved"
    assert driver.soup.select("[data-clicked=true]")[0]["aria-label"] == "126 bài đánh giá"
    review = ReviewExtractor().extract(
        driver.find_elements("css selector", selectors.REVIEW_CARD)[0], "store-1"
    )
    assert review.rating == 5.0
    assert review.text == "Món ăn ngon"


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("<main role='main'>Chưa có bài đánh giá</main>", ReviewSurfaceState.NO_REVIEWS),
        (
            "<main role='main'><button role='tab' aria-label='Bài đánh giá'></button>"
            "Đăng nhập để xem tất cả bài đánh giá</main>",
            ReviewSurfaceState.LIMITED,
        ),
        (
            "<div role='dialog'>Đăng nhập để tiếp tục xem bài đánh giá</div>",
            ReviewSurfaceState.AUTH_REQUIRED,
        ),
    ],
)
def test_vietnamese_review_state(markup, expected):
    state, evidence = ReviewSurface(_Driver(markup)).classify_state()
    assert state == expected, evidence


def test_vietnamese_sort_button_confirms_full_review_surface():
    driver = _Driver(
        "<main role='main'><button role='tab' aria-label='Bài đánh giá'></button>"
        "<button aria-label='Sắp xếp bài đánh giá'></button>"
        "<div id='pane'><div data-review-id='review-1'></div></div></main>"
    )
    surface = ReviewSurface(driver)
    pane = driver.find_elements("css selector", "#pane")[0]
    state, evidence = surface.classify_state(pane)

    assert state == ReviewSurfaceState.FULL, evidence
    assert evidence["sort_control"] is True


@pytest.mark.parametrize(
    "control",
    [
        "<button title='Sắp xếp bài đánh giá'>▾</button>",
        "<button aria-label='Sort reviews menu'>▾</button>",
        "<div role='button' aria-label='Sắp xếp bài đánh giá'>▾</div>",
        "<button jsaction='pane.reviewSort.open'>▾</button>",
        "<button>Phù hợp nhất</button>",
        "<button>Mới nhất</button>",
    ],
)
def test_sort_control_localized_variants(control):
    driver = _Driver(f"<div role='main'>{control}</div>")
    surface = ReviewSurface(driver)
    assert surface._find_sort_button() is not None


def test_sort_control_ignores_unrelated_review_and_menu_buttons():
    driver = _Driver(
        "<div role='main'><button aria-label='Viết bài đánh giá'>Viết bài đánh giá</button>"
        "<button aria-label='Menu'>Menu</button></div>"
    )
    assert ReviewSurface(driver)._find_sort_button() is None


@pytest.mark.parametrize(
    ("label", "seconds"),
    [
        ("Vừa xong", 0),
        ("Hôm qua", 86400),
        ("2 ngày trước", 2 * 86400),
        ("một tuần trước", 7 * 86400),
        ("3 months ago", 3 * 30 * 86400),
    ],
)
def test_relative_review_dates_in_vietnamese_and_english(label, seconds):
    assert ReviewSurface._relative_age_seconds(label) == seconds


@pytest.mark.parametrize("label", ["mới nhất", "mơ\u0301i nhâ\u0301t"])
def test_vietnamese_newest_normalization_and_selected_signals(label):
    newest_labels = {_normalized(value) for value in selectors.NEWEST_LABELS}
    assert _normalized(label) == "mới nhất"

    driver = _Driver(
        f"<button aria-label='Sắp xếp: {label}'>{label}</button>"
        f"<div role='option' aria-selected='true'>{label}</div>"
    )
    button = driver.find_elements("css selector", "button")[0]
    option = driver.find_elements("css selector", "[role='option']")[0]
    surface = ReviewSurface(driver)

    assert surface._control_reports_newest(button, newest_labels)
    assert surface._option_selected_signal(option, newest_labels) == (True, "aria-selected")


def test_sort_newest_clicks_decomposed_vietnamese_label(monkeypatch):
    label = "mơ\u0301i nhâ\u0301t"  # Actual Unicode form observed in Google Maps sort menu.
    driver = _Driver(
        "<main role='main'><button aria-label='Sắp xếp bài đánh giá'></button></main>"
        "<div role='menu'>"
        "<div role='option' aria-selected='false'>phù hợp nhất</div>"
        "<div role='option' aria-selected='false'>xếp hạng cao nhất</div>"
        f"<div role='option' aria-selected='false'>{label}</div>"
        "</div>"
    )
    surface = ReviewSurface(driver, sleeper=lambda _: None)
    monkeypatch.setattr(surface, "_button_diagnostics", lambda _: {})
    monkeypatch.setattr(surface, "_wait_until_control_stable", lambda _: None)
    monkeypatch.setattr(surface, "_open_menu_with_bounded_clicks", lambda _: (True, ["normal"]))

    def click_and_select(element):
        element.node["data-clicked"] = "true"
        element.node["aria-selected"] = "true"

    monkeypatch.setattr(_Element, "click", click_and_select)
    surface._sort_newest_once()

    clicked = driver.soup.select("[data-clicked=true]")
    assert len(clicked) == 1
    assert clicked[0].get_text(strip=True) == label


def test_review_tab_scrolls_then_waits_before_native_click(monkeypatch):
    events = []
    driver = _Driver("<button role='tab' aria-label='Bài đánh giá'></button>")
    driver.execute_script = lambda script, element: events.append('scroll')
    original_click = _Element.click
    def click(element):
        assert events == ['scroll', 'stable']
        events.append('native_click')
        original_click(element)
    monkeypatch.setattr(_Element, 'click', click)
    surface = ReviewSurface(driver)
    monkeypatch.setattr(surface, '_wait_until_control_stable', lambda element: events.append('stable'))
    pane = object()
    surface.resolve_review_pane = lambda: pane
    assert surface.open_reviews() is pane
    assert events == ['scroll', 'stable', 'native_click']


def test_selected_review_tab_is_not_clicked_or_scrolled(monkeypatch):
    driver = _Driver("<button role='tab' aria-label='Reviews' aria-selected='true'></button>")
    def unexpected(*args):
        pytest.fail('selected tab must not be toggled')
    driver.execute_script = unexpected
    monkeypatch.setattr(_Element, 'click', unexpected)
    surface = ReviewSurface(driver)
    surface.resolve_review_pane = lambda: 'pane'
    assert surface.open_reviews() == 'pane'


@pytest.mark.parametrize('intercepted', [True, False])
def test_review_tab_failure_retains_typed_evidence(monkeypatch, intercepted):
    from selenium.common.exceptions import ElementClickInterceptedException
    from crawl_experiment.core.errors import ReviewSurfaceError
    driver = _Driver("<button role='tab' aria-label='Reviews'></button>")
    cause = (ElementClickInterceptedException('element click intercepted: search combobox')
             if intercepted else RuntimeError('driver transport failure'))
    def fail(element):
        raise cause
    monkeypatch.setattr(_Element, 'click', fail)
    surface = ReviewSurface(driver)
    with pytest.raises(ReviewSurfaceError) as caught:
        surface.open_reviews()
    assert caught.value.__cause__ is cause
    assert caught.value.local_review_tab_click_intercepted is intercepted
    surface.classify_state = lambda: (ReviewSurfaceState.UNKNOWN, {})
    state, pane, evidence = surface.prepare_and_classify()
    assert state == ReviewSurfaceState.UNKNOWN
    assert pane is None
    assert (evidence.get('open_error_code') == 'REVIEW_TAB_CLICK_INTERCEPTED') is intercepted
