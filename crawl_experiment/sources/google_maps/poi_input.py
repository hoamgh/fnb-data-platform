"""Source-neutral search planning. Optional data is evidence, never identity."""
from __future__ import annotations

import math


def text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def coordinates(place):
    lat=place.get('lat',place.get('latitude')); lon=place.get('lng',place.get('longitude'))
    valid=all(not isinstance(v,bool) and isinstance(v,(int,float)) and math.isfinite(v) and abs(v)<=bound
              for v,bound in [(lat,90),(lon,180)])
    return (lat,lon) if valid else (None,None)


def identity(place):
    key=text(place.get('place_key'))
    if key: return key
    source,source_id=text(place.get('source')),text(place.get('source_id'))
    if not source or not source_id:
        raise ValueError('Source identity is required; optional fields may be missing')
    return source+':'+source_id


def search_plan(place):
    name=text(place.get('name')); address=text(place.get('address'))
    context=text(place.get('admin_area_name')) or text(place.get('geographic_context'))
    if not context:
        context=', '.join(filter(None,[text(place.get(k)) for k in ['ward','city','country']])) or None
    lat,lon=coordinates(place)
    flags=list(place.get('dq_flags') or [])
    if not address: flags.append('SOURCE_ADDRESS_MISSING')
    if lat is None: flags.append('SOURCE_COORDINATES_MISSING_OR_INVALID')
    if not name: flags.append('SOURCE_NAME_MISSING')
    coordinate_text=f'{lat:.7f},{lon:.7f}' if lat is not None else None
    mode=('NAME_ADDRESS_COORDINATES' if address and coordinate_text else
          'NAME_ADDRESS' if address else 'NAME_COORDINATES_CONTEXT' if coordinate_text else 'NAME_CONTEXT')
    eligible=bool(name and (address or coordinate_text or context))
    return dict(query=' '.join(filter(None,[name,address,coordinate_text,context])), mode=mode,
                eligible=eligible, reason=None if eligible else 'INSUFFICIENT_SEARCH_INFORMATION',
                dq_flags=list(dict.fromkeys(flags)), latitude=lat, longitude=lon,
                geographic_context=context, ward=text(place.get('ward')))
