"""SYNTHETIC activity generator. Deterministic, and NOT real athlete data.

Includes deliberately bad rows so the quarantine path is exercised by default.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..transform import SCHEMA_V1

SYNTHETIC_MARKER = "SYNTHETIC"


class SyntheticSource:
    """Generates reproducible fake runs. ``name`` is recorded in every row."""

    name = "synthetic"

    def __init__(self, days: int = 120, seed: int = 42,
                 reference: datetime | None = None, include_bad_rows: bool = True) -> None:
        self.days = days
        self.seed = seed
        self.include_bad_rows = include_bad_rows
        self.reference = reference or datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

    def _good_rows(self) -> list[dict[str, Any]]:
        rng = random.Random(self.seed)
        rows: list[dict[str, Any]] = []
        for day_offset in range(self.days):
            day = self.reference - timedelta(days=day_offset)
            # ~4 runs a week, and a deliberate gap so zero-run weeks are testable.
            if rng.random() > 0.57 or 30 <= day_offset < 44:
                continue
            distance_km = round(rng.uniform(4.0, 18.0), 2)
            pace_s_per_km = rng.uniform(290, 365)
            duration_s = round(distance_km * pace_s_per_km, 1)
            # ~20% of runs have no HR: watch was not worn / strap dropped.
            avg_hr = None if rng.random() < 0.2 else round(rng.uniform(132, 171), 1)
            start = day.replace(hour=rng.randint(5, 19), minute=rng.randint(0, 59),
                                second=0, microsecond=0)
            rows.append({
                "schema": SCHEMA_V1,
                "activity_id": f"syn-{start.date().isoformat()}-{day_offset}",
                "start_time": start.isoformat(),
                "distance_m": round(distance_km * 1000, 1),
                "duration_s": duration_s,
                "avg_hr": avg_hr,
                "note": SYNTHETIC_MARKER,
            })
        return rows

    def _bad_rows(self) -> list[dict[str, Any]]:
        """Rows that must be quarantined, one per failure mode."""
        base = self.reference.replace(hour=7, minute=0, second=0, microsecond=0)
        return [
            {  # zero distance -> distance_non_positive
                "schema": SCHEMA_V1, "activity_id": "syn-bad-zero-distance",
                "start_time": base.isoformat(), "distance_m": 0,
                "duration_s": 1800, "avg_hr": 150, "note": SYNTHETIC_MARKER,
            },
            {  # absurd HR -> hr_out_of_range
                "schema": SCHEMA_V1, "activity_id": "syn-bad-hr",
                "start_time": base.isoformat(), "distance_m": 10000,
                "duration_s": 3000, "avg_hr": 900, "note": SYNTHETIC_MARKER,
            },
            {  # duration absent -> duration_missing (NOT treated as 0)
                "schema": SCHEMA_V1, "activity_id": "syn-bad-no-duration",
                "start_time": base.isoformat(), "distance_m": 8000,
                "duration_s": None, "avg_hr": 145, "note": SYNTHETIC_MARKER,
            },
            {  # unknown schema -> rejected by the parser
                "schema": "coros_unknown_v9", "activity_id": "syn-bad-schema",
                "start_time": base.isoformat(), "distance_m": 5000,
                "duration_s": 1500, "note": SYNTHETIC_MARKER,
            },
        ]

    def fetch(self, since: str | None = None) -> Iterable[dict[str, Any]]:
        rows = self._good_rows()
        if self.include_bad_rows:
            rows += self._bad_rows()
        if since:
            rows = [r for r in rows if str(r.get("start_time", ""))[:10] >= since[:10]]
        return sorted(rows, key=lambda r: str(r.get("start_time", "")))
