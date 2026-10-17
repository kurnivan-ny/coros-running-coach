{{ config(materialized='table') }}

/*
  Per-ISO-week training volume, Monday start.

  Weeks with no runs MUST appear as 0 km rows with NULL pace. A plain GROUP BY
  would silently omit them, which would make a missed week invisible and would
  also break week-over-week (the prior week would be the wrong week).
  The spine below guarantees every week between the first and last activity
  exists exactly once.
*/

with activities as (

    select
        {{ iso_week_start('start_date_utc') }} as week_start,
        distance_m,
        duration_s,
        avg_hr
    from {{ source('corach', 'silver_activities') }}

),

bounds as (
    select min(week_start) as first_week, max(week_start) as last_week
    from activities
),

spine as (
    -- Every Monday in range, including weeks with no activity at all.
    select cast(d as date) as week_start
    from bounds,
         generate_series(bounds.first_week, bounds.last_week, interval '7 day') as d
),

aggregated as (

    select
        week_start,
        count(*)                as runs,
        sum(distance_m)         as distance_m,
        sum(duration_s)         as duration_s,
        {{ weighted_avg_hr('avg_hr', 'duration_s') }} as avg_hr
    from activities
    group by week_start

),

joined as (

    select
        spine.week_start,
        coalesce(aggregated.runs, 0) as runs,
        -- 0 km is the correct value for a week with no runs: the athlete
        -- genuinely ran zero. Pace and HR stay NULL because they are undefined.
        coalesce(aggregated.distance_m, 0) / 1000.0 as distance_km,
        coalesce(aggregated.duration_s, 0)          as duration_s,
        aggregated.avg_hr,
        {{ pace_s_per_km('aggregated.distance_m', 'aggregated.duration_s') }} as pace_s_per_km
    from spine
    left join aggregated on aggregated.week_start = spine.week_start

)

select
    week_start,
    runs,
    round(cast(distance_km as numeric), 3) as distance_km,
    duration_s,
    pace_s_per_km,
    avg_hr,
    lag(distance_km) over (order by week_start) as prior_week_km,
    {{ safe_pct_change(
         'distance_km',
         'lag(distance_km) over (order by week_start)'
    ) }} as week_over_week_pct
from joined
order by week_start
