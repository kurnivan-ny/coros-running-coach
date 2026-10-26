{#- One row per calendar day from the first activity to today, rest days included.

    acute_load   mean daily TRIMP over the last 7 days
    chronic_load mean daily TRIMP over the last 28 days
    acwr         acute / chronic, only once `acwr_min_days` of history exist
    monotony_7d  mean / stddev of the last 7 daily loads (high = same load every day)
    strain_7d    7-day load sum x monotony

    Days with no activity count as 0 load, which is what makes the ratio meaningful.
    Days where an activity had no heart rate contribute 0 and are counted in
    `sessions_without_hr` so the gap is visible rather than silent. -#}
with bounds as (

    select
        min(local_date)                                   as first_day,
        greatest(max(local_date), current_date)           as last_day
    from {{ ref('fct_activities') }}

),

days as (

    select d::date as day
    from bounds, generate_series(bounds.first_day, bounds.last_day, interval '1 day') as d

),

daily as (

    select
        local_date,
        count(*)                                          as sessions,
        sum(duration_min)                                 as duration_min,
        sum(trimp)                                        as trimp,
        count(*) filter (where trimp is null)             as sessions_without_hr
    from {{ ref('fct_activities') }}
    group by local_date

),

joined as (

    select
        d.day,
        coalesce(a.sessions, 0)                           as sessions,
        coalesce(a.duration_min, 0)                       as duration_min,
        coalesce(a.trimp, 0)                              as trimp,
        coalesce(a.sessions_without_hr, 0)                as sessions_without_hr,
        row_number() over (order by d.day)                as day_index
    from days d
    left join daily a on a.local_date = d.day

),

rolled as (

    select
        *,
        avg(trimp)        over (order by day rows between 6 preceding and current row)  as acute_load,
        avg(trimp)        over (order by day rows between 27 preceding and current row) as chronic_raw,
        avg(trimp)        over (order by day rows between 6 preceding and current row)  as mean_7d,
        stddev_pop(trimp) over (order by day rows between 6 preceding and current row)  as sd_7d,
        sum(trimp)        over (order by day rows between 6 preceding and current row)  as load_7d
    from joined

)

select
    day,
    sessions,
    round(duration_min::numeric, 1)                       as duration_min,
    round(trimp::numeric, 1)                              as trimp,
    sessions_without_hr,
    round(acute_load::numeric, 2)                         as acute_load,
    case when day_index >= 28 then round(chronic_raw::numeric, 2) end   as chronic_load,
    case when day_index >= {{ var('acwr_min_days') }} and chronic_raw > 0
         then round((acute_load / chronic_raw)::numeric, 2) end         as acwr,
    case when day_index >= 7 and sd_7d > 0
         then round((mean_7d / sd_7d)::numeric, 2) end                  as monotony_7d,
    case when day_index >= 7 and sd_7d > 0
         then round((load_7d * mean_7d / sd_7d)::numeric, 1) end        as strain_7d,
    case
        when day_index < {{ var('acwr_min_days') }} or chronic_raw <= 0 then 'insufficient_history'
        when acute_load / chronic_raw < 0.8                             then 'undertraining'
        when acute_load / chronic_raw <= 1.3                            then 'optimal'
        when acute_load / chronic_raw <= 1.5                            then 'caution'
        else 'high_risk'
    end                                                   as acwr_zone
from rolled
