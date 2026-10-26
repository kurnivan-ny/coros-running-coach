{#- Validated activities across every sport. Missing values stay NULL, never 0. -#}
select
    activity_id,
    source,
    sport_type,
    sport_name,
    sport_group,
    is_indoor,
    local_date,
    start_time_utc,
    end_time_utc,
    location,
    duration_s,
    distance_m,
    sets,
    avg_hr,
    calories,
    case when distance_m > 0 and sport_group in ('run', 'walk', 'hike', 'cycling')
         then duration_s / (distance_m / 1000.0) end                as avg_pace_s_per_km,
    reported_pace_s_per_km,
    quality_flag,
    payload_hash
from {{ ref('stg_activities') }}
where reject_reason is null
