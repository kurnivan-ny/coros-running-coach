{#
  Metric macros mirroring src/corach/metrics.py.

  metrics.py is the AUTHORITATIVE definition. These macros exist so the gold
  models cannot drift from each other, and tests/test_gold_parity.py asserts
  this SQL agrees with the Python for the same rows.

  The NULL rules are the whole point:
    * pace comes from SUMMED totals, never an average of per-run paces
    * HR is duration-weighted over ONLY the runs reporting HR
    * an undefined result is NULL, never 0
#}

{# Seconds per km from totals. NULL when distance is missing or non-positive. #}
{% macro pace_s_per_km(distance_m, duration_s) %}
    case
        when coalesce({{ distance_m }}, 0) > 0
            then {{ duration_s }} / ({{ distance_m }} / 1000.0)
        else null
    end
{% endmacro %}


{#
  Duration-weighted mean HR.

  SUM ignores NULL rows, so a run without HR contributes to neither the
  numerator nor the denominator -- it is excluded, not counted as zero.
  Plain AVG(avg_hr) would be wrong twice over: unweighted, and it would treat
  every run as equal regardless of length.
#}
{% macro weighted_avg_hr(hr_col, duration_col) %}
    case
        when sum(case when {{ hr_col }} is not null then {{ duration_col }} end) > 0
            then sum(case when {{ hr_col }} is not null
                          then {{ hr_col }} * {{ duration_col }} end)
                 / sum(case when {{ hr_col }} is not null then {{ duration_col }} end)
        else null
    end
{% endmacro %}


{# Monday of the ISO week, as a date. #}
{% macro iso_week_start(ts_col) %}
    cast(date_trunc('week', cast({{ ts_col }} as date)) as date)
{% endmacro %}


{#
  Fractional change, NULL-safe.

  A change measured against zero is undefined, not infinite and not 100%.
  Returning NULL here is what keeps "week-over-week" honest for a week that
  followed a week with no running at all.
#}
{% macro safe_pct_change(current_col, prior_col) %}
    case
        when {{ prior_col }} is null or {{ prior_col }} = 0 then null
        else ({{ current_col }} - {{ prior_col }}) / {{ prior_col }} * 100.0
    end
{% endmacro %}
