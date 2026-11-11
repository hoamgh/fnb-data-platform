import html
import re
from urllib.parse import urlparse

from crawl_experiment.core.errors import ReviewParseError
from crawl_experiment.core.models import Review

from . import selectors
from .deadline import StoreAttemptTimeout


def _attribute(element, name, check):
    check()
    return element.get_attribute(name)


def _find(card, selector, check):
    check()
    return card.find_elements("css selector", selector)


def _text(card, selector: str, check=lambda: None) -> str | None:
    elements = _find(card, selector, check)
    if not elements:
        return None
    check()
    return (elements[0].text or "").strip() or None


def _review_url(card, check=lambda: None) -> str | None:
    for element in _find(card, selectors.REVIEW_URL, check):
        url = (_attribute(element, "href", check) or "").strip()
        if url and ("/maps/reviews/" in url or "review_id=" in url or "reviewId=" in url):
            return url
    return None


def _reviewer_metadata(text: str | None) -> dict:
    """Parse only explicit English/Vietnamese labels; retain unknown text raw."""
    result = dict(reviewer_metadata_raw=text, reviewer_review_count=None,
                  reviewer_photo_count=None, reviewer_is_local_guide=None)
    if not text:
        return result
    for field, label in (("reviewer_review_count", r"reviews?|bài đánh giá|đánh giá"),
                         ("reviewer_photo_count", r"photos?|ảnh")):
        match = re.search(r"(?<![\w.,])([0-9]+(?:[, .][0-9]{3})*)\s+(?:" + label + r")\b", text, re.I)
        if match:
            result[field] = int(re.sub(r"[, .]", "", match.group(1)))
    if re.search(r"\b(?:Local Guide|Hướng dẫn viên địa phương)\b", text, re.I):
        result["reviewer_is_local_guide"] = True
    return result


def _reviewer_fields(card, check=lambda: None) -> dict:
    result = _reviewer_metadata(_text(card, selectors.REVIEWER_METADATA, check))
    result.update(reviewer_profile_url=None, reviewer_id=None, reviewer_avatar_url=None)
    for element in _find(card, selectors.REVIEWER_PROFILE, check):
        url = html.unescape(_attribute(element, "data-href", check) or _attribute(element, "href", check) or "").strip()
        parsed = urlparse(url)
        match = re.fullmatch(r"/maps/contrib/([0-9]+)(?:/.*)?", parsed.path)
        if parsed.scheme in {"http", "https"} and parsed.hostname and (parsed.hostname == "google.com" or parsed.hostname.endswith(".google.com")) and match:
            result.update(reviewer_profile_url=url, reviewer_id=match.group(1))
            break
    for element in _find(card, selectors.REVIEWER_AVATAR, check):
        url = html.unescape(_attribute(element, "src", check) or "").strip()
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            result["reviewer_avatar_url"] = url
            break
    return result


def _image_urls(card, check=lambda: None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for element in _find(card, selectors.REVIEW_MEDIA, check):
        resolved_src = _attribute(element, "src", check)
        if resolved_src:
            candidates = [resolved_src]
        else:
            candidates = [
                _attribute(element, "data-src", check),
                _attribute(element, "data-lazy-src", check),
                _attribute(element, "data-original", check),
            ]
            style = _attribute(element, "style", check) or ""
            candidates.extend(re.findall(r"url\((?:['\"])?(.*?)(?:['\"])?\)", style))
        for candidate in candidates:
            url = html.unescape(candidate or "").strip().strip("'\"")
            parsed = urlparse(url)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.netloc
                and url not in seen
            ):
                seen.add(url)
                urls.append(url)
    return urls


class ReviewExtractor:
    def extract(self, card, store_id: str) -> Review:
        check = getattr(self, 'check_deadline', lambda: None)
        try:
            review_id = (_attribute(card, selectors.REVIEW_ID_ATTRIBUTE, check) or "").strip()
            if not review_id:
                raise ReviewParseError("review card has no data-review-id")

            ratings = _find(card, selectors.RATING, check)
            rating_label = _attribute(ratings[0], "aria-label", check) if ratings else None
            match = re.search(r"([0-5](?:\.\d+)?)", rating_label or "")
            review = Review(
                source="google_maps",
                source_review_id=review_id,
                store_id=store_id,
                author=_text(card, selectors.AUTHOR, check),
                rating=float(match.group(1)) if match else None,
                text=_text(card, selectors.TEXT, check),
                displayed_date=_text(card, selectors.DATE, check),
                owner_response=_text(card, selectors.OWNER_RESPONSE, check),
                review_url=_review_url(card, check),
                image_urls=_image_urls(card, check),
                **_reviewer_fields(card, check),
            )
            collector = getattr(self, "timestamp_collector", None)
            if collector is not None:
                try:
                    return collector.enrich(review)
                except Exception:
                    # Timestamp enrichment must never turn a valid DOM review into a parse failure.
                    from dataclasses import replace
                    return replace(review, timestamp_status="PARSE_FAILED", timestamp_source="google_maps_network")
            return review
        except (ReviewParseError, StoreAttemptTimeout):
            raise
        except Exception as exc:
            raise ReviewParseError(str(exc)) from exc
