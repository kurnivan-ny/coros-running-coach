{{ config(materialized='view') }}

{#- Typed view of every bronze payload, with the first failing validation rule.
    silver_activities keeps rows where reject_reason is null; silver_rejects the rest. -#}

with parsed as (

    select
        activity_id,
        source,
        payload_hash,
        payload ->> 'schema'                                        as schema_version,
        {{ safe_numeric("payload ->> 'sport_type'") }}::int         as sport_type,
        payload ->> 'sport_name'                                    as sport_name,
        case when (payload ->> 'activity_date') ~ '^\d{4}-\d{2}-\d{2}$'
             then (payload ->> 'activity_date')::date end           as local_date,
        to_timestamp({{ safe_numeric("payload ->> 'start_ts'") }})  as start_time_utc,
        to_timestamp({{ safe_numeric("payload ->> 'end_ts'") }})    as end_time_utc,
        payload ->> 'location'                                      as location,
        {{ safe_numeric("payload ->> 'duration_s'") }}              as duration_s,
        {{ safe_numeric("payload ->> 'distance_m'") }}              as distance_m,
        {{ safe_numeric("payload ->> 'sets'") }}::int               as sets,
        {{ safe_numeric("payload ->> 'avg_hr'") }}                  as avg_hr,
        {{ safe_numeric("payload ->> 'calories'") }}                as calories,
        {{ safe_numeric("payload ->> 'reported_pace_s_per_km'") }}  as reported_pace_s_per_km
    from {{ source('bronze', 'bronze_activities') }}

),

typed as (

    select
        *,
        {{ sport_group('sport_type') }}                             as sport_group,
        sport_type in (101, 201, 300, 400, 402, 701, 800)           as is_indoor
    from parsed

)

select
    t.*,
    case
        when t.schema_version is distinct from 'coros_record_v1'      then 'unsupported_schema'
        when activity_id like 'unknown-%'                           then 'missing_activity_id'
        when sport_type is null                                     then 'sport_missing'
        when start_time_utc is null                                 then 'bad_start_time'
        when local_date is null                                     then 'missing_date'
        when duration_s is null                                     then 'duration_missing'
        when duration_s <= 0                                        then 'duration_non_positive'
        when duration_s > 172800                                    then 'duration_out_of_range'
        when sport_group in ('run', 'walk', 'swim', 'cycling', 'hike')
             and distance_m is null                                 then 'distance_missing'
        when sport_group in ('run', 'walk', 'swim', 'cycling', 'hike')
             and distance_m <= 0                                    then 'distance_non_positive'
        when distance_m > 500000                                    then 'distance_out_of_range'
        when avg_hr is not null and (avg_hr < 20 or avg_hr > 260)   then 'hr_out_of_range'
        when sport_group = 'run' and distance_m > 0
             and duration_s / (distance_m / 1000.0) < 120           then 'pace_implausible'
        -- The same workout can arrive from two sources (COROS syncs to Strava). Keep the
        -- higher-priority copy; the other is quarantined, never silently dropped.
        when exists (
            select 1 from typed as o
            where o.activity_id <> t.activity_id
              and o.sport_group = t.sport_group
              and abs(extract(epoch from (o.start_time_utc - t.start_time_utc))) <= 120
              and {{ source_priority('o.source') }} < {{ source_priority('t.source') }}
        )                                                           then 'duplicate_activity'
    end                                                             as reject_reason,
    case
        when sport_group = 'run' and distance_m > 0
             and duration_s / (distance_m / 1000.0) > 720           then 'very_slow_pace'
    end                                                             as quality_flag
from typed as t
