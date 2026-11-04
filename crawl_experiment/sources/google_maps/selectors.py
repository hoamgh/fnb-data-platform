REVIEW_CARD = "div[data-review-id]"
REVIEW_ID_ATTRIBUTE = "data-review-id"
REVIEW_PANE_PRIMARY = "div[role='main'] div.m6QErb.DxyBCb.kA9KIf.dS8AEf"
REVIEW_PANE_FALLBACKS = (
    "div[role='main'] div.m6QErb",
    "div[role='main'] div[tabindex='-1']",
    "div[role='main']",
)
REVIEW_PANE = ", ".join((REVIEW_PANE_PRIMARY, *REVIEW_PANE_FALLBACKS))
PLACE_TABS = "[role='tab']"
PLACE_SHELL = "h1.DUwDvf, [data-item-id='address'], button[jsaction*='pane.reviewChart']"
PLACE_TITLE = "h1.DUwDvf"
PLACE_ADDRESS = "[data-item-id='address']"
PLACE_DETAIL_PANE = "div[role='main']"
PLACE_ENTITY_MARKER = (
    "[data-item-id='address'], "
    "button[jsaction*='pane.reviewChart'], "
    "button[data-item-id^='place-action']"
)
SEARCH_RESULT_CANDIDATES = (
    "div[role='feed'] a[href*='/maps/place/'], "
    "a.hfpxzc[href*='/maps/place/']"
)
SIGN_IN_PROMPT = "a[data-action='sign in'], a[href*='ServiceLogin']"
REVIEWS_TAB_CSS = (
    "button[jsaction*='pane.reviewChart'], "
    "button[jsaction*='pane.rating.moreReviews'], "
    "[role='tab'][aria-label*='review' i]:not([aria-label*='write' i]), "
    "[role='tab'][aria-label*='ánh giá' i]:not([aria-label*='viết' i]), "
    "button[aria-label*='bài đánh giá' i]:not([aria-label*='viết' i]), "
    "button[aria-label*='reviews' i]:not([aria-label*='write' i])"
)
REVIEWS_TAB_XPATH = (
    "//button[(contains(@jsaction,'pane.reviewChart') or "
    "contains(@jsaction,'pane.rating.moreReviews') or "
    "contains(@aria-label,'Reviews') or "
    "contains(@aria-label,'bài đánh giá') or "
    "contains(@aria-label,'Bài đánh giá') or "
    "(@role='tab' and (contains(@aria-label,'ánh giá') or "
    "contains(@aria-label,'Đánh giá'))) or "
    "contains(normalize-space(.),'Bài đánh giá')) and "
    "not(contains(@aria-label,'Viết')) and "
    "not(contains(@aria-label,'viết')) and "
    "not(contains(@aria-label,'Write')) and "
    "not(contains(normalize-space(.),'Viết bài đánh giá')) and "
    "not(contains(normalize-space(.),'Write a review'))]"
)
SORT_BUTTON = (
    "button[aria-label*='Sort reviews' i], "
    "button[aria-label*='Sort' i], "
    "button[aria-label*='Sắp xếp' i], "
    "button[data-value='Sort']"
)
SORT_BUTTON_XPATH = (
    "//button[contains(translate(@aria-label, "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sort') "
    "or contains(@aria-label, 'Sắp xếp')]"
)
SORT_MENU_ITEMS = (
    "div[role='menu'] [role='menuitem'], "
    "li[role='menuitem'], "
    "[role='menuitemradio'], "
    "[role='menuitem'], "
    "[role='option']"
)
NEWEST_LABELS = (
    "newest", "החדשות ביותר", "ใหม่ที่สุด", "最新", "más recientes", "最近",
    "mais recentes", "neueste", "plus récent", "più recenti", "nyeste",
    "новые", "nieuwste", "جديد", "uusimmat", "najnowsze", "senaste",
    "terbaru", "yakın zamanlı", "mới nhất", "नवीनतम",
)
AUTHOR = ".d4r55, [class*='author']"
REVIEWER_PROFILE = "[data-href*='/maps/contrib/'], a[href*='/maps/contrib/']"
REVIEWER_AVATAR = "button[data-href*='/maps/contrib/'] img"
REVIEWER_METADATA = ".RfnDt"
RATING = "span[role='img'][aria-label*='star' i], span[role='img'][aria-label*='sao' i]"
TEXT = ".wiI7pd, .MyEned"
DATE = ".rsqaWe"
OWNER_RESPONSE = ".CDe7pd .wiI7pd, .owner-response"
REVIEW_URL = "a[href*='/maps/reviews/'], a[href*='review_id='], a[href*='reviewId=']"
REVIEW_MEDIA = (
    "button[jsaction*='reviewPhoto'] img, "
    "button[jsaction*='pane.reviewPhoto'] img, "
    "button[data-photo-index] img, "
    ".KtCyie img, .KtCyie .Tya61d, button.Tya61d"
)
SCROLL_TO_END_SCRIPT = """
    const pane = arguments[0];
    const before = {
        scrollTop: pane.scrollTop,
        scrollHeight: pane.scrollHeight,
        clientHeight: pane.clientHeight,
    };
    pane.scrollBy(0, pane.scrollHeight);
    return {
        before: before,
        after: {
            scrollTop: pane.scrollTop,
            scrollHeight: pane.scrollHeight,
            clientHeight: pane.clientHeight,
        },
    };
"""
LIMITED_VIEW_TEXT = ("limited view", "vue limitée", "eingeschränkte ansicht")
LIMITED_PROMPT_TEXT = ("sign in to see", "can't show")
CHALLENGE_TEXT = ("unusual traffic", "not a robot", "recaptcha")
