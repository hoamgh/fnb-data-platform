"""Bounded offline reference-first address feasibility experiment (<=50 rows)."""
import argparse
import csv
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq
from shapely.geometry import LineString, MultiLineString, Point, shape, mapping
from shapely.ops import transform

from fnb_platform import google_silver as silver
from fnb_platform.comparison_features import comparison_text, parse_address

VERSION='reference-first-feasibility-v1'
LOCATOR=r'[a-z]?\d+[a-z0-9]*(?:/\d+[a-z0-9]*)*'


def clean(text):
    text=comparison_text(text) or ''
    text=re.sub(r'\s*/\s*','/',text)
    return re.sub(r'\s*[-–—]\s*','-',text)


def tokens(text):
    return re.findall(r'[a-z]*\d+[a-z0-9]*(?:/\d+[a-z0-9]*)*|[a-z]+',clean(text))


def street_key(text):
    ts=tokens(text)
    if ts and ts[0] in {'duong','d','street'}:ts=ts[1:]
    if len(ts)==1 and re.fullmatch(r'\d+[a-z]?',ts[0]):ts=['so',ts[0]]
    return ' '.join(ts)


def projection(boundary):
    lon,lat=boundary.centroid.x,boundary.centroid.y
    def xy(x,y,z=None):
        return ((x-lon)*math.pi/180*6371008.8*math.cos(math.radians(lat)),
                (y-lat)*math.pi/180*6371008.8)
    return xy


