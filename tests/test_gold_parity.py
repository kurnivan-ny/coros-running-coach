"""Proves the dbt gold SQL agrees with metrics.py.

The real risk in adding a gold layer is two definitions of the same number
drifting apart: the dashboard says 142 bpm, the chat says 145 bpm. metrics.py is
authoritative, so these tests run the SAME aggregation logic as the dbt macros
(SQL, executed by SQLite) and assert it matches the Python.

This is not a full dbt run -- no Postgres is available here -- but it does
execute the metric SQL, which is the part that can silently disagree. The
Postgres-specific constructs (generate_series, interval arithmetic) are verified
by `dbt parse`/`compile`, not by this test.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date

from corach.metrics import pace_s_per_km, weighted_avg_hr, window_summary

# SQLite equivalents of dbt/macros/metrics.sql. The NULL semantics -- which are
# the thing that drifts -- are identical: SUM ignores NULL rows, so runs without
# HR are excluded from both numerator and denominator rather than zero-filled.
SQL_WEIGHTED_HR = """
SELECT CASE
    WHEN SUM(CASE WHEN avg_hr IS NOT NULL THEN duration_s END) > 0
        THEN SUM(CASE WHEN avg_hr IS NOT NULL THEN avg_hr * duration_s END)
             / SUM(CASE WHEN avg_hr IS NOT NULL THEN duration_s END)
    ELSE NULL
END AS avg_hr
FROM activities
"""

SQL_PACE = """
SELECT CASE
    WHEN COALESCE(SUM(distance_m), 0) > 0
        THEN SUM(duration_s) / (SUM(distance_m) / 1000.0)
    ELSE NULL
END AS pace
FROM activities
"""

SQL_WINDOW = """
SELECT
    COUNT(distance_m) AS runs,
    COALESCE(SUM(distance_m), 0) / 1000.0 AS distance_km,
    CASE
        WHEN COALESCE(SUM(distance_m), 0) > 0
            THEN SUM(duration_s) / (SUM(distance_m) / 1000.0)
        ELSE NULL
    END AS pace,
    CASE
        WHEN SUM(CASE WHEN avg_hr IS NOT NULL THEN duration_s END) > 0
            THEN SUM(CASE WHEN avg_hr IS NOT NULL THEN avg_hr * duration_s END)
                 / SUM(CASE WHEN avg_hr IS NOT NULL THEN duration_s END)
        ELSE NULL
    END AS avg_hr
FROM activities
WHERE start_date_utc BETWEEN ? AND ?
"""


def row(day: str, dist: float, dur: float, hr: float | None = None) -> dict:
    return {"start_date_utc": day, "distance_m": dist, "duration_s": dur, "avg_hr": hr}


class GoldParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE activities "
            "(start_date_utc TEXT, distance_m REAL, duration_s REAL, avg_hr REAL)"
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _load(self, rows: list[dict]) -> None:
        self.conn.executemany(
            "INSERT INTO activities VALUES (?,?,?,?)",
            [(r["start_date_utc"], r["distance_m"], r["duration_s"], r["avg_hr"])
             for r in rows],
        )

    def _scalar(self, sql: str, params: tuple = ()) -> float | None:
        return self.conn.execute(sql, params).fetchone()[0]

    # -- heart rate --------------------------------------------------------
    def test_weighted_hr_matches_python(self) -> None:
        rows = [
            row("2026-10-05", 10000.0, 3600.0, 150.0),
            row("2026-10-06", 5000.0, 1800.0, 120.0),
        ]
        self._load(rows)
        self.assertAlmostEqual(self._scalar(SQL_WEIGHTED_HR), weighted_avg_hr(rows), places=9)

    def test_sql_excludes_null_hr_exactly_like_python(self) -> None:
        # The classic drift bug: SQL that zero-fills gives 100.0, not 150.0.
        rows = [
            row("2026-10-05", 10000.0, 3600.0, 150.0),
            row("2026-10-06", 5000.0, 1800.0, None),
        ]
        self._load(rows)
        sql_value = self._scalar(SQL_WEIGHTED_HR)
        self.assertAlmostEqual(sql_value, weighted_avg_hr(rows), places=9)
        self.assertAlmostEqual(sql_value, 150.0)
        self.assertNotAlmostEqual(sql_value, 100.0)  # the zero-filled answer

    def test_all_null_hr_is_null_in_both(self) -> None:
        rows = [row("2026-10-05", 10000.0, 3600.0, None)]
        self._load(rows)
        self.assertIsNone(self._scalar(SQL_WEIGHTED_HR))
        self.assertIsNone(weighted_avg_hr(rows))

    def test_plain_avg_would_disagree(self) -> None:
        # Documents WHY the macro is written the way it is.
        rows = [
            row("2026-10-05", 10000.0, 3600.0, 150.0),
            row("2026-10-06", 1000.0, 300.0, 100.0),
        ]
        self._load(rows)
        naive = self._scalar("SELECT AVG(avg_hr) FROM activities")
        correct = self._scalar(SQL_WEIGHTED_HR)
        self.assertAlmostEqual(correct, weighted_avg_hr(rows), places=9)
        self.assertNotAlmostEqual(naive, correct, places=3)

    # -- pace --------------------------------------------------------------
    def test_pace_matches_python(self) -> None:
        rows = [
            row("2026-10-05", 1000.0, 360.0),
            row("2026-10-06", 9000.0, 2700.0),
        ]
        self._load(rows)
        total_d = sum(r["distance_m"] for r in rows)
        total_t = sum(r["duration_s"] for r in rows)
        self.assertAlmostEqual(self._scalar(SQL_PACE), pace_s_per_km(total_d, total_t), places=9)

    def test_pace_is_null_when_no_rows(self) -> None:
        self.assertIsNone(self._scalar(SQL_PACE))
        self.assertIsNone(pace_s_per_km(0.0, 0.0))

    # -- windows -----------------------------------------------------------
    def test_window_totals_match_python(self) -> None:
        rows = [
            row("2026-10-01", 8000.0, 2400.0, 140.0),
            row("2026-10-14", 10000.0, 3000.0, 150.0),
            row("2026-10-16", 12000.0, 3900.0, None),
        ]
        self._load(rows)
        today = date(2026, 10, 17)
        start = date(2026, 10, 11)

        sql = self.conn.execute(
            SQL_WINDOW, (start.isoformat(), today.isoformat())
        ).fetchone()
        py = window_summary(rows, days=7, today=today)

        self.assertEqual(sql[0], py.runs)
        self.assertAlmostEqual(sql[1], py.distance_km, places=6)
        self.assertAlmostEqual(sql[2], py.pace_s_per_km, places=6)
        self.assertAlmostEqual(sql[3], py.avg_hr, places=6)

    def test_empty_window_is_null_pace_in_both(self) -> None:
        self._load([row("2026-01-01", 10000.0, 3000.0, 150.0)])
        today = date(2026, 10, 17)
        sql = self.conn.execute(SQL_WINDOW, ("2026-10-11", today.isoformat())).fetchone()
        py = window_summary(
            [row("2026-01-01", 10000.0, 3000.0, 150.0)], days=7, today=today
        )

        self.assertEqual(sql[0], 0)
        self.assertEqual(py.runs, 0)
        self.assertIsNone(sql[2])      # pace NULL, not 0
        self.assertIsNone(py.pace_s_per_km)
        self.assertIsNone(sql[3])      # HR NULL, not 0
        self.assertIsNone(py.avg_hr)


if __name__ == "__main__":
    unittest.main()
