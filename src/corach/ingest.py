"""Incremental, idempotent ingestion: source -> bronze.

Python's job ends at bronze. Silver (typing, validation, quarantine) and gold
(metrics) are dbt models built afterwards -- see ``corach.transform_run``.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .envfile import load_env
from .sources import PermanentSourceError, Source, TransientSourceError
from .store import UPSERT_BRONZE, Store

DEFAULT_OVERLAP_DAYS = 3
DEFAULT_MAX_ATTEMPTS = 3
SOURCES = ("coros_snapshot", "strava", "synthetic")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable hash of a payload. Key order must not change the result."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class RunResult:
    run_id: str
    source: str
    status: str
    rows_fetched: int = 0
    rows_bronze: int = 0
    watermark: str | None = None
    error: str | None = None


def get_watermark(store: Store, source: str) -> str | None:
    """Latest activity date already in bronze for this source."""
    rows = store.query(
        "SELECT MAX(activity_date) AS wm FROM bronze_activities WHERE source = ?", (source,)
    )
    return rows[0]["wm"] if rows and rows[0]["wm"] else None


def _fetch_with_retry(source: Source, since: str | None, max_attempts: int,
                      sleep: Callable[[float], None] = time.sleep,
                      base_delay: float = 1.0) -> list[dict[str, Any]]:
    """Fetch with exponential backoff. Only TransientSourceError is retried."""
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return list(source.fetch(since=since))
        except TransientSourceError as exc:
            last = exc
            if attempt < max_attempts:
                sleep(base_delay * (2 ** (attempt - 1)))
        except PermanentSourceError:
            raise  # never retried
    raise TransientSourceError(
        f"source {source.name!r} failed after {max_attempts} attempts: {last}"
    ) from last


def _start_run(store: Store, run_id: str, source: str) -> None:
    store.execute(
        "INSERT INTO ops_pipeline_runs (run_id, source, status, started_at) VALUES (?,?,?,?)",
        (run_id, source, "running", _now_iso()),
    )
    store.commit()


def _finish_run(store: Store, result: RunResult) -> None:
    store.execute(
        """UPDATE ops_pipeline_runs SET status=?, finished_at=?, rows_fetched=?,
           rows_bronze=?, watermark=?, error=? WHERE run_id=?""",
        (result.status, _now_iso(), result.rows_fetched, result.rows_bronze,
         result.watermark, result.error, result.run_id),
    )
    store.commit()


def _activity_date(payload: dict[str, Any]) -> str | None:
    """Calendar date of a payload, kept beside the JSON so the watermark is cheap."""
    value = payload.get("activity_date")
    return str(value)[:10] if value else None


def run_ingest(store: Store, source: Source, *, overlap_days: int = DEFAULT_OVERLAP_DAYS,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS, full_refresh: bool = False,
               sleep: Callable[[float], None] = time.sleep) -> RunResult:
    """Run one ingestion cycle.

    Idempotent: unchanged payloads leave bronze untouched. Failures are logged to
    ops_pipeline_runs and re-raised so the CLI exits non-zero.
    """
    store.init_schema()
    run_id = str(uuid.uuid4())
    result = RunResult(run_id=run_id, source=source.name, status="running")
    _start_run(store, run_id, source.name)

    try:
        since: str | None = None
        if not full_refresh:
            wm = get_watermark(store, source.name)
            if wm:
                # Re-read a few days to catch late edits to already-seen activities.
                since = (datetime.fromisoformat(wm).date()
                         - timedelta(days=overlap_days)).isoformat()

        payloads = _fetch_with_retry(source, since, max_attempts, sleep=sleep)
        result.rows_fetched = len(payloads)
        now = _now_iso()

        with store.transaction():
            for payload in payloads:
                phash = payload_hash(payload)
                activity_id = str(payload.get("activity_id") or f"unknown-{phash[:12]}")

                prior = store.query(
                    "SELECT payload_hash FROM bronze_activities WHERE activity_id = ?",
                    (activity_id,),
                )
                if prior and prior[0]["payload_hash"] == phash:
                    continue  # unchanged: bronze is left exactly as it was
                store.execute(UPSERT_BRONZE, (
                    activity_id, source.name, _activity_date(payload),
                    json.dumps(payload, default=str), phash, now, now,
                ))
                result.rows_bronze += 1
            result.watermark = get_watermark(store, source.name)

        result.status = "success"
        _finish_run(store, result)
        return result

    except Exception as exc:
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        _finish_run(store, result)
        raise


def build_source(name: str) -> Source:
    if name == "synthetic":
        from .sources.synthetic import SyntheticSource
        return SyntheticSource()
    if name == "strava":
        from .sources.strava import StravaSource, persist_refresh_token

        def keep_new_token(token: str) -> None:
            if persist_refresh_token(token):
                print("note: Strava issued a new refresh token; .env was updated.")
            else:
                print("::warning::Strava issued a NEW refresh token. Update the "
                      "STRAVA_REFRESH_TOKEN secret, or the next run will fail.")

        return StravaSource(on_new_refresh_token=keep_new_token)
    if name == "coros_snapshot":
        from .sources.coros_snapshot import CorosSnapshotSource
        return CorosSnapshotSource()
    raise ValueError(f"unknown source {name!r}; expected one of {SOURCES}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Non-zero exit on failure."""
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="corach-ingest")
    parser.add_argument("--source", default=os.environ.get("CORACH_SOURCE", "coros_snapshot"),
                        choices=SOURCES)
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--overlap-days", type=int,
                        default=int(os.environ.get("CORACH_OVERLAP_DAYS", DEFAULT_OVERLAP_DAYS)))
    args = parser.parse_args(argv)

    load_env()
    store = Store()
    try:
        res = run_ingest(store, build_source(args.source),
                         overlap_days=args.overlap_days, full_refresh=args.full_refresh)
    except Exception as exc:
        print(f"ingest FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        store.close()

    print(f"ingest {res.status}: fetched={res.rows_fetched} bronze_changed={res.rows_bronze} "
          f"watermark={res.watermark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
