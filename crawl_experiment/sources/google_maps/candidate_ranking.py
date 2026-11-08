"""Moderate crawl eligibility, explicitly NOT cross-source identity verification."""
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
import math
import re
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit


@dataclass(frozen=True)
class RankingConfig:
    name_weight: float = .45
    address_weight: float = .40
    distance_weight: float = .15
    eligibility_threshold: float = .65
    house_conflict_penalty: float = .30
    street_conflict_penalty: float = .35
    distance_scale_m: float = 200
    same_street_threshold: float = .80
    minimum_name_support: float = .35
    capture_name_threshold: float = .45
    capture_margin: float = .05
    clear_location_conflict_m: float = 20000
    strong_name_threshold: float = .90
    local_name_threshold: float = .80
    local_distance_m: float = 150

    def __post_init__(self):
        weights=(self.name_weight,self.address_weight,self.distance_weight)
        if any(not math.isfinite(w) or w<0 for w in weights) or sum(weights)<=0:
            raise ValueError('Invalid ranking weights')
        for key in ['eligibility_threshold','same_street_threshold','minimum_name_support',
                    'house_conflict_penalty','street_conflict_penalty','capture_name_threshold','capture_margin',
                    'strong_name_threshold','local_name_threshold']:
            if not 0<=getattr(self,key)<=1: raise ValueError('Invalid ranking threshold/penalty')
        if not math.isfinite(self.distance_scale_m) or self.distance_scale_m<=0:
            raise ValueError('Invalid distance scale')
        if not math.isfinite(self.clear_location_conflict_m) or self.clear_location_conflict_m<=0:
            raise ValueError('Invalid location contradiction distance')
        for value in (self.local_distance_m,):
            if not math.isfinite(value) or value<=0: raise ValueError('Invalid local evidence distance')


def normalize(value):
    if not isinstance(value,str) or not value.strip():return None
    text=unicodedata.normalize('NFKD',value.casefold().replace('đ','d'))
    text=''.join(c for c in text if not unicodedata.combining(c))
    text=re.sub(r'\b(?:duong|street|str|st)\b|\bd\.', ' ',text)
    text=re.sub(r'\b(?:tp\.?|thanh pho)\b',' ',text)
    return ' '.join(re.findall(r'[a-z0-9/]+',text)) or None


def similarity(a,b):
    a,b=normalize(a),normalize(b)
    if not a or not b:return None
    return (SequenceMatcher(None,a,b,autojunk=False).ratio()+SequenceMatcher(None,b,a,autojunk=False).ratio())/2


def business_name_evidence(left, right, threshold=.80):
    """Keep distinctive name tokens when Google adds cuisine/location suffixes."""
    generic = {'quan','an','tiem','nha','hang','cafe','coffee','ca','phe','tra','sua',
               'banh','mi','oc','restaurant','nuong','com','tam','bo','la','lot','chay','lau',
               'milk','tea','milktea','shop','store','bakery','food','beverage','beverages'}
    a, b = normalize(left), normalize(right)
    if not a or not b: return None, False
    tokens_a = set(a.split()) - generic
    tokens_b = set(b.split()) - generic
    core_a=' '.join(token for token in a.split() if token not in generic)
    core_b=' '.join(token for token in b.split() if token not in generic)
    core_score=max(similarity(core_a,core_b) or 0,
                   similarity(' '.join(sorted(tokens_a)),' '.join(sorted(tokens_b))) or 0)
    def matches(x, y):
        return x == y or (len(x) >= 3 and len(y) >= 3 and x[0] == y[0]
                         and similarity(x,y) >= .75)
    coverage = sum(any(matches(x,y) for y in tokens_b) for x in tokens_a) / max(1,len(tokens_a))
    distinctive = bool(tokens_a and tokens_b and coverage >= .65)
    # A long brand token can survive an added marketing phrase on either side.
    long_brand = any(x == y and len(x) >= 6 for x in tokens_a for y in tokens_b)
    exact = a == b
    spacing = a.replace(' ','') == b.replace(' ','')
    numbers_a, numbers_b = set(re.findall(r'\d+',a)), set(re.findall(r'\d+',b))
    number_conflict = bool(numbers_a and numbers_b and numbers_a != numbers_b)
    return max(similarity(a,b), core_score, .92*coverage if distinctive else 0), bool(
        not number_conflict and tokens_a and tokens_b
        and (exact or spacing or distinctive or long_brand or core_score>=threshold))


def ward_name(value):
    """Only explicit ward prefixes establish a ward; never infer it from a city."""
    for part in (value or '').split(','):
        label=normalize(part)
        if not label: continue
        match=re.match(r'^(?:phuong|ward|p\s+)\s*(.+)$',label)
        if match:return match.group(1).strip()
    return None


