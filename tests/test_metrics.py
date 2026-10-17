"""Metric edge cases: NULL handling, zero-run weeks, insufficient history."""

from __future__ import annotations

import unittest
from datetime import date

from corach.metrics import (
    format_pace,
    iso_week_start,
    pace_s_per_km,
    week_over_week,
    weekly_volume,
    weighted_avg_hr,
    window_summary,
)


def row(day: str, dist: float, dur: float, hr: float | None = None) -> dict:
    return {"start_date_utc": day, "distance_m": dist, "duration_s": dur, "avg_hr": hr}


class PaceTest(unittest.TestCase):
    def test_pace_is_total_duration_over_total_distance(self) -> None:
        self.assertAlmostEqual(pace_s_per_km(10000.0, 3000.0), 300.0)

    def test_pace_is_none_when_inputs_missing_or_zero(self) -> None:
        self.assertIsNone(pace_s_per_km(None, 3000.0))
        self.assertIsNone(pace_s_per_km(10000.0, None))
        self.assertIsNone(pace_s_per_km(0.0, 3000.0))

    def test_format_pace_preserves_none(self) -> None:
        self.assertEqual(format_pace(300.0), "5:00")
        self.assertEqual(format_pace(329.0), "5:29")
        self.assertIsNone(format_pace(None))  # never render a fake 0:00

    def test_aggregate_pace_is_not_the_mean_of_paces(self) -> None:
        # 1 km at 600 s/km and 9 km at 300 s/km.
        # Mean of paces = 450; correct total-based pace = 330.
        rows = [row("2026-10-05", 1000.0, 600.0), row("2026-10-06", 9000.0, 2700.0)]
        total_d = sum(r["distance_m"] for r in rows)
        total_t = sum(r["duration_s"] for r in rows)
        self.assertAlmostEqual(pace_s_per_km(total_d, total_t), 330.0)


class HeartRateTest(unittest.TestCase):
    def test_hr_is_duration_weighted(self) -> None:
        rows = [row("2026-10-05", 10000.0, 3600.0, 150.0),
                row("2026-10-06", 5000.0, 1800.0, 120.0)]
        # (150*3600 + 120*1800) / 5400 = 140
        self.assertAlmostEqual(weighted_avg_hr(rows), 140.0)

    def test_runs_without_hr_are_excluded_not_zeroed(self) -> None:
        rows = [row("2026-10-05", 10000.0, 3600.0, 150.0),
                row("2026-10-06", 5000.0, 1800.0, None)]
        # Zero-filling would give 100.0; excluding gives 150.0.
        self.assertAlmostEqual(weighted_avg_hr(rows), 150.0)

    def test_hr_is_none_when_no_run_reports_it(self) -> None:
        self.assertIsNone(weighted_avg_hr([row("2026-10-05", 10000.0, 3600.0, None)]))
        self.assertIsNone(weighted_avg_hr([]))


class WeeklyTest(unittest.TestCase):
    def test_iso_week_starts_on_monday(self) -> None:
        # 2026-10-17 is a Saturday; its ISO week began Monday 2026-10-12.
        self.assertEqual(iso_week_start("2026-10-17"), date(2026, 10, 12))
        self.assertEqual(iso_week_start("2026-10-12"), date(2026, 10, 12))

    def test_zero_run_week_reports_zero_km_and_none_pace(self) -> None:
        rows = [row("2026-10-12", 10000.0, 3000.0, 150.0)]
        weeks = weekly_volume(rows, weeks=2, today=date(2026, 10, 17))
        prior, current = weeks[0], weeks[1]

        self.assertEqual(prior.runs, 0)
        self.assertEqual(prior.distance_km, 0.0)
        self.assertIsNone(prior.pace_s_per_km)
        self.assertIsNone(prior.avg_hr)
        self.assertEqual(current.distance_km, 10.0)

    def test_weeks_are_ordered_oldest_first(self) -> None:
        weeks = weekly_volume([], weeks=3, today=date(2026, 10, 17))
        self.assertEqual([w.week_start for w in weeks],
                         ["2026-09-28", "2026-10-05", "2026-10-12"])

    def test_week_over_week_is_none_when_prior_week_was_empty(self) -> None:
        rows = [row("2026-10-12", 10000.0, 3000.0)]
        weeks = weekly_volume(rows, weeks=2, today=date(2026, 10, 17))
        self.assertIsNone(week_over_week(weeks))  # no division by zero

    def test_week_over_week_computes_fractional_change(self) -> None:
        rows = [row("2026-10-05", 10000.0, 3000.0), row("2026-10-12", 15000.0, 4500.0)]
        weeks = weekly_volume(rows, weeks=2, today=date(2026, 10, 17))
        self.assertAlmostEqual(week_over_week(weeks), 0.5)

    def test_week_over_week_needs_two_weeks(self) -> None:
        weeks = weekly_volume([], weeks=1, today=date(2026, 10, 17))
        self.assertIsNone(week_over_week(weeks))


class WindowTest(unittest.TestCase):
    def test_window_flags_insufficient_history(self) -> None:
        rows = [row("2026-10-15", 10000.0, 3000.0)]
        s = window_summary(rows, days=90, today=date(2026, 10, 17))
        self.assertFalse(s.sufficient)  # only 3 days of history for a 90-day window

    def test_window_is_sufficient_when_history_covers_it(self) -> None:
        rows = [row("2026-07-01", 10000.0, 3000.0), row("2026-10-15", 10000.0, 3000.0)]
        s = window_summary(rows, days=28, today=date(2026, 10, 17))
        self.assertTrue(s.sufficient)

    def test_window_excludes_activities_outside_it(self) -> None:
        rows = [row("2026-01-01", 99000.0, 30000.0), row("2026-10-15", 10000.0, 3000.0)]
        s = window_summary(rows, days=7, today=date(2026, 10, 17))
        self.assertEqual(s.runs, 1)
        self.assertEqual(s.distance_km, 10.0)

    def test_window_includes_both_boundaries(self) -> None:
        rows = [row("2026-10-11", 1000.0, 300.0), row("2026-10-17", 1000.0, 300.0)]
        s = window_summary(rows, days=7, today=date(2026, 10, 17))
        self.assertEqual(s.runs, 2)

    def test_empty_window_has_none_pace_not_zero(self) -> None:
        s = window_summary([], days=7, today=date(2026, 10, 17))
        self.assertEqual(s.distance_km, 0.0)
        self.assertIsNone(s.pace_s_per_km)
        self.assertIsNone(s.avg_hr)
        self.assertFalse(s.sufficient)


if __name__ == "__main__":
    unittest.main()
