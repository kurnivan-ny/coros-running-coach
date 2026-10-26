"""The COROS snapshot parser, checked against the real output format.

The sample below has the exact shape of a real ``querySportRecords`` response
(including the odd line break after ``Sets``), with made-up values.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from corach.sources import PermanentSourceError
from corach.sources.coros_snapshot import CorosSnapshotSource, parse_records

SAMPLE = """Sport Records — 2026-10-01 to 2026-10-26 (5 records)
========================

1. Outdoor Run — 2026-10-17
   Location: Sample Park Run
   Start Coordinates: -6.100000, 106.800000
   Time Window: startTimestamp=1792195056 | endTimestamp=1792197887
   Duration: 47:10 | Distance: 6.10 km
   Average Pace: 7:44 /km | Avg HR: 162 bpm | Calories: 678 kcal
   LabelId: 1000000000000000001 | SportType: 100

2. Indoor Run — 2026-10-13
   Location: Indoor Run
   Time Window: startTimestamp=1791850818 | endTimestamp=1791852709
   Duration: 1:15:15 | Distance: 11.29 km
   Average Pace: 6:40 /km | Avg HR: 156 bpm | Calories: 942 kcal
   LabelId: 1000000000000000002 | SportType: 101

3. Strength — 2026-10-12
   Location: Mobility and Core Workout
   Time Window: startTimestamp=1791760000 | endTimestamp=1791760300
   Duration: 2:58 | Sets: 5
 | Avg HR: 100 bpm | Calories: 27 kcal
   LabelId: 1000000000000000003 | SportType: 402

4. Outdoor Run — 2026-10-11
   Location: Short Run
   Time Window: startTimestamp=1791700000 | endTimestamp=1791700500
   Duration: 5:01 | Distance: 351 m
   Average Pace: 14:19 /km | Avg HR: 141 bpm | Calories: 40 kcal
   LabelId: 1000000000000000004 | SportType: 100

5. Walk — 2026-10-10
   Location: Walk
   Time Window: startTimestamp=1791600000 | endTimestamp=1791600900
   Duration: 15:00 | Distance: 0.96 km
   Average Pace: 15:37 /km | Calories: 60 kcal
   LabelId: 1000000000000000005 | SportType: 900
"""


class ParseRecordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = {r["activity_id"]: r for r in parse_records(SAMPLE)}

    def test_all_records_are_found(self) -> None:
        self.assertEqual(len(self.rows), 5)

    def test_outdoor_run_fields(self) -> None:
        r = self.rows["1000000000000000001"]
        self.assertEqual(r["schema"], "coros_record_v1")
        self.assertEqual((r["sport_type"], r["sport_name"]), (100, "Outdoor Run"))
        self.assertEqual(r["activity_date"], "2026-10-17")
        self.assertEqual((r["start_ts"], r["end_ts"]), (1792195056, 1792197887))
        self.assertEqual(r["duration_s"], 47 * 60 + 10)
        self.assertAlmostEqual(r["distance_m"], 6100.0)
        self.assertEqual((r["avg_hr"], r["calories"]), (162, 678))
        self.assertEqual(r["reported_pace_s_per_km"], 7 * 60 + 44)
        self.assertEqual((r["start_lat"], r["start_lon"]), (-6.1, 106.8))
        self.assertEqual(r["location"], "Sample Park Run")

    def test_hour_duration_is_parsed(self) -> None:
        self.assertEqual(self.rows["1000000000000000002"]["duration_s"], 3600 + 15 * 60 + 15)

    def test_metres_are_not_treated_as_kilometres(self) -> None:
        self.assertAlmostEqual(self.rows["1000000000000000004"]["distance_m"], 351.0)

    def test_strength_has_sets_and_no_distance(self) -> None:
        r = self.rows["1000000000000000003"]
        self.assertEqual(r["sets"], 5)
        self.assertIsNone(r["distance_m"])  # NULL, never 0
        self.assertIsNone(r["reported_pace_s_per_km"])
        self.assertEqual(r["avg_hr"], 100)  # on the line after the line break

    def test_missing_heart_rate_stays_none(self) -> None:
        r = self.rows["1000000000000000005"]
        self.assertIsNone(r["avg_hr"])  # not 0
        self.assertEqual(r["calories"], 60)

    def test_coordinates_are_optional(self) -> None:
        r = self.rows["1000000000000000002"]
        self.assertIsNone(r["start_lat"])
        self.assertIsNone(r["start_lon"])

    def test_json_string_wrapped_response_is_decoded(self) -> None:
        wrapped = json.dumps(SAMPLE)
        self.assertEqual(len(parse_records(wrapped)), 5)

    def test_block_without_ids_yields_none_not_a_guess(self) -> None:
        rows = parse_records("1. Outdoor Run — 2026-10-01\n   Location: x\n")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["activity_id"])
        self.assertIsNone(rows[0]["duration_s"])

    def test_garbage_input_yields_no_records(self) -> None:
        self.assertEqual(parse_records("No sport records found."), [])


class SnapshotSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_files_fail_loudly(self) -> None:
        with self.assertRaises(PermanentSourceError) as ctx:
            list(CorosSnapshotSource(self.dir).fetch())
        self.assertIn("sport_records_", str(ctx.exception))

    def test_reads_all_files_sorted_by_date(self) -> None:
        (self.dir / "sport_records_2026.txt").write_text(SAMPLE, encoding="utf-8")
        rows = list(CorosSnapshotSource(self.dir).fetch())
        dates = [r["activity_date"] for r in rows]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(rows), 5)

    def test_since_filters_by_activity_date(self) -> None:
        (self.dir / "sport_records_2026.txt").write_text(SAMPLE, encoding="utf-8")
        rows = list(CorosSnapshotSource(self.dir).fetch(since="2026-10-13"))
        self.assertEqual({r["activity_date"] for r in rows}, {"2026-10-13", "2026-10-17"})

    def test_duplicate_ids_across_files_are_deduplicated(self) -> None:
        (self.dir / "sport_records_a.txt").write_text(SAMPLE, encoding="utf-8")
        (self.dir / "sport_records_b.txt").write_text(SAMPLE, encoding="utf-8")
        self.assertEqual(len(list(CorosSnapshotSource(self.dir).fetch())), 5)


if __name__ == "__main__":
    unittest.main()