def ward_evidence(store,address):
    expected=normalize(getattr(store,'expected_ward',None))
    if expected:
        expected=re.sub(r'^(?:phuong|ward|p)\s+','',expected)
    expected=expected or ward_name(getattr(store,'geographic_context',None)) or ward_name(store.expected_address)
    actual=ward_name(address)
    segments=[normalize(part) for part in (address or '').split(',')]
    # Google often omits the administrative prefix in comma-separated fields.
    same=bool(expected and (actual==expected or (actual is None and expected in segments[1:])))
    conflict=bool(expected and actual and expected!=actual)
    return expected,actual or (expected if same else None),same,conflict


def address_parts(value,locality=None):
    if not isinstance(value,str) or not value.strip():return None,None
    cleaned=re.sub(r'^[^\w]+','',value.strip())
    fields=cleaned.split(',')
    first=fields[0].strip()
    if normalize(first) in {'mat tien','dia chi','address'} and len(fields)>1:
        first=fields[1].strip()
    elif re.fullmatch(r'\d+[a-zA-Z]?\s*[-–]\s*\d+[a-zA-Z]?',first) and len(fields)>1:
        first+=' '+fields[1].strip()
    elif re.fullmatch(r'\d+[a-zA-Z]?',first) and len(fields)>1:
        first+=' '+fields[1].strip()
    if re.fullmatch(r'[A-Z0-9]+\+[A-Z0-9]+',first.strip()) and len(fields)>1:
        first=fields[1].strip()
    first=re.split(r'\s+-\s+(?:[Pp]hường|[Qq]uận|[Ww]ard|[Dd]istrict)\b',first)[0]
    descriptive=bool(re.match(r'(?i)^(?:trước cửa nhà|truoc cua nha|đối diện|doi dien|gần|gan)\s+',first))
    first=re.sub(r'(?i)^(?:trước cửa nhà|truoc cua nha|đối diện|doi dien|gần|gan)\s+','',first)
    # Ranges and descriptive/alley prefixes remain uncertain house numbers.
    range_match=re.match(r'^\s*\d+[a-zA-Z]?\s*[-–]\s*\d+[a-zA-Z]?\s+',first)
    if range_match:return None,normalize(first[range_match.end():])
    match=re.match(r'^\s*(\d+[a-zA-Z]?(?:/\d+[a-zA-Z]?)*)(?:\s+)(.+)$',first)
    house=match.group(1).casefold() if match and not descriptive else None
    street=normalize(match.group(2) if match else first)
    if locality and street==normalize(locality) and house is None and not re.search(r'(?i)đường|duong|đ\.|d\.',first):
        return None,None  # Bare locality is not an inferred street address.
    if street and re.search(r'\b(?:phuong|quan|ward|district|ho chi minh)\b',street):
        return house,None
    if street:
        street=re.sub(r'\bql\s*(\d+)',r'quoc lo \1',street)
    return house,street


def candidate_id(candidate):
    url=str(candidate.get('href') or '')
    match=re.search(r'!1s([^!/?&]+)',unquote(url))
    if match:return match.group(1)
    parts=urlsplit(url)
    return urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))


def valid_candidate_url(url):
    parts=urlsplit(str(url or ''))
    return bool(parts.scheme=='https' and re.fullmatch(r'(?:www\.)?google\.(?:com|com\.vn)',parts.hostname or '')
                and '/maps/place/' in parts.path)


