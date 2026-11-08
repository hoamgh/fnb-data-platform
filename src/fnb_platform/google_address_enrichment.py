"""Google-first observed address features and provisional ER-V3 attachments, offline."""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import google_silver as silver
from .comparison_features import comparison_text, parse_address, visible_text
from .overture_incremental import _digest

VERSION='google-address-enrichment-v1'
ER_VERSION='ov-google-er-proposal-v3-experimental'
KEY=('google_id_kind','google_identity_value')
LOCATOR=r'(?:[A-Za-z]?\d+[A-Za-z0-9]*(?:/\d+[A-Za-z0-9]*)*)(?:\s*[Bb][Ii][Ss])?'
ROAD=r'(?:đường|duong|đ\.|d\.|street)\s*'
PREMISE=r'(?:cư xá|cu xa|chung cư|chung cu|tòa nhà|toa nha|tttm|building|block|lô|lo)\b'
PLUS=r'^[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,}(?:\s|$)'
PARSED_FIELDS=['house_identifier','alley_identifier','lot_identifier','street_name',
    'normalized_street_name','ward_name','city_name','legacy_district']
ADDRESS_FIELDS=PARSED_FIELDS+['address_parse_status','street_status','ward_evidence_kind',
    'locality_name_raw','street_name_raw','house_kind']
PLACE_SCHEMA=pa.schema([(k,pa.string()) for k in [*KEY,'google_name','address_raw',*ADDRESS_FIELDS,
    'street_dictionary_id','observed_at','source_run','address_provenance_json','parser_evidence_json','rule_version']]
    +[('street_analysis_eligible',pa.bool_()),('shared_by_overture_ids',pa.bool_()),
      ('parse_notes',pa.list_(pa.string()))])
LINK_SCHEMA=pa.schema([(k,pa.string()) for k in ['overture_id','overture_name_raw','overture_address_raw',
    *KEY,'enrichment_status','preferred_address_source','preferred_address_raw',*PARSED_FIELDS,
    'address_parse_status','er_status','er_classification','er_rule_version','candidate_relationships_json',
    'provenance_json','rule_version']]+[('street_analysis_eligible',pa.bool_()),
    ('source_candidate_count',pa.int64()),('shared_google_identity',pa.bool_()),
    ('multiple_google_candidates',pa.bool_())])
DICTIONARY_SCHEMA=pa.schema([(k,pa.string()) for k in ['street_dictionary_id','street_name',
    'normalized_street_name','city_name','dictionary_status','provenance_json','rule_version']]
    +[('aliases',pa.list_(pa.string())),('google_poi_count',pa.int64()),
      ('ward_names_observed',pa.list_(pa.string()))])


def key(row):return tuple(row[k] for k in KEY)


def norm(value):
    value=comparison_text(value)
    return ' '.join(re.findall(r'[^\W_]+',value)) if value else None


def street_norm(value):
    value=re.sub('^'+ROAD,'',value or '',flags=re.I).strip()
    value=norm(value)
    return 'so '+value if value and re.fullmatch(r'\d+[a-z]?',value) else value


def label_context(metadata):
    parts=[x.strip() for x in metadata['admin_area_name'].split(',')]
    ward=re.sub(r'^(?:Phường|Ward|Xã)\s+','',parts[0],flags=re.I)
    city=re.sub(r'^(?:Thành phố|City|Tỉnh)\s+','',parts[1],flags=re.I) if len(parts)>1 else None
    return dict(wards={norm(ward):ward},cities={norm(city):city} if city else {},
        countries={norm(parts[-1])} if len(parts)>2 else set())


def _split_physical(part):
    # Keep numeric house paths intact. Split compound street expressions only
    # after a street-like word, never inside 134/41 or lot O/28.
    for m in re.finditer('/',part):
        left,right=part[:m.start()],part[m.end():]
        if ((len(re.findall(r'[^\W\d_]+',left))>=2 and re.search(r'[^\W\d_]$',left) or re.match('^'+ROAD,left,re.I))
            and re.match(r'(?:'+LOCATOR+r'\s+|'+ROAD+r')',right,re.I)):
            return [left,*_split_physical(right)]
    return [part]


