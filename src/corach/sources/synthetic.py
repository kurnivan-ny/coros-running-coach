"""SYNTHETIC activity generator. Deterministic, and NOT real athlete data.

Emits the same ``coros_record_v1`` payloads as the real snapshot source, across
several sports, so dbt silver and gold run on it unchanged. Includes deliberately
bad rows so the quarantine path is exercised by default.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .coros_snapshot import SCHEMA

SYNTHETIC_MARKER = "SYNTHETIC"


class SyntheticSource:
    """Generates reproducible fake activities. ``name`` is recorded in every row."""

    name = "synthetic"

    def __init__(self, days: int = 120, seed: int = 42,
                 reference: datetime | None = None, include_bad_rows: bool = True) -> None:
        self.days = days
        self.seed = seed
        self.include_bad_rows = include_bad_rows
        self.reference = reference or datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

    @staticmethod
    def _record(activity_id: str, start: datetime, sport_type: int, sport_name: str,
                duration_s: float | None, distance_m: float | None, avg_hr: int | None,
                sets: int | None = None, schema: str = SCHEMA) -> dict[str, Any]:
        start_ts = int(start.timestamp())
        return {
            "schema": schema,
            "activity_id": activity_id,
            "sport_type": sport_type,
            "sport_name": sport_name,
            "activity_date": start.date().isoformat(),
            "start_ts": start_ts,
            "end_ts": start_ts + int(duration_s or 0),
            "location": SYNTHETIC_MARKER,
            "start_lat": None,
            "start_lon": None,
            "duration_s": duration_s,
            "distance_m": distance_m,
            "sets": sets,
            "reported_pace_s_per_km": None,
            "avg_hr": avg_hr,
            "calories": None,
        }

    def _good_rows(self) -> list[dict[str, Any]]:
        rng = random.Random(self.seed)
        rows: list[dict[str, Any]] = []
        for day_offset in range(self.days):
            day = self.reference - timedelta(days=day_offset)
            # A deliberate gap so zero-activity weeks are testable.
            if 30 <= day_offset < 44:
                continue
            start = day.replace(hour=rng.randint(5, 19), minute=rng.randint(0, 59),
                                second=0, microsecond=0)
            roll = rng.random()
            aid = f"syn-{start.date().isoformat()}-{day_offset}"
            if roll < 0.50:
                distance_km = round(rng.uniform(4.0, 18.0), 2)
                duration = round(distance_km * rng.uniform(290, 365))
                # ~20% have no HR: watch was not worn / strap dropped.
                hr = None if rng.random() < 0.2 else rng.randint(132, 171)
                rows.append(self._record(aid, start, 100, "Outdoor Run", duration,
                                         distance_km * 1000, hr))
            elif roll < 0.68:
                rows.append(self._record(aid, start, 402, "Strength",
                                         rng.randint(1500, 3600), None, rng.randint(95, 125),
                                         sets=rng.randint(8, 24)))
            elif roll < 0.76:
                distance_km = round(rng.uniform(2.0, 6.0), 2)
                rows.append(self._record(aid, start, 900, "Walk",
                                         round(distance_km * rng.uniform(600, 780)),
                                         distance_km * 1000, rng.randint(95, 118)))
        return rows

    def _bad_rows(self) -> list[dict[str, Any]]:
        """Rows that must be quarantined, one per failure mode."""
        base = self.reference.replace(hour=7, minute=0, second=0, microsecond=0)
        return [
            self._record("syn-bad-zero-distance", base, 100, "Outdoor Run", 1800, 0, 150),
            self._record("syn-bad-hr", base, 100, "Outdoor Run", 3000, 10000, 900),
            self._record("syn-bad-no-duration", base, 100, "Outdoor Run", None, 8000, 145),
            self._record("syn-bad-schema", base, 100, "Outdoor Run", 1500, 5000, None,
                         schema="coros_unknown_v9"),
        ]

    def fetch(self, since: str | None = None) -> Iterable[dict[str, Any]]:
        rows = self._good_rows()
        if self.include_bad_rows:
            rows += self._bad_rows()
        if since:
            rows = [r for r in rows if str(r.get("activity_date", "")) >= since[:10]]
        return sorted(rows, key=lambda r: (str(r["activity_date"]), r["activity_id"]))
