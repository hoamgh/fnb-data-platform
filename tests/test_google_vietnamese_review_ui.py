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
        if by == 'xpath' and selector.startswith('ancestor::'):
            import re
            return [_Element(node) for node in self.node.parents
                    if (node.name in {'button','a'} or node.get('role') in {'button','tab'})
                    and re.search(r'write|viết',node.get_text(' ',strip=True)+' '+str(node.get('aria-label','')),re.I)]
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


@pytest.mark.parametrize('markup',[
    '<div tabindex="0"><span>Bài đánh giá</span></div>',
    '<a href="#reviews">Reviews</a>',
    '<div role="button">Bài đánh giá</div>',
])
def test_visible_review_text_click_without_tab_or_button_tag(markup):
    driver=_Driver('<main role="main">'+markup+'</main>')
    surface=ReviewSurface(driver)
    surface.resolve_review_pane=lambda:'actual-review-pane'
    assert surface.open_reviews()=='actual-review-pane'
    assert len(driver.soup.select('[data-clicked=true]'))==1


def test_text_fallback_does_not_click_nested_write_review_or_review_body():
    driver=_Driver('''<main role="main">
      <button>Viết <span>Bài đánh giá</span></button>
      <div data-review-id="review"><span>Bài đánh giá</span></div>
      <div role="feed"><span>Bài đánh giá</span></div>
    </main>''')
    assert ReviewSurface(driver)._review_controls()==[]


@pytest.mark.parametrize('menu',['','<button role="tab">Thực đơn</button>'])
def test_top_review_tab_selected_with_or_without_menu(menu):
    driver=_Driver('<main role="main"><div role="tablist">'
        '<button role="tab">Tổng quan</button>'+menu+
        '<button role="tab" id="top-reviews">Bài đánh giá</button>'
        '<button role="tab">Giới thiệu</button></div>'
        '<button>Viết bài đánh giá</button></main>')
    surface=ReviewSurface(driver);surface.resolve_review_pane=lambda:'pane'
    assert surface.open_reviews()=='pane'
    clicked=driver.soup.select('[data-clicked=true]')
    assert len(clicked)==1 and clicked[0]['id']=='top-reviews'


def test_top_tab_fast_path_skips_broad_and_xpath_fallbacks():
    driver=_Driver('<main role="main"><div role="tablist">'
        '<button role="tab">Tổng quan</button>'
        '<button role="tab">Bài đánh giá</button></div></main>')
    calls=[];find=driver.find_elements
    def record(by,selector):
        calls.append((by,selector))
        return find(by,selector)
    driver.find_elements=record
    surface=ReviewSurface(driver)
    controls=surface._review_controls()
    assert len(controls)==1 and controls[0].text=='Bài đánh giá'
    assert len(calls)==1 and 'tablist' in calls[0][1]
    assert surface.last_control_diagnostics['detection_path']=='TOP_TAB_FAST_PATH'


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


def test_text_only_review_tab_is_preferred_to_count_button_and_write_review():
    driver=_Driver("<main role='main'><button aria-label='126 bài đánh giá'>126 bài đánh giá</button>"
                   "<button role='tab'>Viết bài đánh giá</button>"
                   "<button role='tab'><span>Bài đánh giá</span></button></main>")
    surface=ReviewSurface(driver);surface.resolve_review_pane=lambda:'pane'
    assert surface.open_reviews()=='pane'
    clicked=driver.soup.select('[data-clicked=true]')
    assert len(clicked)==1 and clicked[0].get('role')=='tab'
    assert clicked[0].get_text(strip=True)=='Bài đánh giá'


def test_waits_for_lazy_loaded_text_only_review_tab():
    driver=_Driver("<main role='main'></main>")
    now=[0]
    def sleep(seconds):
        now[0]+=seconds
        if not driver.soup.select('[role=tab]'):
            driver.soup.select_one('main').append(BeautifulSoup('<button role="tab">Bài đánh giá</button>','html.parser'))
    surface=ReviewSurface(driver,clock=lambda:now[0],sleeper=sleep)
    surface.resolve_review_pane=lambda:'pane'
    assert surface.open_reviews()=='pane' and now[0]>0


@pytest.mark.parametrize('label',['Assorted breads and iced drinks','Milk tea with assorted toppings'])
def test_saved_photo_labels_are_not_sort_controls(label):
    driver=_Driver(f'<main role="main"><button class="Tya61d" aria-label="{label}"></button></main>')
    surface=ReviewSurface(driver)
    assert surface._find_sort_button() is None
    assert not surface._interactive_sort_control_confirmed(driver.find_elements('css selector','button')[0])