def parse_google_address(raw,context,known_streets=None):
    """Extract observed source claims; never infer admin areas from coordinates."""
    known_streets=known_streets or set()
    base=parse_address(raw)
    result={f:None for f in ADDRESS_FIELDS}
    result.update(address_parse_status='MISSING',street_status='MISSING',
        street_analysis_eligible=False,parse_notes=[],evidence=dict(m15=base))
    if raw is None or not isinstance(raw,str) or not raw.strip():return result
    clean=visible_text(raw);parts=[p.strip() for p in clean.split(',') if p.strip()]
    notes=[];consumed=set();claims=defaultdict(list);field_parts=defaultdict(list)
    city_indices=[]
    def claim(field,value,part,method):
        if value:claims[field].append(value);field_parts[field].append(dict(raw_part=part,method=method))
    for i,part in enumerate(parts):
        stripped=re.sub(r'\s+\d{4,6}$','',part).strip();n=norm(stripped)
        explicit=re.match(r'^(?:thành phố|thanh pho|tp\.?|city)\s+(.+)$',stripped,re.I)
        if explicit:
            claim('city_name',explicit.group(1),part,'EXPLICIT_SOURCE_CITY');consumed.add(i);city_indices.append(i)
        elif n in context['cities']:
            claim('city_name',context['cities'][n],part,'SOURCE_CITY_LABEL_MATCH');consumed.add(i);city_indices.append(i)
        elif n in context['countries']:consumed.add(i)
        else:
            ward=re.match(r'^(?:phường|phuong|ward|xã|xa)\s+(.+)$',stripped,re.I)
            district=re.match(r'^(?:quận|quan|district|q\.)\s*(.+)$',stripped,re.I)
            if ward:
                claim('ward_name',ward.group(1),part,'EXPLICIT_SOURCE_WARD');consumed.add(i)
            elif district:
                claim('legacy_district',district.group(1),part,'EXPLICIT_SOURCE_DISTRICT_CLAIM');consumed.add(i)
            elif n in context['wards']:
                claim('ward_name',context['wards'][n],part,'SOURCE_LOCALITY_MATCHED_REFERENCE_LABEL');consumed.add(i)
            elif re.match(r'^(?:khu phố|khu pho|khu tái|khu tai|phường$|ward$)',part,re.I):consumed.add(i)
    # A bare suffix locality has no proven administrative level. It is retained,
    # not promoted to ward or old district simply because it precedes a city.
    if city_indices:
        i=min(city_indices)-1
        if i>=0 and i not in consumed and i>0:
            p=parts[i]
            if not re.search(r'\d|/|'+PREMISE+'|'+ROAD,p,re.I):
                result['locality_name_raw']=p;consumed.add(i);notes.append('UNLABELED_LOCALITY_ADMIN_LEVEL_UNKNOWN')
    physical=[]
    for i,part in enumerate(parts):
        if i in consumed:continue
        # Source may embed old ward/district inside a physical clause. Record
        # only explicit claims and retain the preceding physical expression.
        match=re.search(r'\s+(?:phường|phuong)\s+(\d+)\b',part,re.I)
        district=re.search(r'\b(?:quận|quan|q\.)\s*(\d+)\b',part,re.I)
        if match:claim('ward_name',match.group(1),part,'EXPLICIT_SOURCE_WARD');part=part[:match.start()].strip()
        if district:claim('legacy_district',district.group(1),parts[i],'EXPLICIT_SOURCE_DISTRICT_CLAIM')
        if re.match(PLUS,part,re.I):
            part=re.sub(PLUS,'',part,flags=re.I).strip();notes.append('PLUS_CODE_NOT_HOUSE_OR_STREET')
        if part:physical.extend(_split_physical(part))
    houses=[];alleys=[];lots=[];streets=[];unresolved=[];premises=[]
    def add_street(value,original,method):
        value=value.strip(' ,');prefix=re.match('^'+ROAD,value,re.I)
        display=value[prefix.end():].strip() if prefix else value
        if display:
            streets.append(dict(name=display,raw=original,normalized=street_norm(display),method=method,
                internal=bool(re.match(PREMISE,display,re.I))))
    for original in physical:
        part=original.strip();explicit=False
        house_count_before=len(houses)
        if re.match(r'^(?:mặt tiền|mat tien|cổng|cong)$',part,re.I):continue
        if re.match(r'^(?:trước cửa nhà|truoc cua nha|đối diện|doi dien|gần|gan)\b',part,re.I):
            notes.append('REFERENCE_ONLY_LOCATION');unresolved.append(part);continue
        # A named building after /CC is context, not another street.
        premise_tail=re.search(r'/\s*(?:CC|chung cư|chung cu)\b',part,re.I)
        if premise_tail:premises.append(part[premise_tail.start()+1:]);part=part[:premise_tail.start()].strip()
        lot=re.search(r'\b(?:lô|lo|block)\s+([A-Za-z0-9]+(?:/\d+)*)\b',part,re.I)
        if lot:
            lots.append(lot.group(1).casefold());part=(part[:lot.start()]+' '+part[lot.end():]).strip()
        elif re.match(r'^(?:lô|lo)\s*$',part,re.I):premises.append(part);continue
        if re.fullmatch(r'(?:số|so)\s+\d+[A-Za-z]?',part,re.I):
            if lot and street_norm(part) in known_streets:add_street(part,part,'NUMBERED_STREET_AFTER_LOT_AND_OBSERVED_ALIAS')
            else:notes.append('NUMBERED_STREET_OR_HOUSE_ROLE_UNKNOWN');unresolved.append(original)
            continue
        house_range=re.match(r'^(\d+[A-Za-z]?)\s*[-–—]\s*(\d+[A-Za-z]?)(?=\s|$)',part)
        house=re.match(r'^(?:số\s+|so\s+)?('+LOCATOR+r')(?=\s|$)',part,re.I) if not house_range else None
        if house_range:
            lo,hi=house_range.groups()
            if lo.isdigit() and hi.isdigit() and int(lo)<=int(hi):houses.append((lo+'-'+hi,'RANGE',house_range.group(0)))
            else:notes.append('AMBIGUOUS_HOUSE_RANGE')
            part=part[house_range.end():].strip();explicit=True
        elif house:
            identifier=re.sub(r'\s+','',house.group(1)).casefold();houses.append((identifier,'LITERAL',house.group(1)))
            part=part[house.end():].strip();explicit=True
        alley=re.match(r'^(?:hẻm|hem|ngõ|ngo|ngách|ngach|kiệt|kiet)\s+('+LOCATOR+r')(?=\s|$)',part,re.I)
        if alley:
            alleys.append(re.sub(r'\s+','',alley.group(1)).casefold());part=part[alley.end():].strip();explicit=True
        road=re.match('^'+ROAD,part,re.I)
        if len(re.findall(r'\b(?:đ\.|đường\b|duong\b|street\b)',part,re.I))>1:
            notes.append('MULTIPLE_OR_UNRESOLVED_STREET_EXPRESSION');unresolved.append(original);continue
        if road:
            add_street(part,part,'EXPLICIT_SOURCE_ROAD_PREFIX');continue
        if not part:continue
        if re.match(PREMISE,part,re.I):premises.append(part);continue
        if re.match(r'^'+LOCATOR+r'(?:\s|$)',part,re.I):
            houses=houses[:house_count_before]
            notes.append('UNSEPARATED_NUMERIC_ADDRESS');unresolved.append(original);continue
        if re.search(r'\b(?:đ\.|đường|duong)\s+',part,re.I):
            notes.append('MULTIPLE_OR_UNRESOLVED_STREET_EXPRESSION');unresolved.append(original);continue
        if re.match(r'^(?:tổ|to|khu phố|khu pho)\s+\d+\b',part,re.I):
            premises.append(part);notes.append('SUBAREA_LABEL_NOT_STREET');continue
        if explicit:add_street(part,part,'AFTER_OBSERVED_LOCATOR')
        elif street_norm(part) in known_streets:add_street(part,part,'EXACT_OBSERVED_GOOGLE_DICTIONARY_ALIAS')
        else:unresolved.append(part)
    house_values=sorted({h[0] for h in houses});alley_values=sorted(set(alleys));lot_values=sorted(set(lots))
    if len(house_values)==1:
        result['house_identifier']=house_values[0];result['house_kind']=next(h[1] for h in houses if h[0]==house_values[0])
    elif len(house_values)>1:notes.append('MULTIPLE_HOUSE_CANDIDATES_NO_SELECTION')
    for field,values in [('alley_identifier',alley_values),('lot_identifier',lot_values)]:
        if len(values)==1:result[field]=values[0]
        elif len(values)>1:notes.append('MULTIPLE_'+field.upper()+'_CANDIDATES')
    normalized_streets=sorted({r['normalized'] for r in streets})
    if len(normalized_streets)==1:
        candidates=sorted(streets,key=lambda r:(r['name'],r['raw']))
        result.update(street_name=candidates[0]['name'],street_name_raw=candidates[0]['raw'],
            normalized_street_name=normalized_streets[0],street_status='EXTRACTED')
        if any(r['internal'] for r in streets):
            notes.append('INTERNAL_PREMISE_ROAD_LABEL_NOT_VERIFIED_STREET');result['street_status']='UNCERTAIN_INTERNAL_ROAD'
        else:result['street_analysis_eligible']=True
    elif len(normalized_streets)>1:notes.append('MULTIPLE_STREET_CANDIDATES_NO_SELECTION');result['street_status']='AMBIGUOUS'
    else:result['street_status']='UNKNOWN';notes.append('STREET_NOT_IDENTIFIED')
    for field,values in claims.items():
        by_norm={norm(v):v for v in sorted(values)}
        if len(by_norm)==1:result[field]=next(iter(by_norm.values()))
        elif len(by_norm)>1:notes.append('CONFLICTING_SOURCE_'+field.upper()+'_CLAIMS')
    if result['ward_name']:result['ward_evidence_kind']=';'.join(sorted({r['method'] for r in field_parts['ward_name']}))
    ambiguous=any(t.startswith(('MULTIPLE_','CONFLICTING_','AMBIGUOUS_','UNSEPARATED_','REFERENCE_ONLY')) for t in notes)
    if unresolved:notes.append('UNPARSED_SOURCE_PARTS_RETAINED')
    result['address_parse_status']='AMBIGUOUS' if ambiguous else 'PARSED' if result['street_analysis_eligible'] and not unresolved else 'PARTIAL'
    result['parse_notes']=sorted(set(notes))
    result['evidence'].update(cleaned_address=clean,physical_parts=physical,house_candidates=houses,
        alley_candidates=alley_values,lot_candidates=lot_values,street_candidates=streets,
        administrative_claims=dict(field_parts),unresolved_parts=unresolved,premise_context=premises,
        policy='Observed Google claims only; not verified physical address/admin coverage; NULL is unknown.')
    return result


