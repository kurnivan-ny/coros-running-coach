-- The daily series must contain every activity's load exactly once.
with a as (
    select count(*) as n, coalesce(sum(trimp), 0) as load from {{ ref('fct_activities') }}
),
d as (
    select coalesce(sum(sessions), 0) as n, coalesce(sum(trimp), 0) as load
    from {{ ref('daily_load') }}
)
select * from a, d where a.n <> d.n or abs(a.load - d.load) > 1.0
