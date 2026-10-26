{#- Cast a JSON text value to numeric, or NULL when it is absent or not a number.
    A malformed payload must become a quarantined row, not a failed dbt run. -#}
{% macro safe_numeric(expr) -%}
    case when ({{ expr }}) ~ '^-?[0-9]+(\.[0-9]+)?$' then ({{ expr }})::numeric end
{%- endmacro %}

{#- COROS sport type code -> coarse group used by every dashboard breakdown. -#}
{% macro sport_group(sport_type) -%}
    case
        when {{ sport_type }} in (100, 101, 102, 103) then 'run'
        when {{ sport_type }} in (104, 105, 106) then 'hike'
        when {{ sport_type }} in (200, 201, 202, 203, 204, 205, 299) then 'cycling'
        when {{ sport_type }} in (300, 301) then 'swim'
        when {{ sport_type }} in (400, 401, 402, 1200) then 'strength'
        when {{ sport_type }} = 900 then 'walk'
        when {{ sport_type }} in (904, 905) then 'mobility'
        else 'other'
    end
{%- endmacro %}

{#- Which copy wins when two sources report the same workout: COROS, then Strava. -#}
{% macro source_priority(source_col) -%}
    case {{ source_col }} when 'coros_snapshot' then 1 when 'strava' then 2 else 3 end
{%- endmacro %}