def test_positive_metadata_count_prevents_false_zero_review_classification():
    driver=_Driver('<main role="main">Chưa có bài đánh giá</main>')
    surface=ReviewSurface(driver);surface.advertised_review_count=68
    state,evidence=surface.classify_state()
    assert state==ReviewSurfaceState.UNKNOWN and evidence['no_reviews_count_conflict']


def test_initial_no_review_text_does_not_prevent_clicking_reviews_tab():
    driver=_Driver('<main role="main">Chưa có bài đánh giá<button role="tab">Bài đánh giá</button></main>')
    surface=ReviewSurface(driver)
    state,_=surface.classify_state()
    assert state!=ReviewSurfaceState.NO_REVIEWS
    surface.resolve_review_pane=lambda:driver.find_elements('css selector','main')[0]
    surface.prepare_and_classify()
    assert driver.soup.select('[data-clicked=true]')


def test_historical_xpath_review_fallback_is_used():
    element=_Element(BeautifulSoup('<button role="tab">Reviews for this place</button>','html.parser').button)
    class XPathDriver:
        def find_elements(self,by,selector):return [element] if by=='xpath' else []
    surface=ReviewSurface(XPathDriver());surface.resolve_review_pane=lambda:'pane'
    assert surface.open_reviews()=='pane'
    assert element.get_attribute('data-clicked')=='true'


@pytest.mark.parametrize('kind',['stale','intercept'])
def test_tab_is_reacquired_after_rerender_or_interception(monkeypatch,kind):
    from selenium.common.exceptions import StaleElementReferenceException,ElementClickInterceptedException
    driver=_Driver('<button role="tab">Bài đánh giá</button>')
    count=[0];click=_Element.click
    def flaky(element):
        count[0]+=1
        if count[0]==1:
            error=StaleElementReferenceException if kind=='stale' else ElementClickInterceptedException
            raise error('DOM rerender/overlay')
        click(element)
    monkeypatch.setattr(_Element,'click',flaky)
    surface=ReviewSurface(driver);surface.resolve_review_pane=lambda:'pane'
    assert surface.open_reviews()=='pane' and count[0]==2


def test_search_feed_cannot_be_selected_as_review_pane():
    class Driver:
        def execute_script(self,*args):return dict(scrollHeight=1000,clientHeight=500,searchFeed=True)
    class Pane:
        def is_displayed(self):return True
        def find_elements(self,*args):return []
    result,reason=ReviewSurface(Driver())._score_pane(Pane(),selectors.REVIEW_PANE_PRIMARY)
    assert result is None and reason['rejected']=='SEARCH_RESULTS_FEED_NOT_REVIEWS'


def test_already_open_cards_without_tab_reacquire_pane_without_click():
    driver=_Driver('<main role="main"><div data-review-id="real-id"></div></main>')
    surface=ReviewSurface(driver);surface.resolve_review_pane=lambda:'existing-pane'
    assert surface.open_reviews()=='existing-pane'
    assert not driver.soup.select('[data-clicked=true]')


@pytest.mark.parametrize('confirmed',[True,False])
def test_historical_newest_sort_requires_confirmation(monkeypatch,confirmed):
    from crawl_experiment.core.errors import SortError
    driver=_Driver('<main role="main"><button aria-label="Sắp xếp bài đánh giá"></button>'
                   '<div role="menuitemradio" aria-checked="false">Mới nhất</div></main>')
    surface=ReviewSurface(driver)
    button=driver.find_elements('css selector','button')[0]
    option=driver.find_elements('css selector','[role=menuitemradio]')[0]
    monkeypatch.setattr(surface,'_find_sort_button',lambda:button)
    monkeypatch.setattr(surface,'_button_diagnostics',lambda b:{})
    monkeypatch.setattr(surface,'_open_menu_with_bounded_clicks',lambda b:(True,['normal']))
    monkeypatch.setattr(surface,'_visible_sort_options',lambda:[option])
    monkeypatch.setattr(surface,'_confirm_newest',lambda *args:confirmed)
    if confirmed:
        surface._sort_newest_once()
        assert option.get_attribute('data-clicked')=='true'
    else:
        with pytest.raises(SortError,match='not confirmed'):surface._sort_newest_once()


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
