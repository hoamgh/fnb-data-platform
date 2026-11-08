from __future__ import annotations

import logging
from selenium.common.exceptions import StaleElementReferenceException
import math
import re
import unicodedata
from difflib import SequenceMatcher
import time
from enum import StrEnum
from urllib.parse import quote_plus, urlsplit

from crawl_experiment.core.errors import (
    ChallengeError,
    LimitedViewError,
    NavigationError,
    PlaceResolutionError,
    RateLimitedError,
)
from crawl_experiment.core.models import Store

from . import selectors
from .candidate_ranking import RankingConfig, candidate_id, evaluate, rank
from .health import HealthState, PageEvidence, classify_page

logger = logging.getLogger(__name__)


class NavigationState(StrEnum):
    SEARCH_RESULTS = "SEARCH_RESULTS"
    SEARCH_PREVIEW = "SEARCH_PREVIEW"
    PLACE_ENTITY = "PLACE_ENTITY"


class GoogleMapsNavigator:
    def __init__(
        self,
        driver,
        *,
        base_url: str = "https://www.google.com",
        resolve_timeout_seconds: float = 8,
        poll_interval: float = 0.25,
        max_search_scrolls: int = 5,
        search_idle_limit: int = 2,
        ranking_config: RankingConfig | None = None,
        max_candidate_opens: int = 3,
        inspection_name_threshold: float = 0.55,
        clock=time.monotonic,
        sleeper=time.sleep,
    ):
        self.driver = driver
        self.base_url = base_url.rstrip("/")
        self.resolve_timeout_seconds = resolve_timeout_seconds
        self.poll_interval = poll_interval
        if max_search_scrolls < 0 or max_candidate_opens < 1 or not 0 <= inspection_name_threshold <= 1:
            raise ValueError("Invalid bounded search limits")
        self.max_search_scrolls = max_search_scrolls
        if search_idle_limit < 1: raise ValueError("search_idle_limit must be positive")
        self.search_idle_limit=search_idle_limit
        self.ranking_config=ranking_config or RankingConfig()
        self.max_candidate_opens = max_candidate_opens
        self.inspection_name_threshold = inspection_name_threshold
        self.clock = clock
        self.sleeper = sleeper
        self.last_resolution_diagnostics: dict[str, object] = {}

    def warm_up(self) -> None:
        try:
            self.driver.get(self.base_url)
        except Exception as exc:
            raise NavigationError(f"google.com warm-up failed: {exc}") from exc

    def _check_access(self):
        url=self.driver.current_url
        body=self.driver.find_element("tag name","body").text
        evidence=self._page_evidence()
        state=classify_page(url,body,evidence=evidence)
        if state is HealthState.RATE_LIMITED:
            raise RateLimitedError("Google rate limit page")
        if state is HealthState.CHALLENGE:
            raise ChallengeError("Google challenge page")
        return url,evidence

    def open_store(self, store: Store) -> str:
        from dataclasses import asdict
        self.last_resolution_diagnostics=dict(search_scroll_count=0,search_idle_count=0,
            candidate_attempts=[],candidate_rankings=[],place_entity_confirmed=False,
            crawl_eligible=False,identity_status='IDENTITY_UNVERIFIED',
            ranking_policy=asdict(self.ranking_config),selection_policy_version='source-neutral-core-name-ward-v5')
        if not store.search_eligible:
            self.last_resolution_diagnostics.update(resolution_outcome='UNRESOLVED',resolution_reason=store.search_reason)
            raise PlaceResolutionError(store.search_reason or 'INSUFFICIENT_SEARCH_INFORMATION')
        search_started=self.clock()
        initial_url=f"{self.base_url}/maps/search/{quote_plus(store.query)}/"
        seen={};idle=0;after_scroll=False;selected=None
        try:
            self.driver.get(initial_url)
            self.last_resolution_diagnostics['initial_url']=self.driver.current_url
            callback=getattr(self,'after_search_navigation',None)
            if callback:callback()
            deadline=self.clock()+self.resolve_timeout_seconds
            while True:
                url,evidence=self._check_access()
                before=len(seen)
                for candidate in self._candidate_details():
                    key=candidate_id(candidate)
                    # Retain richer observations when virtualized cards reappear.
                    previous=seen.get(key,{})
                    seen[key]={**previous,**candidate,'address':candidate.get('address') or previous.get('address')}
                feeds=self.driver.find_elements('css selector',selectors.SEARCH_RESULTS_FEED)
                if not feeds and '/maps/place/' in url and self._place_title():
                    direct=dict(text=self._place_title(),address=self._place_address(),href=url)
                    seen[candidate_id(direct)]=direct
                    break
                if after_scroll:
                    idle=idle+1 if len(seen)==before else 0
                    self.last_resolution_diagnostics['search_idle_count']=idle
                    after_scroll=False
                if idle>=self.search_idle_limit or self.last_resolution_diagnostics['search_scroll_count']>=self.max_search_scrolls:
                    break
                if feeds:
                    self.driver.execute_script(selectors.SEARCH_SCROLL_SCRIPT,feeds[0])
                    self.last_resolution_diagnostics['search_scroll_count']+=1
                    growth_deadline=self.clock()+min(1.5,self.resolve_timeout_seconds)
                    while self.clock()<growth_deadline:
                        self.sleeper(self.poll_interval)
                        self._check_access()
                        if any(candidate_id(c) not in seen for c in self._candidate_details()):break
                    after_scroll=True
                    continue
                if self.clock()>=deadline:break
                self.sleeper(self.poll_interval)
            candidates=list(seen.values())
            ranked,_=rank(store,candidates,self.ranking_config)
            self.last_resolution_diagnostics.update(search_candidate_count=len(seen),
                candidate_rankings=[details for _,details in ranked])
            # Restore the historical virtual hooks (including CaptureNavigator).
            candidate=self._best_exact_candidate(store,candidates)
            selected=candidate
            remaining=list(candidates)
            opens=0
            while candidate and opens<self.max_candidate_opens:
                opens+=1
                remaining=[c for c in remaining if candidate_id(c)!=candidate_id(candidate)]
                self.last_resolution_diagnostics.pop('resolution_outcome',None)
                decision=evaluate(store,candidate,self.ranking_config)
                decision['selection_method']=self.last_resolution_diagnostics.get('selection_method','SUBCLASS_SELECTION')
                decision['score_crawl_eligible']=decision['crawl_eligible']
                decision['crawl_eligible']=True
                self.last_resolution_diagnostics['selected_candidate']=decision
                self.last_resolution_diagnostics['selection_attempt']=decision
                if self.driver.current_url!=candidate['href']:self.driver.get(candidate['href'])
                deadline=self.clock()+self.resolve_timeout_seconds
                while True:
                    url,evidence=self._check_access()
                    resolved=self._resolved_place(store,url,evidence,candidates)
                    title=self._place_title();address=self._place_address()
                    observed=evaluate(store,dict(text=title,address=address,href=url),self.ranking_config)
                    original_id=decision['candidate_id'];actual_id=observed['candidate_id']
                    id_conflict=original_id.startswith(('0x','ChIJ')) and actual_id.startswith(('0x','ChIJ')) and original_id!=actual_id
                    if resolved is not None:
                        self.last_resolution_diagnostics['candidate_attempts'].append(dict(
                            observed,selected_candidate_id=original_id,identifier_conflict=id_conflict))
                        if not id_conflict:
                            resolved.update(identity_status='IDENTITY_UNVERIFIED',crawl_eligible=True,
                                resolution_outcome='IDENTITY_UNVERIFIED',
                                resolution_reason='SELECTED_FOR_COLLECTION_NOT_ER_VERIFIED',
                                selection_score=observed['score'])
                            self.last_resolution_diagnostics['search_elapsed_seconds']=self.clock()-search_started
                            return self._record_resolution(resolved,candidates)
                        break
                    if self.last_resolution_diagnostics.get('resolution_outcome')=='WRONG_BRANCH':break
                    if self.clock()>=deadline:break
                    self.sleeper(self.poll_interval)
                self.last_resolution_diagnostics['candidate_attempts'].append(dict(observed,
                    selected_candidate_id=original_id,rejection_reason='DETAIL_NOT_ELIGIBLE_OR_NOT_READY'))
                candidate=self._best_exact_candidate(store,remaining)
        except (LimitedViewError,RateLimitedError,ChallengeError):raise
        except Exception as exc:
            raise NavigationError(f"Maps navigation failed: {exc}") from exc
        self.last_resolution_diagnostics['search_elapsed_seconds']=self.clock()-search_started
        self.last_resolution_diagnostics.update(navigation_state=str(NavigationState.SEARCH_RESULTS if seen else NavigationState.SEARCH_PREVIEW),
            selected_candidate=None,place_entity_confirmed=False,crawl_eligible=False,
            resolution_outcome='UNRESOLVED',resolution_reason='NO_ELIGIBLE_CANDIDATE' if not selected else 'SELECTED_ENTITY_REJECTED')
        raise PlaceResolutionError(f"No eligible Google candidate; diagnostics={self.last_resolution_diagnostics}")

    def _record_resolution(self,resolved,candidates):
        self.last_resolution_diagnostics.update(resolved,navigation_state=str(NavigationState.PLACE_ENTITY),
                                                search_candidate_count=len(candidates))
        return self.driver.current_url

    def _rank_inspection_candidates(self,store,candidates):
        expected=self._identity_text(store.name)
        tokens=set(re.findall(r'[a-z0-9]+',expected))
        ranked=[]
        for candidate in candidates:
            parts=urlsplit(str(candidate.get('href') or ''))
            if not re.fullmatch(r'(?:www\.)?google\.(?:com|com\.vn)',parts.hostname or '') or '/maps/place/' not in parts.path:
                continue
            actual=self._identity_text(str(candidate.get('text') or ''))
            forward=SequenceMatcher(None,expected,actual,autojunk=False).ratio()
            reverse=SequenceMatcher(None,actual,expected,autojunk=False).ratio()
            score=(forward+reverse)/2
            related=bool(tokens & set(re.findall(r'[a-z0-9]+',actual)))
            if self._name_match(store.name,candidate.get('text')) is None and not (score>=self.inspection_name_threshold and related):
                continue
            address_match=bool(store.expected_address and self._address_matches(store.expected_address,str(candidate.get('address') or '')))
            ranked.append((address_match,score,candidate))
        return [c for _,_,c in sorted(ranked,key=lambda x:(-int(x[0]),-x[1],x[2]['href']))]

    @staticmethod
    def _normalized(value: str | None) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))

    def _place_title(self) -> str | None:
        elements = self.driver.find_elements("css selector", selectors.PLACE_TITLE)
        for element in elements:
            text = (getattr(element, "text", "") or element.get_attribute("aria-label") or "").strip()
            if text:
                return text
        return None

    def _place_address(self) -> str | None:
        elements = self.driver.find_elements("css selector", selectors.PLACE_ADDRESS)
        for element in elements:
            text = (getattr(element, "text", "") or element.get_attribute("aria-label") or "").strip()
            if text:
                return text
        return None

    def _candidate_details(self) -> list[dict[str, object]]:
        candidates = []
        seen = set()
        for element in self.driver.find_elements(
            "css selector", selectors.SEARCH_RESULT_CANDIDATES
        ):
            try:
                href = element.get_attribute("href") or ""
                text = (
                    element.get_attribute("aria-label")
                    or getattr(element, "text", "")
                    or ""
                ).strip()
                address = (element.get_attribute("data-address") or "").strip() or None
                # Address often belongs to the parent card, not its overlay link.
                details=self.driver.execute_script(selectors.SEARCH_CARD_DETAILS_SCRIPT,element)
                raw_label=text
                if isinstance(details,dict):
                    text=str(details.get('name') or '').strip() or text
                    address=address or (str(details.get('address') or '').strip() or None)
                    if not details.get('name') and not raw_label and details.get('text'):
                        text=str(details['text']).splitlines()[0].strip()
                    if not address:
                        # Numbered line only, never reuse category/rating/locality as an address.
                        lines=[line.strip() for line in (details.get('lines') or [])+str(details.get('text') or '').splitlines()]
                        segments=[part.strip() for line in lines for part in re.split(r'[·•]',line)]
                        address=next((line for line in segments
                            if re.match(r'^\d+[a-zA-Z]?(?:/\d+[a-zA-Z]?)*\s+[^\d]',line)
                            and not re.search(r'(?i)reviews?|đánh giá|stars?|sao\b|minutes?|phút|giờ',line)),None)
                # Overlay labels may contain the entire card. Structured heading
                # wins; otherwise retain only its first visible name line.
                text=text.splitlines()[0] if text else text
                text=re.split(r'\s*[·•]\s*(?:Đường liên kết đã truy cập|Đường liên kết đã truy cập|Visited link|\d[.,]\d|Đóng cửa|Permanently closed)\b',text,flags=re.I)[0].strip()
                key = (href, text, address)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {"element": element, "href": href, "text": text, "address": address,"raw_label":raw_label}
                )
            except StaleElementReferenceException:
                continue
        return candidates

    def _best_exact_candidate(
        self, store: Store, candidates: list[dict[str, object]]
    ) -> dict[str, object] | None:
        # Reuse CaptureNavigator's rank + clear-winner pattern, with relevance
        # and corroboration checks so a lone unrelated candidate is not forced.
        evidence=[(c,evaluate(store,c,self.ranking_config)) for c in candidates
                  if not c.get('href') or self._valid_url(c.get('href'))]
        evidence=[(c,e) for c,e in evidence if (e['distinctive_name_supported'] or self._name_match(store.name,str(c.get('text') or '')) is not None) and (not e['location_conflict'] or e['same_ward_name_eligible']) and not e['ward_conflict']]
        exact=[c for c,e in evidence if self._name_match(store.name,str(c.get('text') or '')) is not None
               and store.expected_address and self._address_matches(store.expected_address,str(c.get('address') or ''))]
        if len(exact)>1:
            self.last_resolution_diagnostics['selection_method']='NO_CLEAR_CANDIDATE'
            return None
        if len(exact)==1:
            self.last_resolution_diagnostics['selection_method']='EXACT_CANDIDATE_FOR_REVIEW'
            return exact[0]
        rows,selected=rank(store,[c for c,_ in evidence],self.ranking_config)
        if selected:
            eligible=[item for item in rows if item[1]['crawl_eligible']]
            if len(eligible)>1 and eligible[0][1]['score']-eligible[1][1]['score']<self.ranking_config.capture_margin:
                self.last_resolution_diagnostics['selection_method']='NO_CLEAR_CANDIDATE'
                return None
            self.last_resolution_diagnostics['selection_method']='COMPATIBLE_CANDIDATE_FOR_REVIEW'
            return selected[0]
        ordered=sorted(evidence,key=lambda item:-(item[1]['name_similarity'] or 0))
        if not ordered:return None
        c,e=ordered[0]
        gap=(e['name_similarity'] or 0)-(ordered[1][1]['name_similarity'] or 0) if len(ordered)>1 else 1
        support=e['same_street'] or (e['distance_m'] is not None and e['distance_m']<=self.ranking_config.distance_scale_m)
        if ((e['name_similarity'] or 0)>=self.ranking_config.capture_name_threshold
                and gap>=self.ranking_config.capture_margin and support
                and not e['house_conflict'] and not e['street_conflict'] and not e['location_conflict']):
            self.last_resolution_diagnostics['selection_method']='RANKED_CANDIDATE_FOR_REVIEW'
            return c
        self.last_resolution_diagnostics['selection_method']='NO_CLEAR_CANDIDATE'
        return None

    @staticmethod
    def _valid_url(url):
        from .candidate_ranking import valid_candidate_url
        return valid_candidate_url(url)

    def _inspection_candidate(self, store, candidates):
        """Open a unique name candidate for evidence only; never a branch match."""
        generic={'quan','an','tiem','nha','hang','cafe','coffee','tra','sua','banh','mi','oc','restaurant'}
        tokens=set(re.findall(r'[a-z]+',self._identity_text(store.name)))
        matches=[c for c in candidates if self._name_match(store.name,str(c.get('text') or '')) is not None]
        return matches[0] if len(matches)==1 and tokens-generic else None

    @staticmethod
    def _identity_text(value: str | None) -> str:
        text = unicodedata.normalize('NFKD', (value or '').casefold().replace('đ', 'd'))
        return ''.join(char for char in text if not unicodedata.combining(char))

    def _name_match(self, expected: str | None, actual: str | None) -> str | None:
        left = re.findall(r'[a-z0-9]+', self._identity_text(expected))
        right = re.findall(r'[a-z0-9]+', self._identity_text(actual))
        if not left or not right:
            return None
        if left == right:
            return 'EXACT'
        a, b = ''.join(left), ''.join(right)
        if a == b:
            return 'SPACING_VARIANT'
        # Similar spellings must not erase numbered branch/name conflicts.
        numbers_a, numbers_b = set(re.findall(r'\d+', a)), set(re.findall(r'\d+', b))
        if numbers_a and numbers_b and numbers_a != numbers_b:
            return None
        short, long = sorted((left, right), key=len)
        generic = {'quan', 'an', 'tiem', 'nha', 'hang', 'cafe', 'coffee', 'tra', 'sua',
                   'banh', 'mi', 'oc', 'pizza', 'restaurant', 'shop', 'store'}
        distinctive = [token for token in short if token not in generic and not token.isdigit()]
        if long[:len(short)] == short and distinctive:
            if len(short) >= 2 or len(short[0]) >= 6:
                return 'NAME_WITH_QUALIFIER'
        if min(len(a), len(b)) >= 8 and SequenceMatcher(None, a, b).ratio() >= 0.94:
            return 'HIGH_SIMILARITY'
        return None

    def _address_matches(self, expected: str, actual: str) -> bool:
        # Require a numbered street, comparing whole components. Locality-only
        # fallback addresses cannot identify a branch.
        def components(value):
            text = self._identity_text(value).split(',', 1)[0]
            text = re.sub(r'\s+-\s+(?:phuong|quan|ward|district)\b.*$', '', text)
            normalized = " ".join(re.findall(r"[a-z0-9/]+", text))
            normalized = re.sub(r"^(?:address|dia chi)\s+", "", normalized)
            tokens = normalized.split()
            number = re.match(r"^(\d+[a-z]?(?:/\d+[a-z]?)*)(?:\s+|$)", normalized)
            if not number or len(tokens) < 2:
                return None
            street = tokens[1:]
            if street and street[0] in {'d', 'duong'}:
                street = street[1:]
            return (number.group(1), street) if street else None

        left, right = components(expected), components(actual)
        if left is None or right is None or left[0] != right[0]:
            return False
        # Compare the complete street component; comma-separated locality
        # suffixes do not substitute for branch evidence.
        return left[1] == right[1]

    def _resolved_place(
        self,
        store: Store,
        current_url: str,
        evidence: PageEvidence,
        candidates: list[dict[str, object]],
    ) -> dict[str, object] | None:
        title=self._place_title();address=self._place_address()
        entity=bool(title and evidence and evidence.has_place_shell
            and self.driver.find_elements('css selector',selectors.PLACE_DETAIL_PANE)
            and self.driver.find_elements('css selector',selectors.PLACE_ENTITY_MARKER)
            and self._valid_url(current_url))
        if not entity:return None
        observed=dict(text=title,address=address,href=current_url)
        decision=evaluate(store,observed,self.ranking_config)
        # Detail pages may expose a contradiction absent from the search card.
        selected=self._best_exact_candidate(store,[observed])
        if selected is None:
            contradiction=decision['house_conflict'] or decision['street_conflict'] or decision['location_conflict'] or decision['ward_conflict']
            self.last_resolution_diagnostics.update(resolution_outcome='WRONG_BRANCH' if contradiction else 'REVIEW_REQUIRED',
                resolution_reason='DETAIL_EVIDENCE_INSUFFICIENT',coordinate_distance_m=decision['distance_m'])
            return None
        duplicate_branches=[c for c in candidates if store.expected_address
            and self._name_match(store.name,str(c.get('text') or '')) is not None
            and self._address_matches(store.expected_address,str(c.get('address') or ''))]
        if len(duplicate_branches)>1:return None
        return dict(resolved_place_title=title,resolved_place_address=address,resolved_url=current_url,
                    place_entity_confirmed=True,identity_status='IDENTITY_UNVERIFIED',
                    identity_name_match=self._name_match(store.name,title) or 'RANKED_NAME_VARIANT',
                    identity_address_match=self._address_matches(store.expected_address or '',address or ''),
                    coordinate_distance_m=decision['distance_m'],crawl_eligible=True,
                    resolution_outcome='IDENTITY_UNVERIFIED')

    def _page_evidence(self) -> PageEvidence:
        return PageEvidence(
            has_place_tabs=bool(
                self.driver.find_elements("css selector", selectors.PLACE_TABS)
            ),
            has_review_ui=bool(
                self.driver.find_elements("css selector", selectors.REVIEWS_TAB_CSS)
                or self.driver.find_elements("css selector", selectors.REVIEW_CARD)
            ),
            has_place_shell=bool(
                self.driver.find_elements("css selector", selectors.PLACE_SHELL)
            ),
            has_sign_in_prompt=bool(
                self.driver.find_elements("css selector", selectors.SIGN_IN_PROMPT)
            ),
        )
