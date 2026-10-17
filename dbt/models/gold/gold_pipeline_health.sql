{{ config(materialized='table') }}

/*
  One-row pipeline health summary for the dashboard's freshness banner.

  Freshness is measured from the last SUCCESSFUL run. A failed run that happened
  five minutes ago does not make the data fresh.
*/

with runs as (

    select status, started_at, finished_at, rows_silver, rows_rejected, error
    from {{ source('corach', 'ops_pipeline_runs') }}

),

last_success as (
    select max(cast(started_at as timestamptz)) as last_success_at
    from runs
    where status = 'success'
),

totals as (
    select
        count(*)                                          as total_runs,
        sum(case when status = 'failed' then 1 else 0 end) as failed_runs,
        sum(coalesce(rows_rejected, 0))                   as total_rejected
    from runs
)

select
    last_success.last_success_at,
    -- NULL, not a large number, when no run has ever succeeded: the age is
    -- unknown rather than infinite.
    case
        when last_success.last_success_at is null then null
        else extract(epoch from (now() - last_success.last_success_at)) / 3600.0
    end as hours_since_success,
    case
        when last_success.last_success_at is null then false
        else (now() - last_success.last_success_at) <= interval '36 hour'
    end as is_fresh,
    totals.total_runs,
    totals.failed_runs,
    totals.total_rejected
from last_success
cross join totals