def build(places,er_rows,overture,zero_inventory,context,refs):
    input_place_count=len(places)
    unique={}
    for r in places:
        k=key(r)
        if not all(k):raise ValueError('Missing Google identity')
        if k in unique and silver.dump(unique[k])!=silver.dump(r):raise ValueError('Conflicting duplicate Silver identity; resolve upstream')
        unique[k]=r
    places=[unique[k] for k in sorted(unique)]
    first={key(r):parse_google_address(r['address_display'],context) for r in places}
    known={p['normalized_street_name'] for p in first.values() if p['street_analysis_eligible']}
    parsed={key(r):parse_google_address(r['address_display'],context,known) for r in places}
    by_google=defaultdict(set);by_ov=defaultdict(list)
    er_keys=set()
    for r in sorted(er_rows,key=lambda r:(r['overture_id'],*key(r))):
        if r['rule_version']!=ER_VERSION or r['er_status']!='ER_PENDING':raise ValueError('Expected unconfirmed ER V3 proposals')
        if r['classification'] not in {'MATCH_CANDIDATE','REVIEW','INSUFFICIENT_DATA','NON_MATCH_CANDIDATE'}:raise ValueError('Unknown ER classification')
        k=(r['overture_id'],*key(r))
        if k in er_keys:raise ValueError('Duplicate ER relationship')
        if key(r) not in unique:raise ValueError('ER Google FK missing')
        er_keys.add(k);by_google[key(r)].add(r['overture_id']);by_ov[r['overture_id']].append(r)
    dict_groups=defaultdict(list)
    for r in places:
        p=parsed[key(r)]
        if p['street_analysis_eligible']:dict_groups[(norm(p['city_name']),p['normalized_street_name'])].append(r)
    dictionary=[];street_ids={}
    for k,group in sorted(dict_groups.items(),key=lambda kv:silver.dump(kv[0])):
        identifier='google-observed-street:'+_digest(k)[:20]
        aliases=sorted({a for r in group for a in [parsed[key(r)]['street_name'],parsed[key(r)]['street_name_raw']] if a})
        display_counts=Counter(parsed[key(r)]['street_name'] for r in group)
        display=min(display_counts,key=lambda n:(-display_counts[n],n))
        for r in group:street_ids[key(r)]=identifier
        dictionary.append(dict(street_dictionary_id=identifier,street_name=display,normalized_street_name=k[1],
            city_name=next((parsed[key(r)]['city_name'] for r in group if parsed[key(r)]['city_name']),None),
            aliases=aliases,google_poi_count=len(group),ward_names_observed=sorted({parsed[key(r)]['ward_name'] for r in group if parsed[key(r)]['ward_name']}),
            dictionary_status='OBSERVED_GOOGLE_LABEL_NOT_OFFICIAL_STREET_ID',
            provenance_json=silver.dump([dict(google_id_kind=key(r)[0],google_identity_value=key(r)[1],source_ref=refs['places'],address_field_provenance=json.loads(r['field_provenance_json']).get('address_display')) for r in group]),rule_version=VERSION))
    enrich=[]
    for r in places:
        p=parsed[key(r)]
        enrich.append(dict(**dict(zip(KEY,key(r))),google_name=r['name_display'],address_raw=r['address_display'],
            **{f:p[f] for f in ADDRESS_FIELDS},street_dictionary_id=street_ids.get(key(r)),
            observed_at=r['observed_at'],source_run=r['source_run'],street_analysis_eligible=p['street_analysis_eligible'],
            shared_by_overture_ids=len(by_google[key(r)])>1,parse_notes=p['parse_notes'],
            address_provenance_json=silver.dump(dict(silver_ref=refs['places'],google_key=dict(zip(KEY,key(r))),
                address_field_provenance=json.loads(r['field_provenance_json']).get('address_display'),
                address_is_fallback='address_display' in r['fallback_fields'] or 'address_normalized' in r['fallback_fields'],
                source_observed_at=r['observed_at'],admin_label_reference=refs.get('admin_labels'))),
            parser_evidence_json=silver.dump(p['evidence']),rule_version=VERSION))
    zero_ids={r['overture_id'] for r in zero_inventory}
    if len(zero_ids)!=len(zero_inventory):raise ValueError('Duplicate zero-candidate inventory ID')
    if zero_ids & by_ov.keys():raise ValueError('Zero-candidate inventory overlaps ER links')
    scope=set(by_ov)|zero_ids;ov={r['source_id']:r for r in overture}
    if len(ov)!=len(overture) or scope-ov.keys():raise ValueError('Overture source key missing or duplicated')
    links=[]
    for oid in sorted(scope):
        candidates=by_ov.get(oid,[]);positives=[r for r in candidates if r['classification']=='MATCH_CANDIDATE']
        selected=None;multiple=len({key(r) for r in candidates})>1
        if not candidates:status='NOT_ENRICHED_NO_GOOGLE_CANDIDATE'
        elif multiple:status='NOT_ENRICHED_MULTIPLE_CANDIDATES'
        elif len(positives)==1:
            selected=positives[0]
            if not unique[key(selected)]['address_display']:status='NOT_ENRICHED_MISSING_GOOGLE_ADDRESS';selected=None
            else:status='PROVISIONAL'
        else:status='NOT_ENRICHED_ER_'+candidates[0]['classification']
        p=parsed[key(selected)] if selected else None
        gg=unique[key(selected)] if selected else None
        links.append(dict(overture_id=oid,overture_name_raw=ov[oid]['name'],overture_address_raw=ov[oid]['address'],
            **{f:(selected[f] if selected else None) for f in KEY},enrichment_status=status,
            preferred_address_source='google' if selected else 'overture',preferred_address_raw=gg['address_display'] if gg else ov[oid]['address'],
            **{f:p[f] if p else None for f in PARSED_FIELDS},address_parse_status=p['address_parse_status'] if p else None,
            street_analysis_eligible=p['street_analysis_eligible'] if p else False,er_status='ER_PENDING',
            er_classification=selected['classification'] if selected else candidates[0]['classification'] if len(candidates)==1 else None,
            er_rule_version=ER_VERSION,source_candidate_count=len(candidates),multiple_google_candidates=multiple,
            shared_google_identity=len(by_google[key(selected)])>1 if selected else any(len(by_google[key(r)])>1 for r in candidates),
            candidate_relationships_json=silver.dump([dict(google_id_kind=r['google_id_kind'],google_identity_value=r['google_identity_value'],classification=r['classification'],er_status=r['er_status'],observation_ref=r['observation_ref']) for r in candidates]),
            provenance_json=silver.dump(dict(er_ref=refs['er'],overture_ref=refs['overture'],google_ref=refs['places'] if selected else None,source_id=oid)),rule_version=VERSION))
    # Report by observed normalized NAME with distinct Google IDs. Dictionary
    # IDs remain city-scoped; an unknown city is not silently imputed for counts.
    name_groups=defaultdict(list)
    for r in enrich:
        if r['street_analysis_eligible']:name_groups[r['normalized_street_name']].append(r)
    top=[]
    for name,group in name_groups.items():
        counts=Counter(r['street_name'] for r in group)
        top.append(dict(street_name=min(counts,key=lambda n:(-counts[n],n)),normalized_street_name=name,
            google_poi_count=len({key(r) for r in group}),city_names_observed=sorted({r['city_name'] for r in group if r['city_name']}),
            unknown_city_poi_count=sum(r['city_name'] is None for r in group)))
    top=sorted(top,key=lambda r:(-r['google_poi_count'],r['normalized_street_name']))[:20]
    hard=sorted(enrich,key=lambda r:(-int(r['address_parse_status']=='AMBIGUOUS'),-int(r['address_raw'] is not None and not r['street_analysis_eligible']),-len(r['parse_notes']),*key(r)))[:20]
    summary=dict(rule_version=VERSION,google_identity_rows=len(enrich),technical_exact_duplicates_removed=input_place_count-len(unique),
        google_pois_with_address=sum(bool(r['address_raw']) for r in enrich),google_pois_parse_success=sum(r['address_parse_status']=='PARSED' for r in enrich),
        google_pois_with_analytical_street=sum(r['street_analysis_eligible'] for r in enrich),
        google_pois_missing_address=sum(not r['address_raw'] for r in enrich),google_pois_without_analytical_street=sum(not r['street_analysis_eligible'] for r in enrich),
        google_pois_with_ward=sum(bool(r['ward_name']) for r in enrich),google_pois_with_city=sum(bool(r['city_name']) for r in enrich),
        unique_normalized_street_names=len({r['normalized_street_name'] for r in dictionary}),street_dictionary_rows=len(dictionary),
        parse_status_counts=dict(sorted(Counter(r['address_parse_status'] for r in enrich).items())),
        overture_rows=len(links),er_relationships=len(er_rows),zero_candidate_overture_ids=len(zero_ids),
        provisional_enriched_pois=sum(r['enrichment_status']=='PROVISIONAL' for r in links),
        provisional_distinct_google_id_count=len({key(r) for r in links if r['enrichment_status']=='PROVISIONAL'}),
        provisional_shared_google_links=sum(r['enrichment_status']=='PROVISIONAL' and r['shared_google_identity'] for r in links),
        status_counts=dict(sorted(Counter(r['enrichment_status'] for r in links).items())),
        provisional_with_analytical_street=sum(r['enrichment_status']=='PROVISIONAL' and r['street_analysis_eligible'] for r in links),
        top_20_streets=top,
        difficult_cases=[{k:r[k] for k in [*KEY,'google_name','address_raw','house_identifier','street_name','ward_name','city_name','address_parse_status','parse_notes','address_provenance_json','parser_evidence_json']} for r in hard],
        semantics='Unique Google identities, not verified physical store counts. Observed labels only; no complete street/ward coverage claim. All attachments PROVISIONAL, never human-verified ER MATCH.')
    return {'google_address_enrichment.parquet':pa.Table.from_pylist(enrich,schema=PLACE_SCHEMA),
        'provisional_poi_address_links.parquet':pa.Table.from_pylist(links,schema=LINK_SCHEMA),
        'street_dictionary.parquet':pa.Table.from_pylist(dictionary,schema=DICTIONARY_SCHEMA)},summary


