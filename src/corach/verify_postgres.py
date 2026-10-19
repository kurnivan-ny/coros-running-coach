"""Postgres round-trip check, run by the `postgres` CI job.

The unit tests all run on SQLite, so the Postgres branch of store.py -- psycopg,
jsonb payload columns, and the ``?`` -> ``%s`` placeholder rewrite -- is never
exercised by them. This module asserts the properties that could plausibly
differ between the two backends, against a real server.

Exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import sys

from .ingest import load_silver, rebuild_silver, run_ingest
from .metrics import weighted_avg_hr
from .sources.synthetic import SyntheticSource
from .store import Store


def _check(label: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail else ''}")
    return condition


def main() -> int:
    store = Store()
    if not store.is_postgres:
        print("verify_postgres: DATABASE_URL is not a Postgres DSN; nothing to verify.")
        return 1

    print("verify_postgres: checking the Postgres backend")
    ok = True
    try:
        store.init_schema()

        # A prior ingest in the same CI job has already populated the database.
        rows = load_silver(store)
        ok &= _check("silver has rows", len(rows) > 0, f"{len(rows)} rows")

        # The property most likely to be silently broken by a backend swap:
        # a missing heart rate must come back as None, not 0 and not "NaN".
        null_hr = [r for r in rows if r["avg_hr"] is None]
        ok &= _check("missing HR reads back as NULL", len(null_hr) > 0,
                     f"{len(null_hr)} rows with NULL avg_hr")
        ok &= _check("no HR was coerced to zero",
                     all(r["avg_hr"] != 0 for r in rows))

        # jsonb round-trip: bronze payloads must still parse.
        bronze = store.query("SELECT payload FROM bronze_activities LIMIT 1")
        payload = bronze[0]["payload"] if bronze else None
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        ok &= _check("bronze payload round-trips as an object",
                     isinstance(payload, dict) and "schema" in payload)

        # Metrics must compute over Postgres rows exactly as over SQLite rows.
        hr = weighted_avg_hr(rows)
        ok &= _check("duration-weighted HR computes", hr is None or hr > 0,
                     "None" if hr is None else f"{hr:.1f} bpm")

        # ON CONFLICT upsert semantics are shared SQL; confirm they behave.
        before = len(rows)
        result = run_ingest(store, SyntheticSource())
        after = len(load_silver(store))
        ok &= _check("rerun is idempotent (no bronze writes)",
                     result.rows_bronze == 0, f"bronze_changed={result.rows_bronze}")
        ok &= _check("rerun did not duplicate rows", before == after,
                     f"{before} -> {after}")

        # Quarantine survived the backend too.
        rejects = store.query("SELECT COUNT(*) AS n FROM silver_rejects")
        ok &= _check("rejects were quarantined", rejects[0]["n"] > 0,
                     f"{rejects[0]['n']} rows")

        # rebuild_silver reprocesses from bronze without contacting the source.
        rebuilt = rebuild_silver(store, "synthetic")
        ok &= _check("rebuild_silver succeeds", rebuilt.status == "success",
                     f"silver={rebuilt.rows_silver}")

    except Exception as exc:  # noqa: BLE001 - report and fail the CI job
        print(f"  [FAIL] unexpected error: {type(exc).__name__}: {exc}")
        ok = False
    finally:
        store.close()

    print("verify_postgres:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
