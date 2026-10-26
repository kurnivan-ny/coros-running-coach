-- Every bronze row must land in exactly one of silver_activities or silver_rejects.
-- Returns a row (= test failure) when the counts do not add up.
with counts as (
    select
        (select count(*) from {{ source('bronze', 'bronze_activities') }}) as bronze_rows,
        (select count(*) from {{ ref('silver_activities') }})              as silver_rows,
        (select count(*) from {{ ref('silver_rejects') }})                 as reject_rows
)
select * from counts where bronze_rows <> silver_rows + reject_rows
