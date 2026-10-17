"""Metric computations. Pure Python, no database access, no third-party deps.

Conventions (see README "Metric definitions"):
  * Pace  = total duration / total distance over the period -- NOT a mean of
    per-run paces, which would over-weight short runs.
  * Avg HR = duration-weighted mean over only those runs reporting HR. Runs
    without HR are excluded from the weighting, never counted as 0.
  * Weeks = ISO weeks, Monday start, UTC dates.
  * Undefined results are None, never 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

M_PER_KM = 1000.0


def _d(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def iso_week_start(day: Any) -> date:
    """Monday of the ISO week containing ``day``."""
    d = _d(day)
    return d - timedelta(days=d.weekday())


def pace_s_per_km(distance_m: float | None, duration_s: float | None) -> float | None:
    """Seconds per km. None when either input is missing or distance is non-positive."""
    if distance_m is None or duration_s is None or distance_m <= 0:
        return None
    return duration_s / (distance_m / M_PER_KM)


def format_pace(pace: float | None) -> str | None:
    """Seconds/km -> 'M:SS'. None stays None so callers cannot print a fake 0:00."""
    if pace is None:
        return None
    total = int(round(pace))
    return f"{total // 60}:{total % 60:02d}"


def weighted_avg_hr(rows: Iterable[dict[str, Any]]) -> float | None:
    """Duration-weighted mean HR over rows that report one. None if no row does."""
    num = 0.0
    den = 0.0
    for r in rows:
        hr, dur = r.get("avg_hr"), r.get("duration_s")
        if hr is None or dur is None or dur <= 0:
            continue  # excluded entirely -- not counted as zero
        num += float(hr) * float(dur)
        den += float(dur)
    return num / den if den > 0 else None


@dataclass(frozen=True)
class WeekSummary:
    week_start: str
    distance_km: float
    duration_s: float
    runs: int
    pace_s_per_km: float | None
    avg_hr: float | None


def weekly_volume(rows: Sequence[dict[str, Any]], weeks: int = 12,
                  today: date | None = None) -> list[WeekSummary]:
    """Per-ISO-week totals, oldest first, including weeks with no runs.

    A zero-run week reports 0.0 km with pace None and avg_hr None.
    """
    today = today or date.today()
    current = iso_week_start(today)
    starts = [current - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]

    buckets: dict[date, list[dict[str, Any]]] = {s: [] for s in starts}
    for r in rows:
        if r.get("start_date_utc") is None:
            continue
        ws = iso_week_start(r["start_date_utc"])
        if ws in buckets:
            buckets[ws].append(r)

    out: list[WeekSummary] = []
    for s in starts:
        group = buckets[s]
        dist = sum(float(r["distance_m"]) for r in group if r.get("distance_m") is not None)
        dur = sum(float(r["duration_s"]) for r in group if r.get("duration_s") is not None)
        out.append(WeekSummary(
            week_start=s.isoformat(),
            distance_km=round(dist / M_PER_KM, 3),
            duration_s=dur,
            runs=len(group),
            pace_s_per_km=pace_s_per_km(dist, dur) if group and dist > 0 else None,
            avg_hr=weighted_avg_hr(group),
        ))
    return out


def week_over_week(summaries: Sequence[WeekSummary]) -> float | None:
    """Fractional change in distance, latest vs previous week.

    None when there is no prior week or the prior week had 0 km -- a division by
    zero has no meaningful percentage.
    """
    if len(summaries) < 2:
        return None
    prev, latest = summaries[-2], summaries[-1]
    if prev.distance_km <= 0:
        return None
    return (latest.distance_km - prev.distance_km) / prev.distance_km


@dataclass(frozen=True)
class WindowSummary:
    days: int
    distance_km: float
    duration_s: float
    runs: int
    pace_s_per_km: float | None
    avg_hr: float | None
    sufficient: bool  # False when history is shorter than the window


def window_summary(rows: Sequence[dict[str, Any]], days: int,
                   today: date | None = None,
                   first_activity_date: Any | None = None) -> WindowSummary:
    """Totals over the trailing ``days``-day window (inclusive of today)."""
    today = today or date.today()
    start = today - timedelta(days=days - 1)

    sel = [r for r in rows
           if r.get("start_date_utc") is not None and start <= _d(r["start_date_utc"]) <= today]
    dist = sum(float(r["distance_m"]) for r in sel if r.get("distance_m") is not None)
    dur = sum(float(r["duration_s"]) for r in sel if r.get("duration_s") is not None)

    # "Sufficient" is about history length, not how much was run in it.
    if first_activity_date is None:
        all_dates = [_d(r["start_date_utc"]) for r in rows if r.get("start_date_utc") is not None]
        first = min(all_dates) if all_dates else None
    else:
        first = _d(first_activity_date)
    sufficient = first is not None and first <= start

    return WindowSummary(
        days=days,
        distance_km=round(dist / M_PER_KM, 3),
        duration_s=dur,
        runs=len(sel),
        pace_s_per_km=pace_s_per_km(dist, dur) if dist > 0 else None,
        avg_hr=weighted_avg_hr(sel),
        sufficient=sufficient,
    )
