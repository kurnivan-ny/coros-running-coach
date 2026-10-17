"""Thin storage layer over SQLite (local/CI) and Postgres (production).

One set of SQL statements serves both backends. SQLite uses ``?`` placeholders
and Postgres uses ``%s``; :meth:`Store.execute` rewrites them at the boundary so
callers write ``?`` everywhere.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# JSON column type differs: Postgres has native jsonb, SQLite stores TEXT.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS bronze_activities (
    activity_id   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    payload       {json_type} NOT NULL,
    payload_hash  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver_activities (
    activity_id    TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    start_time_utc TEXT NOT NULL,
    start_date_utc TEXT NOT NULL,
    distance_m     REAL NOT NULL,
    duration_s     REAL NOT NULL,
    avg_hr         REAL,
    payload_hash   TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver_rejects (
    activity_id  TEXT NOT NULL,
    source       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    payload      {json_type},
    rejected_at  TEXT NOT NULL,
    PRIMARY KEY (activity_id, reason)
);

CREATE TABLE IF NOT EXISTS ops_pipeline_runs (
    run_id         TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    status         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    rows_fetched   INTEGER NOT NULL DEFAULT 0,
    rows_bronze    INTEGER NOT NULL DEFAULT 0,
    rows_silver    INTEGER NOT NULL DEFAULT 0,
    rows_rejected  INTEGER NOT NULL DEFAULT 0,
    watermark      TEXT,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_silver_start ON silver_activities (start_date_utc);
"""


class Store:
    """Database handle. ``dsn`` picks the backend: ``postgresql://`` or a file path."""

    def __init__(self, dsn: str | None = None) -> None:
        dsn = dsn or os.environ.get("DATABASE_URL") or ""
        self.is_postgres = dsn.startswith(("postgresql://", "postgres://"))
        if self.is_postgres:
            self.dsn = dsn
        else:
            self.dsn = dsn or os.environ.get("CORACH_SQLITE_PATH") or "data/corach.db"
            if self.dsn != ":memory:":
                Path(self.dsn).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Any = None
        # Streamlit reruns the script on worker threads while caching this Store,
        # so the connection is shared across threads. SQLite allows that only
        # with check_same_thread=False, and only if callers serialise access --
        # hence the lock held around every statement.
        self._lock = threading.RLock()

    # -- connection -------------------------------------------------------
    def connect(self) -> Any:
        with self._lock:
            if self._conn is not None:
                return self._conn
            if self.is_postgres:
                import psycopg  # imported lazily so SQLite users need no driver

                self._conn = psycopg.connect(self.dsn)
            else:
                self._conn = sqlite3.connect(self.dsn, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Store:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- sql --------------------------------------------------------------
    def _adapt(self, sql: str) -> str:
        """SQLite dialect is canonical; rewrite placeholders for Postgres."""
        return sql.replace("?", "%s") if self.is_postgres else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            cur = self.connect().cursor()
            cur.execute(self._adapt(sql), tuple(params))
            return cur

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = [tuple(r) for r in rows]
        if not rows:
            return
        with self._lock:
            cur = self.connect().cursor()
            cur.executemany(self._adapt(sql), rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        # Cursor is consumed inside the lock so another thread cannot interleave
        # a statement between execute and fetchall.
        with self._lock:
            cur = self.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def commit(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.rollback()

    @contextmanager
    def transaction(self) -> Iterator[Store]:
        # The lock spans the whole transaction (RLock, so nested calls are fine),
        # so another thread cannot commit partway through it.
        with self._lock:
            try:
                yield self
                self.commit()
            except Exception:
                self.rollback()
                raise

    def init_schema(self) -> None:
        json_type = "jsonb" if self.is_postgres else "TEXT"
        sql = _SCHEMA.format(json_type=json_type)
        with self._lock:
            conn = self.connect()
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(sql)
            else:
                conn.executescript(sql)
            self.commit()


# -- shared upsert statements ---------------------------------------------
# Bronze updates only when the payload hash changed, so fetched_at churn does
# not mark unchanged rows as updated.
UPSERT_BRONZE = """
INSERT INTO bronze_activities
    (activity_id, source, payload, payload_hash, fetched_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (activity_id) DO UPDATE SET
    payload      = EXCLUDED.payload,
    payload_hash = EXCLUDED.payload_hash,
    fetched_at   = EXCLUDED.fetched_at,
    updated_at   = EXCLUDED.updated_at
WHERE bronze_activities.payload_hash <> EXCLUDED.payload_hash
"""

UPSERT_SILVER = """
INSERT INTO silver_activities
    (activity_id, source, start_time_utc, start_date_utc,
     distance_m, duration_s, avg_hr, payload_hash, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (activity_id) DO UPDATE SET
    source         = EXCLUDED.source,
    start_time_utc = EXCLUDED.start_time_utc,
    start_date_utc = EXCLUDED.start_date_utc,
    distance_m     = EXCLUDED.distance_m,
    duration_s     = EXCLUDED.duration_s,
    avg_hr         = EXCLUDED.avg_hr,
    payload_hash   = EXCLUDED.payload_hash,
    updated_at     = EXCLUDED.updated_at
"""

UPSERT_REJECT = """
INSERT INTO silver_rejects (activity_id, source, reason, payload, rejected_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (activity_id, reason) DO UPDATE SET
    payload     = EXCLUDED.payload,
    rejected_at = EXCLUDED.rejected_at
"""
