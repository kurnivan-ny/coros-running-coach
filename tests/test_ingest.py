"""Bronze ingestion: idempotency, incrementality, retries, migration, DSN handling."""

from __future__ import annotations

import threading
import unittest
from typing import Any

from corach.ingest import _fetch_with_retry, get_watermark, payload_hash, run_ingest
from corach.sources import PermanentSourceError, TransientSourceError
from corach.sources.synthetic import SyntheticSource
from corach.store import Store, dsn_from_env
from helpers import record


class ListSource:
    """Source backed by a fixed list; records the `since` it was asked for."""

    name = "coros_snapshot"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[str | None] = []

    def fetch(self, since: str | None = None):
        self.calls.append(since)
        if since:
            return [r for r in self.rows if str(r["activity_date"]) >= since[:10]]
        return list(self.rows)


class FlakySource:
    """Fails with the given exceptions, then succeeds."""

    name = "coros_snapshot"

    def __init__(self, failures: int, exc: type[Exception] = TransientSourceError) -> None:
        self.failures = failures
        self.exc = exc
        self.attempts = 0

    def fetch(self, since: str | None = None):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.exc(f"boom {self.attempts}")
        return [record("ok-1", "2026-10-01")]


class IngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.store.init_schema()

    def tearDown(self) -> None:
        self.store.close()

    def bronze(self) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM bronze_activities ORDER BY activity_id")

    def test_rerun_is_idempotent(self) -> None:
        src = SyntheticSource(days=40)
        first = run_ingest(self.store, src)
        second = run_ingest(self.store, src)

        self.assertEqual(first.status, "success")
        self.assertGreater(first.rows_bronze, 0)
        self.assertEqual(second.rows_bronze, 0)  # nothing changed upstream

    def test_changed_payload_updates_bronze(self) -> None:
        src = ListSource([record("run-1", "2026-10-01", distance_m=10000.0)])
        run_ingest(self.store, src)

        src.rows = [record("run-1", "2026-10-01", distance_m=12345.0)]
        second = run_ingest(self.store, src, full_refresh=True)

        self.assertEqual(second.rows_bronze, 1)
        rows = self.bronze()
        self.assertEqual(len(rows), 1)
        self.assertIn("12345", rows[0]["payload"])

    def test_unchanged_payload_leaves_bronze_untouched(self) -> None:
        src = ListSource([record("run-1", "2026-10-01")])
        run_ingest(self.store, src)
        before = self.bronze()[0]["updated_at"]
        run_ingest(self.store, src, full_refresh=True)
        self.assertEqual(before, self.bronze()[0]["updated_at"])

    def test_incremental_fetch_uses_watermark_with_overlap(self) -> None:
        src = ListSource([record("a", "2026-10-01"), record("b", "2026-10-10")])
        run_ingest(self.store, src)
        self.assertIsNone(src.calls[0])  # first run has no watermark

        run_ingest(self.store, src, overlap_days=3)
        self.assertEqual(src.calls[1], "2026-10-07")  # watermark 10-10 minus 3 days

    def test_watermark_tracks_latest_activity_date(self) -> None:
        src = ListSource([record("a", "2026-10-01"), record("b", "2026-10-09")])
        run_ingest(self.store, src)
        self.assertEqual(get_watermark(self.store, "coros_snapshot"), "2026-10-09")

    def test_watermark_is_per_source(self) -> None:
        run_ingest(self.store, ListSource([record("a", "2026-10-01")]))
        self.assertIsNone(get_watermark(self.store, "synthetic"))

    def test_bad_payloads_are_stored_not_dropped(self) -> None:
        # Validation is dbt's job; bronze must keep everything it was given.
        result = run_ingest(self.store, SyntheticSource(days=20))
        ids = {r["activity_id"] for r in self.bronze()}
        self.assertEqual(result.rows_bronze, len(ids))
        self.assertIn("syn-bad-zero-distance", ids)
        self.assertIn("syn-bad-schema", ids)

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
        run_ingest(self.store, ListSource([record("a", "2026-10-01")]))
        runs = self.store.query(
            "SELECT status, rows_fetched, rows_bronze, finished_at FROM ops_pipeline_runs")
        self.assertEqual(runs[0]["status"], "success")
        self.assertEqual((runs[0]["rows_fetched"], runs[0]["rows_bronze"]), (1, 1))
        self.assertIsNotNone(runs[0]["finished_at"])

    def test_store_is_usable_from_another_thread(self) -> None:
        # Regression: Streamlit caches one Store and reruns the script on worker
        # threads, so a thread-bound SQLite connection raised ProgrammingError.
        run_ingest(self.store, ListSource([record("a", "2026-10-01")]))
        captured: list[object] = []

        def read() -> None:
            try:
                captured.append(len(self.bronze()))
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                captured.append(exc)

        thread = threading.Thread(target=read)
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(captured, [1])

    def test_retry_helper_stops_at_max_attempts(self) -> None:
        src = FlakySource(failures=99)
        with self.assertRaises(TransientSourceError):
            _fetch_with_retry(src, None, max_attempts=4, sleep=lambda _: None)
        self.assertEqual(src.attempts, 4)

    def test_payload_hash_ignores_key_order(self) -> None:
        self.assertEqual(payload_hash({"a": 1, "b": 2}), payload_hash({"b": 2, "a": 1}))
        self.assertNotEqual(payload_hash({"a": 1}), payload_hash({"a": 2}))


