{#- Quarantine: rows that failed validation, with the reason. Never reach gold. -#}
select
    activity_id,
    source,
    reject_reason as reason,
    sport_name,
    local_date,
    duration_s,
    distance_m,
    avg_hr
from {{ ref('stg_activities') }}
where reject_reason is not null
