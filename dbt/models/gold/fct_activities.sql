{#- One row per validated activity, enriched with the load metric (TRIMP).

    TRIMP (Banister): minutes x HRr x a x exp(b x HRr), where
    HRr = (avg_hr - rest_hr) / (max_hr - rest_hr), clamped to [0, 1].
    It is computed from AVERAGE heart rate, so it understates sessions with large
    heart-rate swings (intervals) and strength work. An activity with no heart rate
    gets a NULL trimp, not 0. -#}
with s as (

    select * from {{ ref('silver_activities') }}

),

hr as (

    select
        *,
        case when avg_hr is not null then
            least(1.0, greatest(0.0,
                (avg_hr - {{ var('rest_hr') }}) / ({{ var('max_hr') }} - {{ var('rest_hr') }}.0)))
        end as hr_reserve_fraction
    from s

)

select
    activity_id,
    source,
    sport_type,
    sport_name,
    sport_group,
    is_indoor,
    local_date,
    date_trunc('week', local_date)::date            as week_start,
    start_time_utc,
    location,
    duration_s,
    duration_s / 60.0                               as duration_min,
    distance_m,
    distance_m / 1000.0                             as distance_km,
    sets,
    avg_hr,
    calories,
    avg_pace_s_per_km,
    quality_flag,
    hr_reserve_fraction,
    case when hr_reserve_fraction is not null then
        (duration_s / 60.0) * hr_reserve_fraction
        * {{ var('trimp_a') }} * exp({{ var('trimp_b') }} * hr_reserve_fraction)
    end                                             as trimp
from hr