def publish(silver_root,er_root,output_root,admin_labels,*,fault=lambda stage:None):
    roots=[Path(p).resolve() for p in (silver_root,er_root)];out=Path(output_root).resolve();admin_labels=Path(admin_labels).resolve()
    if any(out.is_relative_to(r) or r.is_relative_to(out) for r in roots):raise ValueError('Separate input/output required')
    if (out/'current.json').exists():
        active=silver.read_active(out)
        if silver.verify_release(active)['signature'].get('rule_version')!=VERSION:raise ValueError('Cannot replace a different published dataset')
    m,e=[silver.read_active(r) for r in roots]
    mm,em=[silver.verify_release(r) for r in (m,e)]
    if em['signature']['rule_version']!=ER_VERSION:raise ValueError('Expected published ER V3')
    places=m/'silver/google_places.parquet';source_links=m/'silver/source_links.parquet';er=e/'er_results.parquet'
    inventory_path=m/'reports/google_silver_dq_summary.json'
    ov_paths=[Path(p) for p in mm['signature']['inputs_sha256'] if Path(p).name=='current_state.parquet']
    if len(ov_paths)!=1:raise ValueError('Cannot identify pinned Overture')
    overture=ov_paths[0]
    if out.is_relative_to(overture.parent) or overture.parent.is_relative_to(out):raise ValueError('Output overlaps Overture input')
    hashes={str(p.resolve()):silver.sha(p) for p in [places,source_links,er,overture,inventory_path,admin_labels,m/'manifest.json',e/'manifest.json',*[r/'current.json' for r in roots]]}
    for p in [places,source_links,overture]:
        if em['signature']['inputs_sha256'].get(str(p))!=hashes[str(p.resolve())]:raise ValueError('ER/Silver input lineage mismatch')
    if mm['signature']['inputs_sha256'].get(str(overture))!=hashes[str(overture.resolve())]:raise ValueError('Pinned Overture mismatch')
    place_rows=pq.read_table(places).to_pylist();er_rows=pq.read_table(er).to_pylist()
    actual_links=pq.read_table(source_links,columns=['overture_id',*KEY]).to_pylist()
    if {(r['overture_id'],*key(r)) for r in er_rows}!={(r['overture_id'],*key(r)) for r in actual_links}:raise ValueError('Lost ER relationship')
    for row in place_rows:
        ref=json.loads(row['field_provenance_json']).get('address_display')
        if ref:
            p=Path(ref['metadata_path']);digest=silver.sha(p)
            if digest!=ref['metadata_sha256'] or json.loads(p.read_text(encoding='utf-8')).get('address')!=row['address_display']:raise ValueError('Raw address provenance mismatch')
            hashes[str(p.resolve())]=digest
    refs={k:dict(path=str(p.resolve()),sha256=hashes[str(p.resolve())]) for k,p in [('places',places),('er',er),('overture',overture),('admin_labels',admin_labels)]}
    inventory=json.loads(inventory_path.read_text(encoding='utf-8'))['zero_candidate_inventory']
    tables,summary=build(place_rows,er_rows,pq.read_table(overture).to_pylist(),inventory,
        label_context(json.loads(admin_labels.read_text(encoding='utf-8'))),refs)
    summary['inputs']=refs
    signature=dict(rule_version=VERSION,inputs_sha256=dict(sorted(hashes.items())),pyarrow_version=pa.__version__,
        code_sha256={str(Path(__file__).resolve()):silver.sha(__file__),str(Path(silver.__file__).resolve()):silver.sha(silver.__file__),
        str(Path(__file__).with_name('comparison_features.py').resolve()):silver.sha(Path(__file__).with_name('comparison_features.py'))})
    rid=_digest(signature)[:24];out.mkdir(parents=True,exist_ok=True);lock=out/'.publish.lock'
    with lock.open('x') as stream:stream.write(str(os.getpid()))
    try:
        silver.read_active(out);dest=out/'releases'/rid
        if not dest.exists():
            stage=out/('.stage-'+uuid.uuid4().hex);stage.mkdir();artifacts={}
            for name,table in tables.items():
                pq.write_table(table,stage/name,compression='zstd');artifacts[name]=dict(rows=table.num_rows,sha256=silver.sha(stage/name),logical_sha256=_digest(table.to_pylist()),schema=str(table.schema))
            (stage/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')
            artifacts['summary.json']=dict(sha256=silver.sha(stage/'summary.json'))
            (stage/'manifest.json').write_text(json.dumps(dict(release_id=rid,signature=signature,artifacts=artifacts),sort_keys=True,indent=2),encoding='utf-8')
            silver.verify_release(stage);fault('after_stage_validation')
            for p,h in hashes.items():
                if silver.sha(p)!=h:raise ValueError('Input changed during publication: '+p)
            dest.parent.mkdir(exist_ok=True);os.replace(stage,dest)
        else:silver.verify_release(dest)
        fault('before_pointer_replace')
        pointer=out/('.current-'+uuid.uuid4().hex);pointer.write_text(silver.dump(dict(release_id=rid,manifest_sha256=silver.sha(dest/'manifest.json'))),encoding='utf-8');os.replace(pointer,out/'current.json')
        return dest,summary
    finally:lock.unlink()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--silver-root',type=Path,required=True);p.add_argument('--er-root',type=Path,required=True);p.add_argument('--output-root',type=Path,required=True)
    p.add_argument('--admin-labels',type=Path,default=Path('config/admin_areas/phuong_khanh_hoi_metadata.json'))
    args=p.parse_args();dest,summary=publish(args.silver_root,args.er_root,args.output_root,args.admin_labels)
    print(silver.dump(dict(release=str(dest),counts={k:v for k,v in summary.items() if isinstance(v,int)},parse_status=summary['parse_status_counts'])))
