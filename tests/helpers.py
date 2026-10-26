"""Test fixtures: a SQLite store holding hand-written gold-layer rows.

dbt builds the real gold tables on Postgres. These helpers create tables with the
same column names, so the coach tools (which read ``gold.*``) run unchanged on
SQLite. The tests therefore check the tools against values written by hand, not
against a second implementation of the dbt logic.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from corach.store import Store

_TABLES = """
CREATE TABLE fct_activities (
    activity_id TEXT PRIMARY KEY, source TEXT, sport_type INTEGER, sport_name TEXT,
    sport_group TEXT, is_indoor INTEGER, local_date TEXT, week_start TEXT,
    start_time_utc TEXT, location TEXT, duration_s REAL, duration_min REAL,
    distance_m REAL, distance_km REAL, sets INTEGER, avg_hr REAL, calories REAL,
    avg_pace_s_per_km REAL, quality_flag TEXT, hr_reserve_fraction REAL, trimp REAL
);
CREATE TABLE weekly_summary (
    week_start TEXT, sport_group TEXT, sessions INTEGER, duration_min REAL,
    distance_km REAL, trimp REAL, sets INTEGER, avg_hr REAL, pace_s_per_km REAL
);
CREATE TABLE daily_load (
    day TEXT PRIMARY KEY, sessions INTEGER, duration_min REAL, trimp REAL,
    sessions_without_hr INTEGER, acute_load REAL, chronic_load REAL, acwr REAL,
    monotony_7d REAL, strain_7d REAL, acwr_zone TEXT
);
CREATE TABLE silver_rejects (
    activity_id TEXT, source TEXT, reason TEXT, sport_name TEXT, local_date TEXT,
    duration_s REAL, distance_m REAL, avg_hr REAL
);
"""


def gold_store() -> Store:
    """An empty in-memory store with bronze/ops tables plus empty gold tables."""
    store = Store(":memory:")
    store.init_schema()
    conn = store.connect()
    conn.executescript(_TABLES)
    conn.commit()
    return store


def add_activity(store: Store, activity_id: str, day: str, *, sport: str = "run",
                 name: str | None = None, distance_km: float | None = 10.0,
                 duration_min: float = 50.0, avg_hr: float | None = 150.0,
                 sets: int | None = None, trimp: float | None = 60.0,
                 source: str = "coros_snapshot") -> None:
    d = date.fromisoformat(day)
    week = d - timedelta(days=d.weekday())
    pace = (duration_min * 60 / distance_km) if distance_km and sport in ("run", "walk") else None
    store.execute(
        "INSERT INTO fct_activities (activity_id, source, sport_group, sport_name, local_date, "
        "week_start, start_time_utc, duration_s, duration_min, distance_m, distance_km, sets, "
        "avg_hr, avg_pace_s_per_km, trimp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (activity_id, source, sport, name or sport.title(), day, week.isoformat(),
         f"{day}T06:00:00+00:00", duration_min * 60, duration_min,
         None if distance_km is None else distance_km * 1000, distance_km, sets,
         avg_hr, pace, trimp),
    )
    store.commit()


def add_weekly(store: Store, week_start: str, *, sport: str = "run", sessions: int = 3,
               distance_km: float = 30.0, duration_min: float = 180.0, trimp: float = 200.0,
               avg_hr: float | None = 150.0, pace: float | None = 360.0) -> None:
    store.execute(
        "INSERT INTO weekly_summary (week_start, sport_group, sessions, duration_min, "
        "distance_km, trimp, avg_hr, pace_s_per_km) VALUES (?,?,?,?,?,?,?,?)",
        (week_start, sport, sessions, duration_min, distance_km, trimp, avg_hr, pace),
    )
    store.commit()


def add_load(store: Store, day: str, *, acute: float = 80.0, chronic: float | None = 90.0,
             acwr: float | None = 0.89, zone: str = "optimal",
             monotony: float | None = 1.2, strain: float | None = 500.0) -> None:
    store.execute(
        "INSERT INTO daily_load (day, sessions, duration_min, trimp, sessions_without_hr, "
        "acute_load, chronic_load, acwr, monotony_7d, strain_7d, acwr_zone) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (day, 1, 50.0, 60.0, 0, acute, chronic, acwr, monotony, strain, zone),
    )
    store.commit()


def record(activity_id: str, day: str, **overrides: Any) -> dict[str, Any]:
    """A bronze ``coros_record_v1`` payload."""
    base = {
        "schema": "coros_record_v1", "activity_id": activity_id, "sport_type": 100,
        "sport_name": "Outdoor Run", "activity_date": day, "start_ts": 1_790_000_000,
        "end_ts": 1_790_003_000, "location": "Test Run", "start_lat": None,
        "start_lon": None, "duration_s": 3000, "distance_m": 10000.0, "sets": None,
        "reported_pace_s_per_km": 300, "avg_hr": 150, "calories": 500,
    }
    base.update(overrides)
    return base