def make_registry(raw,boundary):
    ways=[]
    for row in raw['elements']:
        if row.get('type')!='way' or len(row.get('geometry',[]))<2:continue
        geom=LineString([(n['lon'],n['lat']) for n in row['geometry']])
        if not geom.intersects(boundary):continue
        ways.append((row,geom))
    # Connect same-name segments only through shared OSM node IDs. A repeated
    # name in disconnected places does not become one invented global street.
    parent=list(range(len(ways)))
    def find(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    nodes={}
    for i,(w,g) in enumerate(ways):
        name=street_key(w['tags']['name'])
        for node in w.get('nodes',[]):
            key=name,node
            if key in nodes:parent[find(i)]=find(nodes[key])
            else:nodes[key]=i
    groups=defaultdict(list)
    for i,w in enumerate(ways):groups[find(i)].append(w)
    registry=[]
    for entries in groups.values():
        entries=sorted(entries,key=lambda item:item[0]['id'])
        ids=sorted(w['id'] for w,g in entries);aliases=[]
        for w,g in entries:
            for tag in ['name','name:vi','official_name','short_name','alt_name','old_name']:
                for value in w['tags'].get(tag,'').split(';'):
                    if value.strip():aliases.append(dict(value=value.strip(),key=street_key(value),kind=tag,
                        way_id=w['id'],object_timestamp=w.get('timestamp'),valid_from=None,valid_to=None))
        aliases=sorted({silver.dump(a):a for a in aliases}.values(),key=silver.dump)
        name=sorted(entries,key=lambda e:e[0]['id'])[0][0]['tags']['name']
        registry.append(dict(street_id='pilot-osm-connected:'+str(ids[0]),display_name=name,
            name_key=street_key(name),way_ids=ids,aliases=aliases,
            geometry=mapping(MultiLineString([list(g.coords) for w,g in entries])),
            geometry_type='MultiLineString',id_status='SNAPSHOT_CONNECTED_COMPONENT_NOT_OFFICIAL_STREET_ID',
            valid_from=None,valid_to=None,effective_date_status='UNKNOWN',
            is_alley=bool(re.match(r'^(?:hem|ngo|ngach)\b',clean(name)))))
    return sorted(registry,key=lambda r:r['street_id']),len(ways)


def extract(raw,registry,point,boundary,admin):
    text=clean(raw);ts=tokens(raw);matches=[];blocked=[]
    ward=street_key(admin['admin_area_name'].split(',')[0].replace('Phường ',''))
    parts=[p.strip() for p in text.split(',')]
    for road in registry:
        if road['is_alley']:continue
        for alias in road['aliases']:
            needle=alias['key'].split()
            variants=[needle]
            if len(needle)==2 and needle[0]=='so':variants += [['duong',needle[1]],['d',needle[1]]]
            for variant in variants:
                for pos in range(len(ts)-len(variant)+1):
                    if ts[pos:pos+len(variant)]!=variant:continue
                    prior=ts[max(0,pos-3):pos];prefix=ts[:pos]
                    admin_context=any(t in {'phuong','ward','quan','district','tinh'} for t in prior[-1:])
                    premise_context=prior[-2:] in [['cu','xa'],['chung','cu'],['toa','nha']]
                    offset=0;segment_index=0
                    for j,part in enumerate(parts):
                        end=offset+len(tokens(part))
                        if offset<=pos<end:segment_index=j;break
                        offset=end
                    bare_ward=alias['key']==ward and segment_index>0 and tokens(parts[segment_index])==needle
                    numeric=needle[0]=='so' and len(needle)==2 and bool(re.fullmatch(r'\d+[a-z]?',needle[1]))
                    numeric_context=(variant[0] in {'duong','d'} or bool(prior and prior[-1] in {'duong','d'})
                        or bool(prefix and any(re.fullmatch(LOCATOR,t) for t in prefix) and prefix[-1] not in {'hem','ngo','ngach'}))
                    if admin_context or premise_context or bare_ward or numeric and not numeric_context:
                        blocked.append(dict(alias=alias['value'],position=pos,reason='ADMIN_PREMISE_OR_NUMBER_ROLE_UNSUPPORTED'));continue
                    dist=None if point is None else transform(projection(boundary),shape(road['geometry'])).distance(transform(projection(boundary),point))
                    matches.append(dict(street_id=road['street_id'],street_name=road['display_name'],name_key=road['name_key'],
                        alias=alias['value'],alias_kind=alias['kind'],position=pos,length=len(variant),distance_m=dist,
                        way_ids=road['way_ids'],match_method='EXACT_TOKEN_BOUNDARY_ALIAS'))
    matches=sorted({(r['street_id'],r['position'],r['alias_kind']):r for r in matches}.values(),key=silver.dump)
    current=[r for r in matches if r['alias_kind']!='old_name']
    groups=defaultdict(list)
    for r in current:groups[r['name_key']].append(r)
    # Coordinates corroborate text-matched candidates only. No nearest-road fallback.
    selected=None;status='NO_REFERENCE_ALIAS';confidence='UNKNOWN'
    if len(groups)>1:status='AMBIGUOUS_MULTIPLE_STREETS'
    elif len(groups)==1:
        candidates=next(iter(groups.values()))
        supported={r['street_id']:r for r in candidates if r['distance_m'] is not None and r['distance_m']<=100}
        uncertain={r['street_id'] for r in candidates if r['distance_m'] is None or 100<r['distance_m']<=200}
        if len(supported)==1 and not uncertain:
            selected=next(iter(supported.values()));status='TEXT_AND_GEOMETRY_SUPPORTED';confidence='SUPPORTED_NOT_CALIBRATED'
        elif len(supported)>1 or uncertain:status='AMBIGUOUS_GEOMETRY_OR_MISSING_COORDINATE'
        else:status='LEXICAL_CANDIDATE_GEOMETRY_DISAGREES'
    # Parse locators relative to reference spans rather than guessing a street
    # from all text following a number. Retain unsupported numeric expressions.
    street_pos=min((r['position'] for r in current),default=len(ts))
    prefix=' '.join(ts[:street_pos]);house=None;house_range=None;notes=[]
    original_head=re.split(r',',text)[0].strip()
    qualified=bool(re.match(r'^(?:truoc cua nha|doi dien|gan|dau hem)\b',text))
    range_match=re.match(r'^(\d+[a-z]?)\s*-\s*(\d+[a-z]?)(?=\s|,|$)',text)
    if range_match:
        a,b=range_match.groups()
        if a.isdigit() and b.isdigit() and int(a)<=int(b):house_range=a+'-'+b
        else:notes.append('RANGE_ORDER_OR_SUFFIX_UNCERTAIN')
    elif re.match(r'^'+LOCATOR+r'\b',text):
        m=re.match(r'^('+LOCATOR+r')(?=\s|,|$)',text)
        if m and not re.match(r'^\s+'+LOCATOR+r'\b',text[m.end():]):house=m.group(1)
        elif m:notes.append('MULTIPLE_UNSEPARATED_NUMBERS')
    # Comma-delimited or explicit road-first trailing locator.
    if not house and not house_range and len(groups)==1 and re.match(r'^(?:duong|d\.)',text):
        m=re.search(r'\s+('+LOCATOR+r')$',original_head)
        if m:house=m.group(1)
    lots=re.findall(r'\b(?:lo|block)\s+([a-z0-9]+)\b',text)
    alleys=re.findall(r'\b(?:hem|ngo|ngach|kiet)\s+('+LOCATOR+r')\b',text)
    if qualified:
        notes.append('REFERENCE_LOCATION_NOT_CONFIRMED_DOOR');house=None
    if len(groups)>1:notes.append('DO_NOT_PICK_NEAREST_AMONG_DIFFERENT_TEXT_STREETS')
    inside=None if point is None else boundary.covers(point)
    return dict(street_id=None if selected is None else selected['street_id'],
        street_name=None if selected is None else selected['street_name'],
        lexical_street_keys=sorted(groups),house=house,house_range=house_range,alley=alleys,lot=lots,
        admin_area_id=admin['admin_area_id'] if inside else None,admin_assignment_method='POINT_IN_VALIDATED_POLYGON' if inside else 'OUTSIDE_OR_UNKNOWN',
        district_id=None,inside_polygon=inside,status=status,confidence=confidence,notes=notes,
        confidence_scope='STREET_REFERENCE_ONLY_NOT_COMPLETE_ADDRESS',house_confidence='UNVERIFIED_LITERAL' if house else 'UNKNOWN',
        candidate_evidence=matches,blocked_matches=blocked,
        fuzzy_candidates=[],fuzzy_policy='DISABLED_IN_PILOT_NO_PROMOTION_WITHOUT_CORROBORATION')


def run(directory,samples):
    directory=Path(directory);samples=Path(samples)
    raw_path=directory/'roads_overpass_raw.json';source=json.loads((directory/'roads_source.json').read_text())
    if silver.sha(raw_path)!=source['source_sha256']:raise ValueError('Reference hash mismatch')
    boundary_path=Path('config/admin_areas/phuong_khanh_hoi.geojson');meta_path=boundary_path.with_name('phuong_khanh_hoi_metadata.json')
    if silver.sha(boundary_path)!=source['boundary_sha256']:raise ValueError('Boundary hash mismatch')
    boundary=shape(json.loads(boundary_path.read_text(encoding='utf-8'))['geometry']);admin=json.loads(meta_path.read_text(encoding='utf-8'))
    if not boundary.is_valid:raise ValueError('Invalid polygon')
    registry,way_count=make_registry(json.loads(raw_path.read_text(encoding='utf-8')),boundary)
    expected=json.loads(samples.read_text(encoding='utf-8'));assert 0<len(expected)<=50
    comparison=silver.read_active(Path('output/comparison-m15')).resolve()
    tables={side:pq.read_table(comparison/'comparison'/name).to_pylist() for side,name in
        [('overture','overture_features.parquet'),('google','google_features.parquet')]}
    ov_ref=json.loads(tables['overture'][0]['name_source_ref_json']);ov_path=Path(ov_ref['parquet_path'])
    if silver.sha(ov_path)!=ov_ref['parquet_sha256']:raise ValueError('Overture hash mismatch')
    ov={r['source_id']:r for r in pq.read_table(ov_path).to_pylist()}
    pair=pq.read_table(comparison/'comparison/address_pair_evidence.parquet').slice(0,1).to_pylist()[0]
    link_path=Path(json.loads(pair['m1_source_link_ref_json'])['parquet_path']);place_path=link_path.with_name('google_places.parquet')
    silver.verify_release(link_path.parent.parent)
    gg={(r['google_id_kind'],r['google_identity_value']):r for r in pq.read_table(place_path).to_pylist()}
    start=time.perf_counter();rows=[]
    for i,sample in enumerate(expected,1):
        candidates=[r for r in tables[sample['source']] if r['address_raw']==sample['address_raw']]
        if not candidates:raise ValueError('Sample address not found: '+sample['address_raw'])
        r=sorted(candidates,key=silver.dump)[0]
        raw=ov[r['overture_id']] if sample['source']=='overture' else gg[(r['google_id_kind'],r['google_identity_value'])]
        lat,lon=raw['latitude'],raw['longitude']
        point=Point(lon,lat) if isinstance(lat,(int,float)) and isinstance(lon,(int,float)) and -90<=lat<=90 and -180<=lon<=180 else None
        parsed=extract(sample['address_raw'],registry,point,boundary,admin)
        old=parse_address(sample['address_raw'])
        target=sample.get('expected_street_key')
        old_key=street_key(old['street_comparison']) if old['street_comparison'] else None
        new_key=street_key(parsed['street_name']) if parsed['street_name'] else None
        def result(value,expected):return 'CORRECT' if value is not None and value==expected else 'WRONG_ASSIGNMENT' if value is not None else 'ABSTAIN'
        rows.append(dict(sample_id=i,source=sample['source'],name_raw=r['name_raw'],address_raw=r['address_raw'],latitude=lat,longitude=lon,
            expected_street_key=target,expected_house=sample.get('expected_house'),expected_range=sample.get('expected_range'),
            expected_alley=sample.get('expected_alley',[]),expected_lot=sample.get('expected_lot',[]),
            assessment_basis='Agent structural reading of source literal; not human-verified physical address or official street identity',
            m15_street=old_key,m15_house=old['house_identifier'],m15_range=(old['house_range_start']+'-'+old['house_range_end']) if old['house_range_start'] else None,
            m15_alley=old['alley_path'],m15_lot=[],m15_parse_status=old['parse_status'],
            street_result_m15=result(old_key,target),street_result_reference=result(new_key,target),
            **parsed,reference_version=source['source_sha256'],rule_version=VERSION,
            provenance=silver.dump(dict(source_address_ref=json.loads(r['address_source_ref_json']),reference_source=source,
                boundary_file=str(boundary_path.resolve()),boundary_sha256=silver.sha(boundary_path),
                boundary_metadata={k:admin[k] for k in ['admin_area_id','admin_area_name','valid_from','valid_to','source','source_url','source_object_url','legal_source_url','retrieval_date','license','crs']},
                original_observation_status='No street effective-date evidence; snapshot corroboration only'))))
    elapsed=time.perf_counter()-start
    csv_path=directory/'pilot_comparison.csv'
    fields=list(rows[0])
    with csv_path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        writer.writerows({k:silver.dump(v) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in rows)
    (directory/'street_reference.json').write_text(json.dumps(registry,ensure_ascii=False,sort_keys=True,indent=2),encoding='utf-8')
    fields_stats={}
    for field,expected_field,old_field,new_field in [('street','expected_street_key','m15_street','street_name'),('house','expected_house','m15_house','house'),('range','expected_range','m15_range','house_range'),('alley','expected_alley','m15_alley','alley'),('lot','expected_lot','m15_lot','lot')]:
        stats={}
        for label,output in [('m15',old_field),('reference',new_field)]:
            counts=Counter();positive=sum(bool(r[expected_field]) for r in rows)
            for r in rows:
                value=r[output];target=r[expected_field]
                if field=='street' and label=='reference':value=street_key(value) if value else None
                if bool(value):counts['correct_returned' if value==target else 'wrong_returned']+=1
                elif bool(target):counts['abstained_when_expected_present']+=1
                else:counts['correctly_empty']+=1
            stats[label]=dict(expected_present=positive,**dict(counts))
        fields_stats[field]=stats
    lexical=Counter()
    for r in rows:
        values=r['lexical_street_keys']
        if len(values)==1:lexical['correct_returned' if values[0]==r['expected_street_key'] else 'wrong_candidate_returned']+=1
        else:lexical['ambiguous_or_missing']+=1
    issues=[dict(sample_id=r['sample_id'],address_raw=r['address_raw'],
        issue='HOUSE_LITERAL_SELECTED_DESPITE_AMBIGUOUS_SOURCE_EXPRESSION',returned_house=r['house'],structural_target=r['expected_house'])
        for r in rows if r['house'] and r['house']!=r['expected_house']]
    summary=dict(rule_version=VERSION,samples=len(rows),reference_bbox_ways=len(json.loads(raw_path.read_text(encoding='utf-8'))['elements']),
        intersecting_named_ways=way_count,connected_reference_components=len(registry),reference_aliases=sum(len(r['aliases']) for r in registry),
        status_counts=dict(Counter(r['status'] for r in rows)),field_assessment=fields_stats,inside_polygon_counts=dict(Counter(str(r['inside_polygon']) for r in rows)),
        comparison_seconds=round(elapsed,4),reference_id_semantics='Snapshot connected component ID, not an official/permanent canonical street ID',
        source_effective_dates='UNKNOWN for street names; per-way object timestamp and source snapshot retained',
        assessment_limit='Lexical structural audit only, no independent human ground truth or physical-address accuracy estimate',
        source_sha256=source['source_sha256'],sample_sha256=silver.sha(samples),input_sha256={str(p.resolve()):silver.sha(p) for p in [ov_path,place_path,comparison/'manifest.json',boundary_path,meta_path]},
        lexical_street_assessment=dict(lexical),known_pilot_errors=issues,
        boundary_valid_from=admin['valid_from'],boundary_retrieval_date=admin['retrieval_date'],
        reference_snapshot_timestamp=source['osm_base_timestamp'],reference_retrieved_at=source['retrieved_at'],
        excluded_fields='Population and area are not loaded as parsing or confidence features.',
        code_sha256=silver.sha(__file__))
    (directory/'pilot_summary.json').write_text(json.dumps(summary,ensure_ascii=False,sort_keys=True,indent=2),encoding='utf-8')
    print(silver.dump(summary))
    return rows,summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--samples',type=Path,required=True)
    args=parser.parse_args();run(args.directory,args.samples)
