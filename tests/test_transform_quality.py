"""Parser and validator behaviour, including the NULL-vs-zero distinction."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from corach.quality import ValidationResult, freshness, validate
from corach.transform import (
    SCHEMA_V1,
    SchemaError,
    parse_activity,
    parse_start_time,
    payload_hash,
)


def payload(**over) -> dict:
    base = {
        "schema": SCHEMA_V1, "activity_id": "a1",
        "start_time": "2026-10-01T06:30:00+00:00",
        "distance_m": 10000.0, "duration_s": 3000.0, "avg_hr": 150.0,
    }
    base.update(over)
    return base


class ParseTest(unittest.TestCase):
    def test_parses_a_good_payload(self) -> None:
        a = parse_activity(payload(), "synthetic")
        self.assertEqual(a.activity_id, "a1")
        self.assertEqual(a.start_date_utc, "2026-10-01")
        self.assertAlmostEqual(a.distance_m, 10000.0)

    def test_unknown_schema_is_rejected(self) -> None:
        with self.assertRaises(SchemaError):
            parse_activity(payload(schema="coros_v2"), "synthetic")

    def test_missing_schema_is_rejected(self) -> None:
        bad = payload()
        del bad["schema"]
        with self.assertRaises(SchemaError):
            parse_activity(bad, "synthetic")

    def test_non_dict_payload_is_rejected(self) -> None:
        with self.assertRaises(SchemaError):
            parse_activity(["not", "a", "dict"], "synthetic")  # type: ignore[arg-type]

    def test_missing_activity_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_activity(payload(activity_id=""), "synthetic")

    def test_absent_hr_becomes_none_not_zero(self) -> None:
        self.assertIsNone(parse_activity(payload(avg_hr=None), "synthetic").avg_hr)
        self.assertIsNone(parse_activity(payload(avg_hr=""), "synthetic").avg_hr)
        self.assertIsNone(parse_activity(payload(avg_hr="n/a"), "synthetic").avg_hr)

    def test_real_zero_is_preserved_as_zero(self) -> None:
        # 0 is a reported value; the validator, not the parser, judges it.
        self.assertEqual(parse_activity(payload(distance_m=0), "synthetic").distance_m, 0.0)

    def test_booleans_are_not_measurements(self) -> None:
        self.assertIsNone(parse_activity(payload(avg_hr=True), "synthetic").avg_hr)

    def test_numeric_strings_are_accepted(self) -> None:
        self.assertAlmostEqual(parse_activity(payload(distance_m="5000"), "synthetic").distance_m,
                               5000.0)

    def test_start_time_accepts_z_suffix_and_epoch(self) -> None:
        _, day = parse_start_time("2026-10-01T06:30:00Z")
        self.assertEqual(day, "2026-10-01")
        iso_epoch, _ = parse_start_time(1767248400)
        self.assertTrue(iso_epoch.endswith("+00:00"))

    def test_naive_start_time_is_treated_as_utc(self) -> None:
        iso, _ = parse_start_time("2026-10-01T06:30:00")
        self.assertIn("+00:00", iso)

    def test_non_utc_offset_is_converted(self) -> None:
        # 2026-10-01T01:00+07:00 is 2026-09-30T18:00 UTC, i.e. the previous day.
        _, day = parse_start_time("2026-10-01T01:00:00+07:00")
        self.assertEqual(day, "2026-09-30")

    def test_bad_start_time_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_start_time("not-a-date")

    def test_payload_hash_is_order_independent(self) -> None:
        self.assertEqual(payload_hash({"a": 1, "b": 2}), payload_hash({"b": 2, "a": 1}))

    def test_payload_hash_changes_with_content(self) -> None:
        self.assertNotEqual(payload_hash({"a": 1}), payload_hash({"a": 2}))


class ValidateTest(unittest.TestCase):
    def _v(self, **over) -> ValidationResult:
        return validate(parse_activity(payload(**over), "synthetic"))

    def test_good_row_passes(self) -> None:
        self.assertTrue(self._v().ok)

    def test_missing_hr_is_valid(self) -> None:
        result = self._v(avg_hr=None)
        self.assertTrue(result.ok)  # absence is not an error

    def test_implausible_hr_is_rejected(self) -> None:
        self.assertEqual(self._v(avg_hr=900).reason, "hr_out_of_range")
        self.assertEqual(self._v(avg_hr=5).reason, "hr_out_of_range")

    def test_non_positive_distance_is_rejected(self) -> None:
        self.assertEqual(self._v(distance_m=0).reason, "distance_non_positive")
        self.assertEqual(self._v(distance_m=-5).reason, "distance_non_positive")

    def test_missing_distance_is_rejected_not_zeroed(self) -> None:
        self.assertEqual(self._v(distance_m=None).reason, "distance_missing")

    def test_missing_duration_is_rejected(self) -> None:
        self.assertEqual(self._v(duration_s=None).reason, "duration_missing")

    def test_out_of_range_values_are_rejected(self) -> None:
        self.assertEqual(self._v(distance_m=600000).reason, "distance_out_of_range")
        self.assertEqual(self._v(duration_s=200000).reason, "duration_out_of_range")


class FreshnessTest(unittest.TestCase):
    def test_recent_run_is_fresh(self) -> None:
        now = datetime(2026, 10, 17, 12, tzinfo=UTC)
        f = freshness((now - timedelta(hours=2)).isoformat(), 36.0, now=now)
        self.assertTrue(f.is_fresh)
        self.assertAlmostEqual(f.age_hours, 2.0)

    def test_stale_run_is_not_fresh(self) -> None:
        now = datetime(2026, 10, 17, 12, tzinfo=UTC)
        f = freshness((now - timedelta(hours=50)).isoformat(), 36.0, now=now)
        self.assertFalse(f.is_fresh)

    def test_never_run_is_not_fresh(self) -> None:
        f = freshness(None)
        self.assertFalse(f.is_fresh)
        self.assertIsNone(f.age_hours)  # unknown age, not zero

    def test_unparseable_timestamp_is_not_fresh(self) -> None:
        f = freshness("garbage")
        self.assertFalse(f.is_fresh)
        self.assertIsNone(f.age_hours)


if __name__ == "__main__":
    unittest.main()
