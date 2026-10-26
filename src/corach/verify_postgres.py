"""End-to-end Postgres check, run by the `postgres` CI job.

The unit tests all run on SQLite, so the Postgres branch of store.py (psycopg,
jsonb payloads, the ``?`` -> ``%s`` rewrite) and the dbt silver/gold layers are
never exercised by them. After CI has ingested SYNTHETIC data and run
``dbt build``, this module asserts the properties that could plausibly differ
between backends, against a real server, and that the coach tools run on the
gold tables dbt produced.

Exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import json
import sys

from .coach.tools import call_tool
from .ingest import run_ingest
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

    print("verify_postgres: checking Postgres bronze, dbt silver/gold and the coach tools")
    ok = True
    try:
        store.init_schema()

        bronze = store.query("SELECT payload FROM bronze_activities LIMIT 1")
        payload = bronze[0]["payload"] if bronze else None
        if isinstance(payload, str):
            payload = json.loads(payload)
        ok &= _check("bronze payload round-trips as an object",
                     isinstance(payload, dict) and "schema" in payload)

        counts = store.query(
            "SELECT (SELECT COUNT(*) FROM bronze_activities) AS bronze, "
            "(SELECT COUNT(*) FROM silver.silver_activities) AS silver, "
            "(SELECT COUNT(*) FROM silver.silver_rejects) AS rejects")[0]
        ok &= _check("every bronze row is in silver or quarantine",
                     counts["bronze"] == counts["silver"] + counts["rejects"], str(counts))
        ok &= _check("bad rows were quarantined", counts["rejects"] > 0,
                     f"{counts['rejects']} rows")

        reasons = {r["reason"] for r in store.query(
            "SELECT DISTINCT reason FROM silver.silver_rejects")}
        ok &= _check("quarantine reasons are recorded",
                     {"distance_non_positive", "hr_out_of_range", "duration_missing",
                      "unsupported_schema"} <= reasons, ", ".join(sorted(reasons)))

        # The property most likely to break silently: missing HR must stay NULL.
        gold = store.query(
            "SELECT COUNT(*) FILTER (WHERE avg_hr IS NULL) AS null_hr, "
            "COUNT(*) FILTER (WHERE avg_hr = 0) AS zero_hr, "
            "COUNT(*) FILTER (WHERE avg_hr IS NULL AND trimp IS NOT NULL) AS trimp_without_hr, "
            "COUNT(DISTINCT sport_group) AS sports FROM gold.fct_activities")[0]
        ok &= _check("missing HR reads back as NULL", gold["null_hr"] > 0,
                     f"{gold['null_hr']} rows")
        ok &= _check("no HR was coerced to zero", gold["zero_hr"] == 0)
        ok &= _check("no load invented for activities without HR", gold["trimp_without_hr"] == 0)
        ok &= _check("several sport groups present", gold["sports"] >= 3,
                     f"{gold['sports']} groups")

        # Re-ingesting unchanged data must not rewrite bronze.
        result = run_ingest(store, SyntheticSource())
        ok &= _check("rerun is idempotent (no bronze writes)",
                     result.rows_bronze == 0, f"bronze_changed={result.rows_bronze}")

        # The coach tools must run on the gold tables dbt actually built.
        coverage = call_tool(store, "data_coverage")
        ok &= _check("coverage tool reads gold", coverage["activity_count"] > 0,
                     f"{coverage['activity_count']} activities")
        for name, kwargs in (("window", {"days": 28}), ("workload", {}),
                             ("recent_activities", {}), ("sport_breakdown", {"days": 90}),
                             ("weekly_volume", {"weeks": 8}), ("longest_run", {}),
                             ("trend", {"days": 28})):
            facts = call_tool(store, name, **kwargs)
            ok &= _check(f"tool {name} runs", isinstance(facts, dict) and "tool" in facts)

    except Exception as exc:  # noqa: BLE001 - report and fail the CI job
        print(f"  [FAIL] unexpected error: {type(exc).__name__}: {exc}")
        ok = False
    finally:
        store.close()

    print("verify_postgres:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
