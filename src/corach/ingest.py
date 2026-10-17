"""Incremental, idempotent ingestion: source -> bronze -> silver/rejects."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .quality import validate
from .sources import PermanentSourceError, Source, TransientSourceError
from .store import UPSERT_BRONZE, UPSERT_REJECT, UPSERT_SILVER, Store
from .transform import SchemaError, parse_activity, payload_hash

DEFAULT_OVERLAP_DAYS = 3
DEFAULT_MAX_ATTEMPTS = 3


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunResult:
    run_id: str
    source: str
    status: str
    rows_fetched: int = 0
    rows_bronze: int = 0
    rows_silver: int = 0
    rows_rejected: int = 0
    watermark: str | None = None
    error: str | None = None
    reject_reasons: list[str] = field(default_factory=list)


def get_watermark(store: Store, source: str) -> str | None:
    """Latest successfully ingested activity date for this source."""
    rows = store.query(
        "SELECT MAX(start_date_utc) AS wm FROM silver_activities WHERE source = ?", (source,)
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
           rows_bronze=?, rows_silver=?, rows_rejected=?, watermark=?, error=?
           WHERE run_id=?""",
        (result.status, _now_iso(), result.rows_fetched, result.rows_bronze,
         result.rows_silver, result.rows_rejected, result.watermark,
         result.error, result.run_id),
    )
    store.commit()


def _write_silver(store: Store, payload: dict[str, Any], source: str,
                  phash: str, now: str, result: RunResult) -> None:
    """Parse and validate one payload into silver, or quarantine it with a reason."""
    try:
        activity = parse_activity(payload, source)
    except (SchemaError, ValueError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        store.execute(UPSERT_REJECT, (
            str(payload.get("activity_id") or f"unknown-{phash[:12]}"),
            source, reason, json.dumps(payload, default=str), now,
        ))
        result.rows_rejected += 1
        result.reject_reasons.append(reason)
        return

    verdict = validate(activity)
    if not verdict.ok:
        store.execute(UPSERT_REJECT, (
            activity.activity_id, source, verdict.reason or "invalid",
            json.dumps(payload, default=str), now,
        ))
        result.rows_rejected += 1
        result.reject_reasons.append(verdict.reason or "invalid")
        return

    store.execute(UPSERT_SILVER, (
        activity.activity_id, source, activity.start_time_utc, activity.start_date_utc,
        activity.distance_m, activity.duration_s, activity.avg_hr, phash, now,
    ))
    result.rows_silver += 1


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
                unchanged = bool(prior) and prior[0]["payload_hash"] == phash

                store.execute(UPSERT_BRONZE, (
                    activity_id, source.name, json.dumps(payload, default=str),
                    phash, now, now,
                ))
                if not unchanged:
                    result.rows_bronze += 1

                # Silver is rewritten even when bronze is unchanged, so a rerun
                # after a validation-rule change still converges.
                _write_silver(store, payload, source.name, phash, now, result)

            result.watermark = get_watermark(store, source.name)

        result.status = "success"
        _finish_run(store, result)
        return result

    except Exception as exc:
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        _finish_run(store, result)
        raise


def rebuild_silver(store: Store, source: str | None = None) -> RunResult:
    """Reprocess silver from bronze without re-contacting the source."""
    store.init_schema()
    run_id = str(uuid.uuid4())
    result = RunResult(run_id=run_id, source=source or "rebuild", status="running")
    _start_run(store, run_id, result.source)

    try:
        if source:
            rows = store.query(
                "SELECT activity_id, source, payload, payload_hash FROM bronze_activities "
                "WHERE source = ?", (source,))
        else:
            rows = store.query(
                "SELECT activity_id, source, payload, payload_hash FROM bronze_activities")

        result.rows_fetched = len(rows)
        now = _now_iso()
        with store.transaction():
            for row in rows:
                raw = row["payload"]
                payload = json.loads(raw) if isinstance(raw, str) else raw
                _write_silver(store, payload, row["source"], row["payload_hash"], now, result)
            result.watermark = get_watermark(store, source) if source else None

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
    if name == "coros_mcp":
        from .sources.coros_mcp import CorosMCPSource
        return CorosMCPSource()
    raise ValueError(f"unknown source {name!r}; expected 'synthetic' or 'coros_mcp'")


def load_silver(store: Store, source: str | None = None) -> list[dict[str, Any]]:
    """All validated activities, oldest first."""
    if source:
        return store.query(
            "SELECT * FROM silver_activities WHERE source = ? ORDER BY start_time_utc", (source,))
    return store.query("SELECT * FROM silver_activities ORDER BY start_time_utc")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Non-zero exit on failure."""
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="corach-ingest")
    parser.add_argument("--source", default=os.environ.get("CORACH_SOURCE", "synthetic"))
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--rebuild-silver", action="store_true")
    parser.add_argument("--overlap-days", type=int,
                        default=int(os.environ.get("CORACH_OVERLAP_DAYS", DEFAULT_OVERLAP_DAYS)))
    args = parser.parse_args(argv)

    store = Store()
    try:
        if args.rebuild_silver:
            res = rebuild_silver(store, args.source)
        else:
            res = run_ingest(store, build_source(args.source),
                             overlap_days=args.overlap_days, full_refresh=args.full_refresh)
    except Exception as exc:
        print(f"ingest FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        store.close()

    print(f"ingest {res.status}: fetched={res.rows_fetched} bronze_changed={res.rows_bronze} "
          f"silver={res.rows_silver} rejected={res.rows_rejected} watermark={res.watermark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