class MigrationTest(unittest.TestCase):
    def test_old_database_is_upgraded_in_place(self) -> None:
        store = Store(":memory:")
        conn = store.connect()
        conn.executescript(
            "CREATE TABLE bronze_activities (activity_id TEXT PRIMARY KEY, source TEXT NOT NULL,"
            " payload TEXT NOT NULL, payload_hash TEXT NOT NULL, fetched_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL);"
            "CREATE TABLE silver_activities (activity_id TEXT);"
            "CREATE TABLE silver_rejects (activity_id TEXT);"
        )
        store.init_schema()

        cols = {r["name"] for r in store.query("PRAGMA table_info(bronze_activities)")}
        tables = {r["name"] for r in store.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("activity_date", cols)
        # On SQLite the table name is shared with the dbt model, so it must survive.
        self.assertIn("silver_rejects", tables)
        store.init_schema()  # and the migration is repeatable
        store.close()


class DsnFromEnvTest(unittest.TestCase):
    def test_database_url_wins(self) -> None:
        env = {"DATABASE_URL": "postgresql://u:p@h/d", "PGHOST": "other"}
        self.assertEqual(dsn_from_env(env), "postgresql://u:p@h/d")

    def test_assembled_from_pg_variables_with_escaping(self) -> None:
        env = {"PGHOST": "h.neon.tech", "PGUSER": "me", "PGPASSWORD": "p@ss/w:rd",
               "PGDATABASE": "db"}
        self.assertEqual(dsn_from_env(env),
                         "postgresql://me:p%40ss%2Fw%3Ard@h.neon.tech:5432/db?sslmode=require")

    def test_incomplete_pg_variables_fall_back_to_sqlite(self) -> None:
        self.assertEqual(dsn_from_env({"PGHOST": "h", "PGUSER": "u"}), "")



class DsnSafetyTest(unittest.TestCase):
    def test_surrounding_quotes_are_tolerated(self) -> None:
        self.assertEqual(dsn_from_env({"DATABASE_URL": "'postgresql://u:p@h/d'"}),
                         "postgresql://u:p@h/d")
        self.assertEqual(dsn_from_env({"DATABASE_URL": ' "postgresql://u:p@h/d" '}),
                         "postgresql://u:p@h/d")

    def test_unsupported_scheme_is_refused_without_echoing_it(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Store("mysql://user:hunter2@host/db")
        self.assertNotIn("hunter2", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
