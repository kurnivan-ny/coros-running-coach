"""Whitelisted, read-only fact tools over the dbt gold layer.

Every number the coach can state originates here. The LLM never writes SQL and
never computes a statistic: it receives this module's output as fixed facts and
may only phrase them. Integer and enum arguments are validated and bounded so a
prompt cannot ask for an unbounded scan, and every SQL statement below is a
constant with bound parameters -- there is no code path that executes
model-supplied SQL.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from ..freshness import freshness
from ..store import Store

# Bounds for caller-supplied values.
MAX_WEEKS = 52
MAX_LIMIT = 50
ALLOWED_WINDOWS = (7, 28, 90)
SPORT_GROUPS = ("run", "strength", "walk", "swim", "cycling", "hike", "mobility", "other")

# Gold tables. dbt builds them in the ``gold`` schema; tests attach a database
# under the same name, so the SQL is identical on both backends.
FCT = "gold.fct_activities"
WEEKLY = "gold.weekly_summary"
DAILY_LOAD = "gold.daily_load"
REJECTS = "silver.silver_rejects"


class ToolArgumentError(ValueError):
    """An argument was missing, malformed, or outside its allowed range."""


def bounded_int(value: Any, name: str, low: int, high: int, default: int | None = None) -> int:
    """Coerce to int and enforce ``low <= value <= high``."""
    if value is None:
        if default is None:
            raise ToolArgumentError(f"{name} is required")
        return default
    if isinstance(value, bool):
        raise ToolArgumentError(f"{name} must be an integer, got bool")
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolArgumentError(f"{name} must be an integer, got {value!r}") from exc
    if not (low <= out <= high):
        raise ToolArgumentError(f"{name} must be between {low} and {high}, got {out}")
    return out


def sport_arg(value: Any, default: str | None = None) -> str | None:
    """Validate a sport group against the fixed list (None means all sports)."""
    if value is None:
        return default
    if value not in SPORT_GROUPS:
        raise ToolArgumentError(f"sport must be one of {SPORT_GROUPS}, got {value!r}")
    return str(value)


def window_arg(value: Any, default: int) -> int:
    days = bounded_int(value, "days", 1, 365, default=default)
    if days not in ALLOWED_WINDOWS:
        raise ToolArgumentError(f"days must be one of {ALLOWED_WINDOWS}, got {days}")
    return days


# -- value helpers ---------------------------------------------------------
def num(value: Any, ndigits: int = 2) -> float | None:
    """Decimal/int/float -> rounded float. None stays None (never 0)."""
    if value is None:
        return None
    return int(round(float(value))) if ndigits == 0 else round(float(value), ndigits)


def iso(value: Any) -> str | None:
    return None if value is None else str(value)[:10]


def format_pace(seconds_per_km: float | None) -> str | None:
    """Seconds/km -> 'M:SS'. None stays None so callers cannot print a fake 0:00."""
    if seconds_per_km is None:
        return None
    total = int(round(seconds_per_km))
    return f"{total // 60}:{total % 60:02d}"


def _sport_clause(sport: str | None) -> tuple[str, tuple[Any, ...]]:
    return ("", ()) if sport is None else (" AND sport_group = ?", (sport,))


def _window_totals(store: Store, start: date, end: date, sport: str | None) -> dict[str, Any]:
    clause, params = _sport_clause(sport)
    row = store.query(
        "SELECT COUNT(*) AS sessions, SUM(duration_s) AS duration_s, "
        "SUM(distance_m) AS distance_m, SUM(trimp) AS trimp, "
        "SUM(CASE WHEN avg_hr IS NOT NULL THEN avg_hr * duration_s END) AS hr_num, "
        "SUM(CASE WHEN avg_hr IS NOT NULL THEN duration_s END) AS hr_den, "
        "SUM(CASE WHEN distance_m > 0 THEN duration_s END) AS pace_dur, "
        "SUM(CASE WHEN distance_m > 0 THEN distance_m END) AS pace_dist, "
        "SUM(sets) AS sets "
        f"FROM {FCT} WHERE local_date >= ? AND local_date <= ?{clause}",  # nosec B608
        (start.isoformat(), end.isoformat(), *params),
    )[0]
    pace = None
    if row["pace_dist"] and float(row["pace_dist"]) > 0:
        pace = float(row["pace_dur"]) / (float(row["pace_dist"]) / 1000.0)
    hr = None
    if row["hr_den"] and float(row["hr_den"]) > 0:
        hr = float(row["hr_num"]) / float(row["hr_den"])
    dist = row["distance_m"]
    return {
        "sessions": int(row["sessions"] or 0),
        "distance_km": num(float(dist) / 1000.0) if dist is not None else 0.0,
        "duration_min": num(float(row["duration_s"]) / 60.0, 1) if row["duration_s"] else 0.0,
        "pace_s_per_km": num(pace, 1),
        "avg_hr": num(hr, 0),
        "trimp": num(row["trimp"], 1) if row["trimp"] is not None else 0.0,
        "sets": int(row["sets"]) if row["sets"] is not None else None,
    }


def _first_activity(store: Store, sport: str | None) -> date | None:
    clause, params = _sport_clause(sport)
    rows = store.query(
        f"SELECT MIN(local_date) AS d FROM {FCT} WHERE 1 = 1{clause}", params)  # nosec B608
    d = iso(rows[0]["d"]) if rows else None
    return date.fromisoformat(d) if d else None


# -- tools -----------------------------------------------------------------
def data_coverage(store: Store, **_: Any) -> dict[str, Any]:
    """How much history exists, which sports, and whether the pipeline ran recently."""
    span = store.query(
        f"SELECT COUNT(*) AS n, MIN(local_date) AS first_d, MAX(local_date) AS last_d "
        f"FROM {FCT}")[0]  # nosec B608
    sources = sorted(r["source"] for r in store.query(f"SELECT DISTINCT source FROM {FCT}"))  # nosec B608
    sports = store.query(
        f"SELECT sport_group, COUNT(*) AS n FROM {FCT} GROUP BY sport_group "  # nosec B608
        "ORDER BY n DESC")
    rejected = store.query(f"SELECT COUNT(*) AS n FROM {REJECTS}")[0]["n"]  # nosec B608
    runs = store.query(
        "SELECT started_at FROM ops_pipeline_runs "
        "WHERE status = 'success' ORDER BY started_at DESC LIMIT 1")
    fresh = freshness(runs[0]["started_at"] if runs else None)
    return {
        "tool": "data_coverage",
        "activity_count": int(span["n"]),
        "first_activity_date": iso(span["first_d"]),
        "last_activity_date": iso(span["last_d"]),
        "sources": sources,
        "is_synthetic": "synthetic" in sources,
        "sport_counts": {r["sport_group"]: int(r["n"]) for r in sports},
        "rejected_rows": int(rejected),
        "last_successful_run_at": fresh.last_success_at,
        "pipeline_age_hours": num(fresh.age_hours),
        "pipeline_is_fresh": fresh.is_fresh,
    }


def window(store: Store, days: Any = 7, sport: Any = "run", today: date | None = None,
           **_: Any) -> dict[str, Any]:
    """Totals over a trailing 7/28/90-day window for one sport group."""
    days_i = window_arg(days, 7)
    sport_s = sport_arg(sport, "run")
    today = today or date.today()
    start = today - timedelta(days=days_i - 1)
    totals = _window_totals(store, start, today, sport_s)
    first = _first_activity(store, sport_s)
    return {
        "tool": "window",
        "days": days_i,
        "sport": sport_s,
        **totals,
        "pace_per_km": format_pace(totals["pace_s_per_km"]),
        "sufficient_history": first is not None and first <= start,
    }


def weekly_volume(store: Store, weeks: Any = 8, sport: Any = "run",
                  today: date | None = None, **_: Any) -> dict[str, Any]:
    """Per-ISO-week totals including weeks with no activity (shown as zero)."""
    weeks_i = bounded_int(weeks, "weeks", 1, MAX_WEEKS, default=8)
    sport_s = sport_arg(sport, "run")
    today = today or date.today()
    current = today - timedelta(days=today.weekday())
    starts = [current - timedelta(weeks=i) for i in range(weeks_i - 1, -1, -1)]
    rows = store.query(
        "SELECT week_start, sessions, distance_km, duration_min, trimp, avg_hr, "
        f"pace_s_per_km FROM {WEEKLY} WHERE sport_group = ? AND week_start >= ? "  # nosec B608
        "ORDER BY week_start",
        (sport_s, starts[0].isoformat()),
    )
    by_week = {iso(r["week_start"]): r for r in rows}
    series = []
    for s in starts:
        r = by_week.get(s.isoformat())
        series.append({
            "week_start": s.isoformat(),
            "sessions": int(r["sessions"]) if r else 0,
            "distance_km": num(r["distance_km"]) if r and r["distance_km"] is not None else 0.0,
            "duration_min": num(r["duration_min"], 1) if r else 0.0,
            "trimp": num(r["trimp"], 1) if r and r["trimp"] is not None else 0.0,
            "pace_per_km": format_pace(num(r["pace_s_per_km"], 1) if r else None),
            "avg_hr": num(r["avg_hr"], 0) if r else None,
        })
    current_week = series[-1]
    complete = series[:-1]
    last_full = complete[-1] if complete else None
    before = complete[-2] if len(complete) >= 2 else None
    wow = None
    if last_full is not None and before is not None and before["distance_km"] > 0:
        wow = round((last_full["distance_km"] - before["distance_km"])
                    / before["distance_km"] * 100, 1)
    return {
        "tool": "weekly_volume",
        "weeks": weeks_i,
        "sport": sport_s,
        "series": series,
        "this_week_km": current_week["distance_km"],
        "this_week_min": current_week["duration_min"],
        "this_week_sessions": current_week["sessions"],
        "last_full_week_km": last_full["distance_km"] if last_full else None,
        "last_full_week_min": last_full["duration_min"] if last_full else None,
        "last_full_week_sessions": last_full["sessions"] if last_full else None,
        "week_over_week_pct": wow,
    }


def longest_run(store: Store, **_: Any) -> dict[str, Any]:
    """The single longest validated run by distance."""
    rows = store.query(
        "SELECT local_date, distance_km, duration_s, avg_hr, avg_pace_s_per_km "
        f"FROM {FCT} WHERE sport_group = 'run' ORDER BY distance_km DESC LIMIT 1")  # nosec B608
    if not rows:
        return {"tool": "longest_run", "found": False}
    r = rows[0]
    return {
        "tool": "longest_run",
        "found": True,
        "date": iso(r["local_date"]),
        "distance_km": num(r["distance_km"]),
        "duration_min": num(float(r["duration_s"]) / 60.0, 1),
        "pace_per_km": format_pace(num(r["avg_pace_s_per_km"], 1)),
        "avg_hr": num(r["avg_hr"], 0),
    }


def recent_activities(store: Store, limit: Any = 5, sport: Any = None,
                      **_: Any) -> dict[str, Any]:
    """The most recent validated activities, newest first."""
    n = bounded_int(limit, "limit", 1, MAX_LIMIT, default=5)
    sport_s = sport_arg(sport)
    clause, params = _sport_clause(sport_s)
    rows = store.query(
        "SELECT local_date, sport_name, sport_group, distance_km, duration_min, avg_hr, "
        f"avg_pace_s_per_km, sets, trimp FROM {FCT} WHERE 1 = 1{clause} "  # nosec B608
        "ORDER BY start_time_utc DESC LIMIT ?",
        (*params, n),
    )
    return {
        "tool": "recent_activities",
        "limit": n,
        "sport": sport_s,
        "activities": [
            {
                "date": iso(r["local_date"]),
                "sport": r["sport_name"],
                "distance_km": num(r["distance_km"]),
                "duration_min": num(r["duration_min"], 1),
                "pace_per_km": format_pace(num(r["avg_pace_s_per_km"], 1)),
                "avg_hr": num(r["avg_hr"], 0),
                "sets": int(r["sets"]) if r["sets"] is not None else None,
                "trimp": num(r["trimp"], 1),
            }
            for r in rows
        ],
    }


def trend(store: Store, days: Any = 28, sport: Any = "run", today: date | None = None,
          **_: Any) -> dict[str, Any]:
    """Compare the trailing window against the window immediately before it."""
    days_i = window_arg(days, 28)
    sport_s = sport_arg(sport, "run")
    today = today or date.today()
    cur_start = today - timedelta(days=days_i - 1)
    prior_end = cur_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days_i - 1)
    cur = _window_totals(store, cur_start, today, sport_s)
    prior = _window_totals(store, prior_start, prior_end, sport_s)
    first = _first_activity(store, sport_s)
    change = None
    if prior["distance_km"] and prior["distance_km"] > 0:
        change = round((cur["distance_km"] - prior["distance_km"]) / prior["distance_km"] * 100, 1)
    return {
        "tool": "trend",
        "days": days_i,
        "sport": sport_s,
        "current_km": cur["distance_km"],
        "prior_km": prior["distance_km"],
        "change_pct": change,
        "current_sessions": cur["sessions"],
        "prior_sessions": prior["sessions"],
        "current_pace_per_km": format_pace(cur["pace_s_per_km"]),
        "prior_pace_per_km": format_pace(prior["pace_s_per_km"]),
        "current_avg_hr": cur["avg_hr"],
        "sufficient_history": first is not None and first <= prior_start,
    }


def sport_breakdown(store: Store, days: Any = 28, today: date | None = None,
                    **_: Any) -> dict[str, Any]:
    """Sessions, time and load per sport group over a trailing window."""
    days_i = window_arg(days, 28)
    today = today or date.today()
    start = today - timedelta(days=days_i - 1)
    rows = store.query(
        "SELECT sport_group, COUNT(*) AS sessions, SUM(duration_min) AS duration_min, "
        f"SUM(distance_km) AS distance_km, SUM(trimp) AS trimp FROM {FCT} "  # nosec B608
        "WHERE local_date >= ? AND local_date <= ? GROUP BY sport_group "
        "ORDER BY SUM(duration_min) DESC",
        (start.isoformat(), today.isoformat()),
    )
    return {
        "tool": "sport_breakdown",
        "days": days_i,
        "sports": [
            {
                "sport": r["sport_group"],
                "sessions": int(r["sessions"]),
                "duration_min": num(r["duration_min"], 1),
                "distance_km": num(r["distance_km"]) if r["distance_km"] is not None else None,
                "trimp": num(r["trimp"], 1) if r["trimp"] is not None else None,
            }
            for r in rows
        ],
    }


def workload(store: Store, **_: Any) -> dict[str, Any]:
    """Latest acute/chronic load state from the daily series."""
    rows = store.query(
        "SELECT day, acute_load, chronic_load, acwr, acwr_zone, monotony_7d, strain_7d "
        f"FROM {DAILY_LOAD} ORDER BY day DESC LIMIT 1")  # nosec B608
    if not rows:
        return {"tool": "workload", "found": False}
    r = rows[0]
    return {
        "tool": "workload",
        "found": True,
        "as_of": iso(r["day"]),
        "acute_load": num(r["acute_load"], 1),
        "chronic_load": num(r["chronic_load"], 1),
        "acwr": num(r["acwr"]),
        "acwr_zone": r["acwr_zone"],
        "monotony_7d": num(r["monotony_7d"]),
        "strain_7d": num(r["strain_7d"], 1),
    }


def training_plan(store: Store, sessions_per_week: Any = 5, strength_per_week: Any = 1,
                  weeks_to_race: Any = None, today: date | None = None,
                  **_: Any) -> dict[str, Any]:
    """Next seven days of training, built from recent weekly volume and current load."""
    from .plan import PlanInputs, build_plan

    sessions = bounded_int(sessions_per_week, "sessions_per_week", 1, 6, default=5)
    strength = bounded_int(strength_per_week, "strength_per_week", 0, 2, default=1)
    race = (None if weeks_to_race is None
            else bounded_int(weeks_to_race, "weeks_to_race", 0, 52))
    today = today or date.today()
    # The last entry is the current, partial week; the plan uses complete weeks only.
    weekly = weekly_volume(store, weeks=5, sport="run", today=today)["series"][:-1]
    longest = store.query(
        f"SELECT MAX(distance_km) AS d FROM {FCT} WHERE sport_group = 'run' "  # nosec B608
        "AND local_date >= ?", ((today - timedelta(days=56)).isoformat(),))[0]["d"]
    recent = _window_totals(store, today - timedelta(days=27), today, "run")
    load = workload(store)
    return build_plan(PlanInputs(
        last_weeks_km=[w["distance_km"] for w in weekly],
        acwr_zone=load.get("acwr_zone") if load.get("found") else None,
        longest_recent_km=num(longest),
        typical_pace_s_per_km=recent["pace_s_per_km"],
        sessions_per_week=sessions,
        strength_per_week=strength,
        weeks_to_race=race,
        start=today,
    ))


# The whitelist. Nothing outside this mapping is callable from chat.
TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "data_coverage": data_coverage,
    "window": window,
    "weekly_volume": weekly_volume,
    "longest_run": longest_run,
    "recent_activities": recent_activities,
    "trend": trend,
    "sport_breakdown": sport_breakdown,
    "workload": workload,
    "training_plan": training_plan,
}


def call_tool(store: Store, name: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke a whitelisted tool by name."""
    fn = TOOLS.get(name)
    if fn is None:
        raise ToolArgumentError(f"unknown tool {name!r}; allowed: {sorted(TOOLS)}")
    return fn(store, **kwargs)
