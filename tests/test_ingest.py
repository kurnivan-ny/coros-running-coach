"""Ingestion behaviour: idempotency, incrementality, retries, quarantine."""

from __future__ import annotations

import unittest
from typing import Any

from corach.ingest import (
    _fetch_with_retry,
    get_watermark,
    load_silver,
    rebuild_silver,
    run_ingest,
)
from corach.sources import PermanentSourceError, TransientSourceError
from corach.sources.coros_mcp import CorosMCPSource
from corach.sources.synthetic import SyntheticSource
from corach.store import Store
from corach.transform import SCHEMA_V1


def activity(aid: str, start: str, dist: float = 10000.0,
             dur: float = 3000.0, hr: float | None = 150.0) -> dict[str, Any]:
    return {
        "schema": SCHEMA_V1, "activity_id": aid, "start_time": start,
        "distance_m": dist, "duration_s": dur, "avg_hr": hr,
    }


class ListSource:
    """Source backed by a fixed list; records the `since` it was asked for."""

    name = "synthetic"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[str | None] = []

    def fetch(self, since: str | None = None):
        self.calls.append(since)
        if since:
            return [r for r in self.rows if str(r["start_time"])[:10] >= since[:10]]
        return list(self.rows)


class FlakySource:
    """Fails with the given exceptions, then succeeds."""

    name = "synthetic"

    def __init__(self, failures: int, exc: type[Exception] = TransientSourceError) -> None:
        self.failures = failures
        self.exc = exc
        self.attempts = 0

    def fetch(self, since: str | None = None):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.exc(f"boom {self.attempts}")
        return [activity("ok-1", "2026-10-01T06:00:00+00:00")]


class IngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.store.init_schema()

    def tearDown(self) -> None:
        self.store.close()

    def test_rerun_is_idempotent(self) -> None:
        src = SyntheticSource(days=40)
        first = run_ingest(self.store, src)
        second = run_ingest(self.store, src)

        self.assertEqual(first.status, "success")
        # Nothing changed upstream, so no bronze row should be rewritten.
        self.assertEqual(second.rows_bronze, 0)
        self.assertEqual(len(load_silver(self.store)), first.rows_silver)

    def test_changed_payload_updates_bronze_and_silver(self) -> None:
        row = activity("run-1", "2026-10-01T06:00:00+00:00", dist=10000.0)
        src = ListSource([row])
        run_ingest(self.store, src)

        # Same id, corrected distance upstream.
        src.rows = [activity("run-1", "2026-10-01T06:00:00+00:00", dist=12345.0)]
        second = run_ingest(self.store, src, full_refresh=True)

        self.assertEqual(second.rows_bronze, 1)
        silver = load_silver(self.store)
        self.assertEqual(len(silver), 1)
        self.assertAlmostEqual(silver[0]["distance_m"], 12345.0)

    def test_unchanged_payload_leaves_bronze_hash_untouched(self) -> None:
        src = ListSource([activity("run-1", "2026-10-01T06:00:00+00:00")])
        run_ingest(self.store, src)
        before = self.store.query("SELECT updated_at FROM bronze_activities")[0]["updated_at"]
        run_ingest(self.store, src, full_refresh=True)
        after = self.store.query("SELECT updated_at FROM bronze_activities")[0]["updated_at"]
        self.assertEqual(before, after)

    def test_incremental_fetch_uses_watermark_with_overlap(self) -> None:
        src = ListSource([
            activity("a", "2026-10-01T06:00:00+00:00"),
            activity("b", "2026-10-10T06:00:00+00:00"),
        ])
        run_ingest(self.store, src)
        self.assertIsNone(src.calls[0])  # first run has no watermark

        run_ingest(self.store, src, overlap_days=3)
        # Watermark is 2026-10-10; overlap of 3 days means asking from 10-07.
        self.assertEqual(src.calls[1], "2026-10-07")

    def test_watermark_tracks_latest_activity(self) -> None:
        src = ListSource([
            activity("a", "2026-10-01T06:00:00+00:00"),
            activity("b", "2026-10-09T06:00:00+00:00"),
        ])
        run_ingest(self.store, src)
        self.assertEqual(get_watermark(self.store, "synthetic"), "2026-10-09")

    def test_retry_succeeds_after_transient_errors(self) -> None:
        src = FlakySource(failures=2)
        delays: list[float] = []
        result = run_ingest(self.store, src, sleep=delays.append)

        self.assertEqual(result.status, "success")
        self.assertEqual(src.attempts, 3)
        self.assertEqual(delays, [1.0, 2.0])  # exponential backoff

    def test_permanent_error_is_not_retried(self) -> None:
        src = FlakySource(failures=5, exc=PermanentSourceError)
        with self.assertRaises(PermanentSourceError):
            run_ingest(self.store, src, sleep=lambda _: None)
        self.assertEqual(src.attempts, 1)

    def test_failed_run_is_logged_and_reraised(self) -> None:
        src = FlakySource(failures=99)
        with self.assertRaises(TransientSourceError):
            run_ingest(self.store, src, max_attempts=2, sleep=lambda _: None)

        runs = self.store.query("SELECT status, error FROM ops_pipeline_runs")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertIn("TransientSourceError", runs[0]["error"])

    def test_successful_run_is_logged(self) -> None:
        run_ingest(self.store, ListSource([activity("a", "2026-10-01T06:00:00+00:00")]))
        runs = self.store.query("SELECT status, rows_silver, finished_at FROM ops_pipeline_runs")
        self.assertEqual(runs[0]["status"], "success")
        self.assertEqual(runs[0]["rows_silver"], 1)
        self.assertIsNotNone(runs[0]["finished_at"])

    def test_bad_rows_are_quarantined_with_reasons(self) -> None:
        result = run_ingest(self.store, SyntheticSource(days=20))
        rejects = self.store.query("SELECT activity_id, reason FROM silver_rejects")
        reasons = {r["reason"] for r in rejects}

        self.assertEqual(result.rows_rejected, 4)
        self.assertIn("distance_non_positive", reasons)
        self.assertIn("hr_out_of_range", reasons)
        self.assertIn("duration_missing", reasons)
        self.assertTrue(any("SchemaError" in r for r in reasons))
        # Quarantined rows must never reach silver.
        ids = {r["activity_id"] for r in load_silver(self.store)}
        self.assertNotIn("syn-bad-zero-distance", ids)

    def test_null_hr_survives_the_round_trip(self) -> None:
        src = ListSource([
            activity("with-hr", "2026-10-01T06:00:00+00:00", hr=150.0),
            activity("no-hr", "2026-10-02T06:00:00+00:00", hr=None),
        ])
        run_ingest(self.store, src)
        rows = {r["activity_id"]: r["avg_hr"] for r in load_silver(self.store)}

        self.assertEqual(rows["with-hr"], 150.0)
        self.assertIsNone(rows["no-hr"])  # must be NULL, never 0

    def test_rebuild_silver_reprocesses_from_bronze(self) -> None:
        src = ListSource([activity("a", "2026-10-01T06:00:00+00:00")])
        run_ingest(self.store, src)
        self.store.execute("DELETE FROM silver_activities")
        self.store.commit()
        self.assertEqual(len(load_silver(self.store)), 0)

        result = rebuild_silver(self.store, "synthetic")
        self.assertEqual(result.status, "success")
        self.assertEqual(len(load_silver(self.store)), 1)

    def test_coros_mcp_stub_fails_loudly(self) -> None:
        with self.assertRaises(PermanentSourceError) as ctx:
            list(CorosMCPSource().fetch())
        self.assertIn("not implemented", str(ctx.exception).lower())

    def test_retry_helper_stops_at_max_attempts(self) -> None:
        src = FlakySource(failures=99)
        with self.assertRaises(TransientSourceError):
            _fetch_with_retry(src, None, max_attempts=4, sleep=lambda _: None)
        self.assertEqual(src.attempts, 4)


if __name__ == "__main__":
    unittest.main()
