"""Whitelisted, read-only fact tools.

Every number the coach can state originates here. The LLM never writes SQL and
never computes a statistic: it receives this module's output as fixed facts and
may only phrase them. Integer arguments are validated and bounded so a prompt
cannot ask for an unbounded scan.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from ..ingest import load_silver
from ..metrics import (
    format_pace,
    week_over_week,
    weekly_volume,
    window_summary,
)
from ..quality import freshness
from ..store import Store

# Bounds for caller-supplied integers.
MAX_WEEKS = 52
MAX_WINDOW_DAYS = 365
MAX_LIMIT = 50
ALLOWED_WINDOWS = (7, 28, 90)


class ToolArgumentError(ValueError):
    """An argument was missing, non-integer, or outside its allowed range."""


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


def _rows(store: Store) -> list[dict[str, Any]]:
    return load_silver(store)


def data_coverage(store: Store, **_: Any) -> dict[str, Any]:
    """How much history exists, and whether the pipeline ran recently."""
    rows = _rows(store)
    runs = store.query(
        "SELECT started_at, status FROM ops_pipeline_runs "
        "WHERE status = 'success' ORDER BY started_at DESC LIMIT 1"
    )
    last_success = runs[0]["started_at"] if runs else None
    fresh = freshness(last_success)
    sources = sorted({r["source"] for r in rows})
    return {
        "tool": "data_coverage",
        "activity_count": len(rows),
        "first_activity_date": rows[0]["start_date_utc"] if rows else None,
        "last_activity_date": rows[-1]["start_date_utc"] if rows else None,
        "sources": sources,
        "is_synthetic": "synthetic" in sources,
        "last_successful_run_at": fresh.last_success_at,
        "pipeline_age_hours": (None if fresh.age_hours is None else round(fresh.age_hours, 2)),
        "pipeline_is_fresh": fresh.is_fresh,
    }


def window(store: Store, days: Any = 7, today: date | None = None, **_: Any) -> dict[str, Any]:
    """Totals over a trailing 7/28/90-day window."""
    days_i = bounded_int(days, "days", 1, MAX_WINDOW_DAYS, default=7)
    if days_i not in ALLOWED_WINDOWS:
        raise ToolArgumentError(f"days must be one of {ALLOWED_WINDOWS}, got {days_i}")
    s = window_summary(_rows(store), days=days_i, today=today)
    return {
        "tool": "window",
        "days": s.days,
        "runs": s.runs,
        "distance_km": round(s.distance_km, 2),
        "duration_s": round(s.duration_s, 1),
        "pace_s_per_km": (None if s.pace_s_per_km is None else round(s.pace_s_per_km, 1)),
        "pace_per_km": format_pace(s.pace_s_per_km),
        "avg_hr": (None if s.avg_hr is None else round(s.avg_hr, 1)),
        "sufficient_history": s.sufficient,
    }


def weekly_volume_tool(store: Store, weeks: Any = 8, today: date | None = None,
                       **_: Any) -> dict[str, Any]:
    """Per-ISO-week distance, including weeks with no runs."""
    weeks_i = bounded_int(weeks, "weeks", 1, MAX_WEEKS, default=8)
    summaries = weekly_volume(_rows(store), weeks=weeks_i, today=today)
    return {
        "tool": "weekly_volume",
        "weeks": weeks_i,
        "series": [
            {
                "week_start": w.week_start,
                "distance_km": round(w.distance_km, 2),
                "runs": w.runs,
                "pace_per_km": format_pace(w.pace_s_per_km),
                "avg_hr": (None if w.avg_hr is None else round(w.avg_hr, 1)),
            }
            for w in summaries
        ],
        "latest_week_km": round(summaries[-1].distance_km, 2) if summaries else None,
        "week_over_week_pct": (
            None if (wow := week_over_week(summaries)) is None else round(wow * 100, 1)
        ),
    }


def longest_run(store: Store, **_: Any) -> dict[str, Any]:
    """The single longest validated run by distance."""
    rows = store.query(
        "SELECT activity_id, start_date_utc, distance_m, duration_s, avg_hr "
        "FROM silver_activities ORDER BY distance_m DESC LIMIT 1"
    )
    if not rows:
        return {"tool": "longest_run", "found": False}
    r = rows[0]
    pace = (r["duration_s"] / (r["distance_m"] / 1000.0)) if r["distance_m"] else None
    return {
        "tool": "longest_run",
        "found": True,
        "date": r["start_date_utc"],
        "distance_km": round(r["distance_m"] / 1000.0, 2),
        "duration_s": round(r["duration_s"], 1),
        "pace_per_km": format_pace(pace),
        "avg_hr": (None if r["avg_hr"] is None else round(r["avg_hr"], 1)),
    }


def recent_runs(store: Store, limit: Any = 5, **_: Any) -> dict[str, Any]:
    """The most recent validated runs, newest first."""
    n = bounded_int(limit, "limit", 1, MAX_LIMIT, default=5)
    rows = store.query(
        "SELECT activity_id, start_date_utc, distance_m, duration_s, avg_hr "
        f"FROM silver_activities ORDER BY start_time_utc DESC LIMIT {n}"
    )
    return {
        "tool": "recent_runs",
        "limit": n,
        "runs": [
            {
                "date": r["start_date_utc"],
                "distance_km": round(r["distance_m"] / 1000.0, 2),
                "duration_s": round(r["duration_s"], 1),
                "pace_per_km": format_pace(
                    r["duration_s"] / (r["distance_m"] / 1000.0) if r["distance_m"] else None
                ),
                "avg_hr": (None if r["avg_hr"] is None else round(r["avg_hr"], 1)),
            }
            for r in rows
        ],
    }


def trend(store: Store, days: Any = 28, today: date | None = None, **_: Any) -> dict[str, Any]:
    """Compare the trailing window against the window immediately before it."""
    days_i = bounded_int(days, "days", 1, MAX_WINDOW_DAYS, default=28)
    if days_i not in ALLOWED_WINDOWS:
        raise ToolArgumentError(f"days must be one of {ALLOWED_WINDOWS}, got {days_i}")

    from datetime import timedelta

    today = today or date.today()
    rows = _rows(store)
    current = window_summary(rows, days=days_i, today=today)
    prior_end = today - timedelta(days=days_i)
    prior = window_summary(rows, days=days_i, today=prior_end)

    if prior.distance_km > 0:
        change_pct = round(
            (current.distance_km - prior.distance_km) / prior.distance_km * 100, 1
        )
    else:
        change_pct = None  # undefined against an empty prior window

    return {
        "tool": "trend",
        "days": days_i,
        "current_km": round(current.distance_km, 2),
        "prior_km": round(prior.distance_km, 2),
        "change_pct": change_pct,
        "current_pace_per_km": format_pace(current.pace_s_per_km),
        "prior_pace_per_km": format_pace(prior.pace_s_per_km),
        "current_avg_hr": (None if current.avg_hr is None else round(current.avg_hr, 1)),
        "sufficient_history": current.sufficient and prior.sufficient,
    }


# The whitelist. Nothing outside this mapping is callable from chat.
TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "data_coverage": data_coverage,
    "window": window,
    "weekly_volume": weekly_volume_tool,
    "longest_run": longest_run,
    "recent_runs": recent_runs,
    "trend": trend,
}


def call_tool(store: Store, name: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke a whitelisted tool by name."""
    fn = TOOLS.get(name)
    if fn is None:
        raise ToolArgumentError(f"unknown tool {name!r}; allowed: {sorted(TOOLS)}")
    return fn(store, **kwargs)
