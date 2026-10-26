{#- One row per ISO week (Monday start, athlete's local date) and sport group.
    Weeks with no activity have no row; the dashboard fills them with zeros.

    pace_s_per_km  total duration / total distance (NOT a mean of per-activity paces)
    avg_hr         duration-weighted over only the activities that report heart rate -#}
select
    week_start,
    sport_group,
    count(*)                                                    as sessions,
    round(sum(duration_min)::numeric, 1)                        as duration_min,
    round(sum(distance_km)::numeric, 2)                         as distance_km,
    round(sum(trimp)::numeric, 1)                               as trimp,
    sum(sets)                                                   as sets,
    round((sum(avg_hr * duration_s) filter (where avg_hr is not null)
           / nullif(sum(duration_s) filter (where avg_hr is not null), 0))::numeric, 1)
                                                                as avg_hr,
    case when sport_group in ('run', 'walk', 'hike', 'cycling') then
        round((sum(duration_s) filter (where distance_m > 0)
               / nullif(sum(distance_m) filter (where distance_m > 0) / 1000.0, 0))::numeric, 1)
    end                                                         as pace_s_per_km
from {{ ref('fct_activities') }}
group by week_start, sport_group
