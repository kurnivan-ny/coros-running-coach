"""Thin storage layer over SQLite (local/CI) and Postgres (production).

One set of SQL statements serves both backends. SQLite uses ``?`` placeholders
and Postgres uses ``%s``; :meth:`Store.execute` rewrites them at the boundary so
callers write ``?`` everywhere.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# JSON column type differs: Postgres has native jsonb, SQLite stores TEXT.
# Only the bronze and ops tables live here. Silver and gold are dbt models (see
# dbt/), built in the ``silver`` and ``gold`` Postgres schemas.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS bronze_activities (
    activity_id    TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    activity_date  TEXT,
    payload        {json_type} NOT NULL,
    payload_hash   TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
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
"""

# Silver used to be Python-built tables in the public schema; dbt owns it now.
_LEGACY_TABLES = ("silver_activities", "silver_rejects")


_SCHEMA_PREFIX = re.compile(r"\b(?:gold|silver)\.(?=[a-z_])")


def dsn_from_env(env: Any = None) -> str:
    """Postgres DSN from ``DATABASE_URL``, else assembled from libpq ``PG*`` variables.

    Returns ``""`` when neither is configured, which selects SQLite. ``sslmode`` is
    forced to ``require`` for the assembled form because Neon rejects plaintext.
    """
    env = os.environ if env is None else env
    # Tolerate the quotes people copy along with a connection string.
    url = (env.get("DATABASE_URL") or "").strip().strip("'\"").strip()
    if url:
        return url
    host, user, password, dbname = (env.get(k) for k in
                                    ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"))
    if not (host and user and password and dbname):
        return ""
    from urllib.parse import quote
    port = env.get("PGPORT") or "5432"
    return (f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{dbname}?sslmode={env.get('PGSSLMODE') or 'require'}")


class Store:
    """Database handle. ``dsn`` picks the backend: ``postgresql://`` or a file path."""

    def __init__(self, dsn: str | None = None) -> None:
        dsn = dsn or dsn_from_env()
        self.is_postgres = dsn.startswith(("postgresql://", "postgres://"))
        if "://" in dsn and not self.is_postgres:
            # Never echo the value: a connection string carries the password.
            raise ValueError("database URL must start with postgresql:// (or be a file path)")
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
        """SQLite dialect is canonical; rewrite placeholders for Postgres.

        dbt builds ``silver`` and ``gold`` as Postgres schemas. SQLite has none, so
        there the prefixes are dropped and the same SQL runs against fixture tables
        in tests and demos.
        """
        if self.is_postgres:
            return sql.replace("?", "%s")
        return _SCHEMA_PREFIX.sub("", sql)

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
            self._migrate()
            self.commit()

    def _migrate(self) -> None:
        """Bring a database created by an older version up to the current schema."""
        if self.is_postgres:
            self.execute("ALTER TABLE bronze_activities "
                         "ADD COLUMN IF NOT EXISTS activity_date TEXT")
        else:
            cols = {r["name"] for r in self.query("PRAGMA table_info(bronze_activities)")}
            if "activity_date" not in cols:
                self.execute("ALTER TABLE bronze_activities ADD COLUMN activity_date TEXT")
        # After the column exists: an older database gets it from the ALTER above.
        self.execute("CREATE INDEX IF NOT EXISTS idx_bronze_source_date "
                     "ON bronze_activities (source, activity_date)")
        # Postgres only: there the old tables sit in ``public`` while dbt writes to the
        # ``silver`` schema. SQLite has no schemas, so the names would collide with the
        # dbt-shaped tables used in tests and demos.
        if self.is_postgres:
            for table in _LEGACY_TABLES:
                self.execute(f"DROP TABLE IF EXISTS public.{table}")  # nosec B608 - constants


# -- shared upsert statements ---------------------------------------------
# Bronze updates only when the payload hash changed, so fetched_at churn does
# not mark unchanged rows as updated.
UPSERT_BRONZE = """
INSERT INTO bronze_activities
    (activity_id, source, activity_date, payload, payload_hash, fetched_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (activity_id) DO UPDATE SET
    source        = EXCLUDED.source,
    activity_date = EXCLUDED.activity_date,
    payload       = EXCLUDED.payload,
    payload_hash  = EXCLUDED.payload_hash,
    fetched_at    = EXCLUDED.fetched_at,
    updated_at    = EXCLUDED.updated_at
WHERE bronze_activities.payload_hash <> EXCLUDED.payload_hash
"""
