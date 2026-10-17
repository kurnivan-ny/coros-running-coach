{{ config(materialized='table') }}

/*
  Trailing 7/28/90-day training load, one row per window.

  `sufficient_history` reports whether the data actually spans the window. A
  28-day total built from three days of history is not a 28-day total, and
  presenting it as one invites a false "your volume dropped" conclusion.
*/

with activities as (

    select start_date_utc, distance_m, duration_s, avg_hr
    from {{ source('corach', 'silver_activities') }}

),

reference as (
    -- Anchored to the newest activity, not to now(), so the table is stable
    -- and reproducible regardless of when dbt happens to run.
    select
        max(cast(start_date_utc as date)) as as_of_date,
        min(cast(start_date_utc as date)) as first_date
    from activities
),

windows as (
    select 7 as days
    union all select 28
    union all select 90
),

windowed as (

    select
        w.days,
        r.as_of_date,
        r.first_date,
        a.distance_m,
        a.duration_s,
        a.avg_hr
    from windows w
    cross join reference r
    left join activities a
        on cast(a.start_date_utc as date)
               between r.as_of_date - (w.days - 1) and r.as_of_date

)

select
    days,
    as_of_date,
    (as_of_date - (days - 1))                       as window_start,
    count(distance_m)                               as runs,
    round(cast(coalesce(sum(distance_m), 0) / 1000.0 as numeric), 3) as distance_km,
    coalesce(sum(duration_s), 0)                    as duration_s,
    {{ pace_s_per_km('sum(distance_m)', 'sum(duration_s)') }} as pace_s_per_km,
    {{ weighted_avg_hr('avg_hr', 'duration_s') }}   as avg_hr,
    -- True only when history reaches back to the start of the window.
    (min(first_date) <= (as_of_date - (days - 1)))  as sufficient_history
from windowed
group by days, as_of_date
order by days
