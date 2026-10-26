"""The rule-based training plan."""

from __future__ import annotations

import unittest
from datetime import date

from corach.coach.plan import PlanInputs, build_plan

START = date(2026, 10, 26)  # a Monday, so the plan covers Tue..Mon


def inputs(**kw) -> PlanInputs:
    base = dict(last_weeks_km=[30.0, 30.0, 30.0, 30.0], acwr_zone="optimal",
                longest_recent_km=15.0, typical_pace_s_per_km=360.0,
                sessions_per_week=5, strength_per_week=1, start=START)
    base.update(kw)
    return PlanInputs(**base)


class PlanTest(unittest.TestCase):
    def test_no_recent_running_means_no_plan(self) -> None:
        plan = build_plan(inputs(last_weeks_km=[0.0, 0.0, 0.0, 0.0]))
        self.assertFalse(plan["found"])  # never a made-up baseline

    def test_covers_seven_days_starting_tomorrow(self) -> None:
        plan = build_plan(inputs())
        self.assertEqual(len(plan["days"]), 7)
        self.assertEqual(plan["days"][0]["date"], "2026-10-27")

    def test_default_week_has_four_runs_one_strength_two_rest(self) -> None:
        plan = build_plan(inputs())
        kinds = [d["kind"] for d in plan["days"]]
        self.assertEqual(plan["run_sessions"], 4)
        self.assertEqual(plan["strength_sessions"], 1)
        self.assertEqual(kinds.count("rest"), 2)
        self.assertEqual(kinds.count("long"), 1)
        self.assertEqual(kinds.count("quality"), 1)

    def test_normal_progression_is_ten_percent(self) -> None:
        plan = build_plan(inputs(longest_recent_km=None))
        self.assertEqual(plan["basis_weekly_km"], 30.0)
        self.assertAlmostEqual(plan["planned_weekly_km"], 33.0, delta=1.5)  # rounding to 0.5

    def test_high_risk_load_cuts_volume(self) -> None:
        normal = build_plan(inputs(longest_recent_km=None))["planned_weekly_km"]
        risky = build_plan(inputs(acwr_zone="high_risk", longest_recent_km=None))
        self.assertLess(risky["planned_weekly_km"], normal * 0.75)

    def test_caution_holds_volume_flat(self) -> None:
        plan = build_plan(inputs(acwr_zone="caution", longest_recent_km=None))
        self.assertAlmostEqual(plan["planned_weekly_km"], 30.0, delta=1.5)

    def test_long_run_is_capped_by_longest_recent_run(self) -> None:
        plan = build_plan(inputs(last_weeks_km=[60.0] * 4, longest_recent_km=10.0))
        long_run = next(d for d in plan["days"] if d["kind"] == "long")
        self.assertLessEqual(long_run["distance_km"], 11.5)  # 110% of 10 km, rounded to 0.5

    def test_race_week_and_taper_reduce_load(self) -> None:
        build = build_plan(inputs(weeks_to_race=5, longest_recent_km=None))
        taper = build_plan(inputs(weeks_to_race=2, longest_recent_km=None))
        race = build_plan(inputs(weeks_to_race=1, longest_recent_km=None))
        self.assertEqual((build["phase"], taper["phase"], race["phase"]),
                         ("build", "taper", "race_week"))
        self.assertGreater(build["planned_weekly_km"], taper["planned_weekly_km"])
        self.assertGreater(taper["planned_weekly_km"], race["planned_weekly_km"])

    def test_duration_is_none_without_a_known_pace(self) -> None:
        plan = build_plan(inputs(typical_pace_s_per_km=None))
        runs = [d for d in plan["days"] if d["distance_km"] is not None]
        self.assertTrue(all(d["duration_min"] is None for d in runs))

    def test_session_count_is_respected(self) -> None:
        for n in (3, 4, 5, 6):
            plan = build_plan(inputs(sessions_per_week=n))
            trained = [d for d in plan["days"] if d["kind"] != "rest"]
            self.assertEqual(len(trained), n, n)

    def test_no_strength_gives_all_runs(self) -> None:
        plan = build_plan(inputs(strength_per_week=0))
        self.assertEqual(plan["strength_sessions"], 0)
        self.assertEqual(plan["run_sessions"], 5)

    def test_invalid_session_count_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_plan(inputs(sessions_per_week=9))


if __name__ == "__main__":
    unittest.main()
