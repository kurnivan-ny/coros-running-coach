-- Weekly session counts must add up to the activity count, and so must load.
with a as (
    select count(*) as n, coalesce(sum(trimp), 0) as load from {{ ref('fct_activities') }}
),
w as (
    select coalesce(sum(sessions), 0) as n, coalesce(sum(trimp), 0) as load
    from {{ ref('weekly_summary') }}
)
select * from a, w where a.n <> w.n or abs(a.load - w.load) > 1.0
