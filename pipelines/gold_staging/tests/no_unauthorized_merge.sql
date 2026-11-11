select source, source_id_kind, source_id
from {{ ref('store_source_mapping') }}
group by all having count(*) <> 1
union all
select 'accepted_google', google_id_kind, google_identity_value
from {{ ref('dim_store') }} where google_identity_value is not null
group by all having count(*) <> 1