def evaluate(store,candidate,config=None):
    config=config or RankingConfig()
    raw_name=similarity(store.name,candidate.get('text'))
    name,name_supported=business_name_evidence(store.name,candidate.get('text'),config.local_name_threshold)
    expected_ward,google_ward,same_ward,ward_conflict=ward_evidence(store,candidate.get('address'))
    lh,ls=address_parts(store.expected_address,expected_ward);rh,rs=address_parts(candidate.get('address'),google_ward or expected_ward)
    street_similarity=similarity(ls,rs)
    left_street_numbers=set(re.findall(r'\d+',ls or ''))
    right_street_numbers=set(re.findall(r'\d+',rs or ''))
    street_number_conflict=bool(left_street_numbers and right_street_numbers
                                and left_street_numbers!=right_street_numbers)
    same_street=bool(street_similarity is not None and street_similarity>=config.same_street_threshold
                     and not street_number_conflict)
    street_conflict=bool(street_number_conflict or (ls and rs and len(ls.split())>=2 and len(rs.split())>=2
                         and street_similarity is not None and street_similarity<.40))
    nested_house=bool(lh and rh and (lh.endswith('/'+rh) or rh.endswith('/'+lh)))
    house_conflict=bool(lh and rh and lh!=rh and same_street and not nested_house)
    address=None
    if ls and rs:
        address=(1.0 if lh and rh and lh==rh else .85) if same_street else street_similarity
    # Use entity coordinates only, never viewport @lat,lng.
    from .place_details import _entity_location
    lat,lon,_,_=_entity_location(candidate.get('href'))
    distance=None
    if all(v is not None for v in [store.expected_latitude,store.expected_longitude,lat,lon]):
        a,b=map(math.radians,[store.expected_latitude,lat]);d=math.radians(lon-store.expected_longitude)
        h=math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(d/2)**2
        distance=12742017.6*math.asin(math.sqrt(min(1,max(0,h))))
    geographic=max(0,1-distance/config.distance_scale_m) if distance is not None else None
    location_conflict=bool(distance is not None and distance>config.clear_location_conflict_m)
    signals=[(name,config.name_weight),(address,config.address_weight),(geographic,config.distance_weight)]
    denominator=sum(w for value,w in signals if value is not None)
    base=sum(value*w for value,w in signals if value is not None)/denominator if denominator else None
    penalty=config.house_conflict_penalty*house_conflict+config.street_conflict_penalty*street_conflict
    score=max(0,base-penalty) if base is not None else None
    # A distant point is a known zero, not corroboration for a name-only match.
    support=(address is not None and address>=.5) or (geographic is not None and geographic>0)
    eligible=bool(valid_candidate_url(candidate.get('href')) and score is not None
                  and score>=config.eligibility_threshold and name is not None
                  and name>=config.minimum_name_support and name_supported and support
                  and not street_conflict and not house_conflict and not location_conflict)
    # Strong names remain searchable when address/location evidence is absent.
    if (valid_candidate_url(candidate.get('href')) and name_supported and name is not None
            and name>=config.strong_name_threshold and not street_conflict and not house_conflict
            and not location_conflict and address is None):
        eligible=True
    nearby=distance is not None and distance<=config.local_distance_m
    ward_name_eligible=bool(valid_candidate_url(candidate.get('href')) and same_ward
        and name_supported and name is not None and name>=config.local_name_threshold
        and not ward_conflict)
    local_variant=bool(valid_candidate_url(candidate.get('href')) and name_supported
        and name is not None and name>=config.local_name_threshold
        and (not street_conflict or name>=config.strong_name_threshold)
        and not house_conflict and not street_number_conflict and not location_conflict
        and not ward_conflict and nearby)
    # A collection exception, not an ER match. Retain contradictory raw address
    # evidence instead of pretending the two street observations agree.
    eligible=bool((eligible or local_variant or ward_name_eligible) and not ward_conflict)
    reasons=[]
    if not name_supported: reasons.append('NO_DISTINCTIVE_NAME_SUPPORT')
    if street_conflict: reasons.append('STREET_CONFLICT')
    if house_conflict: reasons.append('HOUSE_NUMBER_CONFLICT')
    if location_conflict: reasons.append('CLEAR_LOCATION_CONFLICT')
    if ward_conflict: reasons.append('EXPLICIT_WARD_CONFLICT')
    if not support: reasons.append('ADDRESS_AND_PROXIMITY_UNKNOWN_OR_UNSUPPORTIVE')
    if score is None or score<config.eligibility_threshold: reasons.append('SCORE_BELOW_COLLECTION_THRESHOLD')
    return dict(candidate_id=candidate_id(candidate),name=candidate.get('text'),address=candidate.get('address'),
                url=candidate.get('href'),name_similarity=name,raw_name_similarity=raw_name,
                distinctive_name_supported=name_supported,address_compatibility=address,
                distance_m=distance,geographic_score=geographic,available_weight=denominator,
                house_number_left=lh,house_number_right=rh,same_street=same_street,
                house_suffix_compatible_unverified=nested_house,
                house_conflict=house_conflict,street_conflict=street_conflict,
                location_conflict=location_conflict,penalty=penalty,
                expected_ward=expected_ward,google_ward=google_ward,same_ward=same_ward,
                ward_conflict=ward_conflict,local_address_variant_eligible=local_variant,
                same_ward_name_eligible=ward_name_eligible,
                local_evidence_basis=('SAME_WARD_NAME_FOR_COLLECTION' if ward_name_eligible else
                                      'NEAR_SOURCE_POINT_WARD_NOT_CONFIRMED' if nearby else None),
                base_score=base,score=score,crawl_eligible=eligible,identity_status='IDENTITY_UNVERIFIED',
                unknown_signals=[key for key,value in [('name',name),('address',address),('distance',geographic)] if value is None],
                rejection_reasons=[] if eligible else reasons,
                reason=('SAME_WARD_NAME_FOR_COLLECTION' if ward_name_eligible else
                        'LOCAL_NAME_ADDRESS_VARIANT_FOR_COLLECTION' if local_variant else
                        'MODERATE_CRAWL_ELIGIBILITY') if eligible else 'INSUFFICIENT_OR_CONTRADICTING_EVIDENCE')


def rank(store,candidates,config=None):
    config=config or RankingConfig();unique={}
    for candidate in candidates:
        if not valid_candidate_url(candidate.get('href')):continue
        unique[candidate_id(candidate)]=candidate
    rows=[(candidate,evaluate(store,candidate,config)) for candidate in unique.values()]
    rows.sort(key=lambda item:(-(item[1]['score'] if item[1]['score'] is not None else -1),item[1]['candidate_id']))
    for i,(_,evidence) in enumerate(rows,1): evidence['rank']=i
    eligible=[row for row in rows if row[1]['crawl_eligible']]
    selected=eligible[0] if eligible else None
    return rows,selected
