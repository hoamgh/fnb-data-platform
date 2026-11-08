"""Best-effort snapshot of a *resolved* Google Maps place's public detail pane.

All values come from visible/available DOM controls. Missing fields stay null;
the caller is responsible for verifying store identity before invoking this.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote

from .place_resolution import google_identity


def _elements(driver, selector: str):
    try:
        return driver.find_elements("css selector", selector)
    except Exception:
        return []


def _attr(element, name: str) -> str | None:
    try:
        value = element.get_attribute(name)
        return str(value).strip() or None if value is not None else None
    except Exception:
        return None


def _text(element) -> str | None:
    try:
        value = (element.text or "").strip()
        return " ".join(value.split()) or None
    except Exception:
        return None


def _first(driver, selectors: tuple[str, ...], *, attribute: str | None = None) -> str | None:
    for selector in selectors:
        for element in _elements(driver, selector):
            value = (_attr(element, attribute) if attribute else _text(element))
            if value:
                return value
    return None


def _http_url(value: str | None) -> str | None:
    if value and urlsplit(value).scheme in {"http", "https"} and urlsplit(value).netloc:
        return value
    return None


def _rating(value: str | None) -> float | None:
    if not value:
        return None
    # A review total like "(5)" must never become a five-star rating.
    if not (re.fullmatch(r"\s*[0-5](?:[.,]\d)?\s*", value)
            or re.search(r"\b(stars?|sao)\b", value, re.I)):
        return None
    match = re.search(r"(?<!\d)([0-5](?:[.,]\d)?)(?!\d)", value)
    return float(match.group(1).replace(",", ".")) if match else None


def _review_count(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d[\d.,\s]*?)\s*(?:bài\s+đánh\s+giá|lượt\s+đánh\s+giá|reviews?)\b", value, re.I)
    if match:
        return int(re.sub(r"\D", "", match.group(1)))
    # Some rating summary widgets only show the total inside parentheses.
    match = re.fullmatch(r"\(\s*(\d[\d.,\s]*)\s*\)", value.strip())
    return int(re.sub(r"\D", "", match.group(1))) if match else None


def _rating_and_count(driver):
    rating = None
    count = None
    for selector in ("div.F7nice span[aria-hidden='true']", "div.F7nice [aria-label]", "button[jsaction*='pane.rating.moreReviews']", "button[aria-label*='bài đánh giá' i]", "button[aria-label*='reviews' i]"):
        for element in _elements(driver, selector):
            values = (_attr(element, "aria-label"), _text(element))
            for value in values:
                if rating is None and (selector.startswith("div.F7nice") or "sao" in (value or "").lower() or "star" in (value or "").lower()):
                    rating = _rating(value)
                if count is None:
                    count = _review_count(value)
            if rating is not None and count is not None:
                return rating, count
    return rating, count


def _phone(driver) -> str | None:
    for element in _elements(driver, "[data-item-id^='phone:tel:']"):
        value = _text(element)
        if value and re.search(r"\d{6,}", re.sub(r"[\s().-]", "", value)):
            return value
        item_id = _attr(element, "data-item-id") or ""
        number = item_id.partition("phone:tel:")[2]
        if number and re.fullmatch(r"\+?[\d()\s.-]{6,}", number):
            return number
    return None


def _hours(driver) -> dict | None:
    from selenium.webdriver.support.ui import WebDriverWait

    row_selector = "table.eK4R0e tr, [data-item-id='oh'] table tr"
    # Verified user-supplied DOM: dynamic action suffix; do not select .copy
    # or the separate suggestion button.
    dropdowns = _elements(driver, "[role='button'][jsaction*='pane.openhours.'][jsaction*='.dropdown']")
    row_scope = driver
    scope_error = 'AMBIGUOUS_CONTROL' if len(dropdowns) > 1 else None
    if len(dropdowns) == 1:
        try:
            parents = dropdowns[0].find_elements('xpath', '..')
            if len(parents) == 1:
                row_scope = parents[0]
                row_selector = 'table.eK4R0e tbody > tr'
            else:
                scope_error = 'CONTROL_SCOPE_UNAVAILABLE'
        except Exception:
            scope_error = 'CONTROL_SCOPE_UNAVAILABLE'
    if scope_error:
        return {'summary': None, 'weekly': [], 'raw_labels': _status_labels(driver),
                'expansion': {'attempted': False, 'result': scope_error, 'restored': None}}
    raw_rows = []

    def read_weekly(_=None):
        weekly = []
        raw_rows.clear()
        for row in _elements(row_scope, row_selector):
            cells = _elements(row, "td, th")
            if len(cells) < 2:
                continue
            day = _text(cells[0])  # Wrapped Chủ/Nhật stays in one DOM cell.
            shifts = [_text(item) for item in _elements(cells[1], 'ul > li')]
            hours = ', '.join(shifts) if shifts and all(shifts) else _text(cells[1])
            try:
                raw_day, raw_hours = cells[0].text, cells[1].text
            except Exception:
                raw_day, raw_hours = day, _text(cells[1])
            raw_rows.append({'day': day, 'day_text': raw_day, 'hours_text': raw_hours, 'shifts': shifts})
            if day and hours and not (shifts and not all(shifts)):
                weekly.append({"day": day, "hours": hours})
        return weekly

    summary = _first(driver, ("[data-item-id='oh']",), attribute="aria-label")
    if not summary:
        summary = _first(driver, ("[data-item-id='oh']", "[data-item-id='oh'] [aria-label]"))
    if not summary and len(dropdowns) == 1:
        summary = _text(dropdowns[0]) or _attr(dropdowns[0], 'aria-label')
    raw_labels = _status_labels(driver)
    weekly = read_weekly()
    evidence = {'attempted': False, 'result': 'ALREADY_AVAILABLE' if weekly else 'CONTROL_UNAVAILABLE',
                'restored': None}
    if not weekly:
        controls = list(dropdowns)
        controls.extend(_elements(driver, "[data-item-id='oh']"))
        # This exact label exists in the saved smoke snapshot. Resolve its DOM
        # button ancestor instead of guessing a new Google CSS class.
        for label in _elements(driver, "div[role='main'] span:not([data-review-id] *):not([role='feed'] *)"):
            if _normalized_label(_text(label)) == 'hiện giờ mở cửa trong tuần':
                try:
                    controls.extend(label.find_elements('xpath',
                        "ancestor-or-self::*[self::button or @role='button'][1]"))
                except Exception:
                    pass
        for control in controls:
            try:
                interactive = getattr(control, 'tag_name', '') == 'button' or _attr(control, 'role') == 'button'
                if not interactive or _attr(control, 'aria-expanded') != 'false':
                    continue
                displayed = getattr(control, 'is_displayed', None)
                if displayed is not None and not displayed():
                    continue
                evidence.update(attempted=True, result='UNAVAILABLE')
                try:
                    control.click()
                    weekly = WebDriverWait(driver, 2, poll_frequency=0.1).until(read_weekly)
                    evidence['result'] = 'EXPANDED'
                    for label in _status_labels(driver):
                        if re.search(r'hours.*differ|holiday|special hours|giờ.*khác|ngày lễ', _normalized_label(label)) and label not in raw_labels:
                            raw_labels.append(label)
                except Exception as exc:
                    evidence['result'] = 'UNAVAILABLE'
                    evidence['error_type'] = type(exc).__name__
                finally:
                    try:
                        if _attr(control, 'aria-expanded') == 'true':
                            control.click()
                        evidence['restored'] = _attr(control, 'aria-expanded') == 'false'
                    except Exception:
                        evidence['restored'] = False
                break  # One bounded expansion; never retry another control.
            except Exception:
                continue
    if not (summary or weekly or evidence['attempted']):
        return None
    return {'summary': summary, 'weekly': weekly, 'raw_labels': raw_labels,
            'raw_rows': raw_rows, 'expansion': evidence}


def _visible_detail_items(driver) -> list[dict]:
    """Retain any other labeled business facts currently exposed by Maps UI."""
    items = []
    seen = set()
    for element in _elements(driver, "div[role='main'] [data-item-id]"):
        item_id = _attr(element, "data-item-id")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        items.append({
            "item_id": item_id,
            "text": (_text(element) or "")[:500] or None,
            "aria_label": (_attr(element, "aria-label") or "")[:500] or None,
            "url": _http_url(_attr(element, "href")),
        })
    return items


def _normalized_label(value):
    return unicodedata.normalize('NFC', ' '.join((value or '').split()).casefold())


def _status_labels(driver):
    labels = []
    # Exclude user review text and search-result feeds from business evidence.
    selector = ("div[role='main'] [data-item-id='oh'], "
                "div[role='main'] span:not([data-review-id] *):not([role='feed'] *)")
    for element in _elements(driver, selector):
        displayed = getattr(element, 'is_displayed', None)
        if displayed is not None:
            try:
                if not displayed():
                    continue
            except Exception:
                continue
        for label in (_text(element), _attr(element, 'aria-label')):
            if label and label not in labels:
                normalized = _normalized_label(label)
                if re.search(r'open|clos|mở cửa|đóng cửa|đóng vĩnh viễn|giờ.*khác|hours.*differ|holiday|special hours|ngày lễ', normalized):
                    labels.append(label)
    return labels


def _business_state(labels):
    closures, current = set(), set()
    for raw in labels:
        label = _normalized_label(raw)
        if re.fullmatch(r'(?:permanently closed|(?:(?:đã|bị) )?đóng (?:cửa )?vĩnh viễn)', label):
            closures.add('CLOSED_PERMANENTLY')
        elif re.fullmatch(r'(?:temporarily closed|tạm thời đóng cửa|đóng cửa tạm thời)', label):
            closures.add('CLOSED_TEMPORARILY')
        elif re.match(r'^(?:open|đang mở cửa|mở cửa)(?=$|[ ·⋅•])', label) and not re.match(r'^mở cửa (?:lúc|vào)', label):
            current.add(True)
        elif re.match(r'^(?:closed|đang đóng cửa|đã đóng cửa|đóng cửa)(?=$|[ ·⋅•])', label) and not re.search(r'permanent|temporar|vĩnh viễn|tạm thời', label) and not re.match(r'^đóng cửa (?:lúc|vào) ', label):
            current.add(False)
    conflict = len(closures) > 1 or len(current) > 1 or bool(closures and True in current)
    if conflict:
        return 'UNKNOWN', None, True
    if closures:
        return next(iter(closures)), False, False
    if len(current) == 1:
        is_open = next(iter(current))
        # A bare Closed label does not prove the business is still operational.
        operational = is_open or _next_changes(labels)[0] is not None
        return 'OPERATIONAL' if operational else 'UNKNOWN', is_open, False
    return 'UNKNOWN', None, False


def _clock(value):
    value = _normalized_label(value).replace('.', '')
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', value)
    if not match:
        return None
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), match[3]
    if minute > 59 or (meridiem and not 1 <= hour <= 12) or (not meridiem and (hour > 23 or match[2] is None)):
        return None
    if meridiem:
        hour = hour % 12 + (12 if meridiem == 'pm' else 0)
    return f'{hour:02d}:{minute:02d}'


def _weekly_intervals(hours):
    weekly = []
    days = {'monday': 1, 'thứ hai': 1, 'tuesday': 2, 'thứ ba': 2,
            'wednesday': 3, 'thứ tư': 3, 'thursday': 4, 'thứ năm': 4,
            'friday': 5, 'thứ sáu': 5, 'saturday': 6, 'thứ bảy': 6,
            'sunday': 7, 'chủ nhật': 7}
    for row in (hours or {}).get('weekly', []):
        raw = row['hours']
        label = _normalized_label(raw)
        intervals, state = [], 'UNKNOWN'
        if label in {'closed', 'đóng cửa'}:
            state = 'CLOSED'
        elif label in {'open 24 hours', 'mở cửa cả ngày', 'mở cửa 24 giờ'}:
            state = 'OPEN_24_HOURS'
            intervals = [dict(opens='00:00', closes='00:00', closes_day_offset=1)]
        else:
            valid = True
            for shift in re.split(r'[,;]\s*|\s+(?=\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[–—−-])', label):
                pair = re.split(r'\s*[–—−-]\s*', shift)
                start, end = (_clock(pair[0]), _clock(pair[1])) if len(pair) == 2 else (None, None)
                if start is None or end is None or start == end:
                    valid = False
                    break
                intervals.append(dict(opens=start, closes=end, closes_day_offset=int(end < start)))
            if valid and intervals:
                state = 'OPEN'
            else:
                intervals = None  # Preserve raw unsupported/ambiguous hours, never guess.
        weekly.append(dict(day=row['day'], iso_weekday=days.get(_normalized_label(row['day'])),
                           raw_hours=raw, status=state, intervals=intervals))
    counts = [row['iso_weekday'] for row in weekly]
    complete = len(weekly) == 7 and set(counts) == set(range(1, 8)) and all(row['status'] != 'UNKNOWN' for row in weekly)
    return weekly, complete


def _next_changes(labels):
    changes = {'OPEN': [], 'CLOSE': []}
    for raw in labels:
        label = _normalized_label(raw)
        for kind, pattern in [('OPEN', r'(?:opens|mở cửa (?:lúc|vào))\s+'), ('CLOSE', r'(?:closes|đóng cửa (?:lúc|vào))\s+')]:
            match = re.search(r'(?:^|[·⋅•]\s*)' + pattern + r'(.*)$', label)
            if match:
                value = match[1]
                day = None
                if value.endswith(' tomorrow'):
                    value, day = value[:-9], 'tomorrow'
                clock = _clock(value)
                if clock:
                    changes[kind].append(dict(local_time=clock, day_label=day, at=None, raw_label=raw))
    result = []
    for kind in ('OPEN', 'CLOSE'):
        entries = changes[kind]
        unique = {(entry['local_time'], entry['day_label']) for entry in entries}
        result.append(entries[0] if len(unique) == 1 else None)
    return result


def _entity_location(source_url):
    parts = urlsplit(source_url or '')
    if not re.fullmatch(r'(?:www\.)?google\.(?:com|com\.vn)', parts.hostname or '') or '/maps/place/' not in parts.path:
        return None, None, None, None
    points = set(re.findall(r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)', unquote(source_url)))
    lat = lng = None
    if len(points) == 1:
        candidate_lat, candidate_lng = map(float, next(iter(points)))
        if -90 <= candidate_lat <= 90 and -180 <= candidate_lng <= 180:
            lat, lng = candidate_lat, candidate_lng
    identifier, kind = google_identity(source_url)
    identities = set(re.findall(r'!1s(0x[0-9a-f]+:0x[0-9a-f]+|ChIJ[A-Za-z0-9_-]+)(?=!|/|\?|&|$)', unquote(source_url), re.I))
    if len(identities) != 1:
        identifier = kind = None
    return lat, lng, identifier, kind


def extract_place_details(driver, store_id: str) -> dict:
    """Capture resolved-place facts, with one bounded hours-only expansion."""
    try:
        maps_url = _http_url(driver.current_url)
    except Exception:
        maps_url = None
    rating, review_count = _rating_and_count(driver)
    website = _http_url(_first(driver, ("a[data-item-id='authority']", "a[data-item-id^='authority']"), attribute="href"))
    menu_url = _http_url(_first(driver, ("a[data-item-id*='menu']",), attribute="href"))
    price = _first(driver, ("[aria-label*='giá mỗi người' i]", "[aria-label*='price per person' i]"), attribute="aria-label")
    if price is None:
        for element in _elements(driver, "span.mgr77e"):
            candidate = _text(element)
            if candidate and re.search(r"₫|vnđ|vnd", candidate, re.I):
                price = candidate
                break
    fields = {
        "name": _first(driver, ("h1.DUwDvf", "div[role='main'] h1")),
        "address": _first(driver, ("[data-item-id='address']",)),
        "category": _first(driver, ("button.DkEaL", "[jsaction*='pane.rating.category']")),
        "phone": _phone(driver),
        "website": website,
        "menu_url": menu_url,
        "opening_hours": _hours(driver),
        "rating": rating,
        "review_count": review_count,
        "price_range": price,
        "plus_code": _first(driver, ("[data-item-id='oloc']",)),
    }
    observed_at = datetime.now(timezone.utc).isoformat()
    labels = list(dict.fromkeys((fields['opening_hours'] or {}).get('raw_labels', []) + _status_labels(driver)))
    business_status, open_now, status_conflict = _business_state(labels)
    weekly, complete_week = _weekly_intervals(fields['opening_hours'])
    next_opening, next_closing = _next_changes(labels)
    if status_conflict or business_status in {'CLOSED_TEMPORARILY', 'CLOSED_PERMANENTLY'}:
        next_opening = next_closing = None
    latitude, longitude, identifier, identifier_type = _entity_location(maps_url)
    warnings = [label for label in labels if re.search(
        r'hours.*differ|holiday|special hours|giờ.*khác|ngày lễ', _normalized_label(label))]
    new_fields = dict(business_status=business_status, open_now=open_now,
        weekly_opening_hours=weekly, next_opening_time=next_opening, next_closing_time=next_closing,
        latitude=latitude, longitude=longitude, google_identifier=identifier,
        google_identifier_type=identifier_type,
        google_place_id=identifier if identifier_type == 'place_id' else None)
    return {
        "source": "google_maps_dom",
        "schema_version": 2,
        "store_id": store_id,
        "collected_at": observed_at,
        "observed_at": observed_at,
        "source_url": maps_url,
        "maps_url": maps_url,
        **fields,
        **new_fields,
        "raw_status_labels": labels,
        "special_hours_warnings": warnings,
        "metadata_completeness": {
            "weekly_hours_complete": complete_week,
            "missing_or_unknown": [key for key, value in new_fields.items()
                                   if value is None or value == 'UNKNOWN' or value == []],
        },
        "extraction_evidence": {
            "identity_requirement": "caller-confirmed place entity before extraction",
            "status_conflict": status_conflict,
            "hours_source": "DOM hours control/table; at most one confirmed collapsed-control expansion",
            "hours_expansion": (fields['opening_hours'] or {}).get('expansion'),
            "location_source": "entity !3d/!4d URL components" if latitude is not None else None,
            "identifier_source": "entity URL !1s component" if identifier is not None else None,
            "next_change_policy": "explicit local time only; no inferred date/timezone",
        },
        "visible_detail_items": _visible_detail_items(driver),
        "missing_fields": [key for key, value in fields.items() if value is None],
    }
