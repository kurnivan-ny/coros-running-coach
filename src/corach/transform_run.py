"""Run the dbt silver and gold layers against the configured Postgres database.

dbt reads its connection from libpq ``PG*`` variables (see dbt/profiles.yml).
This wrapper accepts the same ``DATABASE_URL`` / ``PG*`` configuration as the rest
of the project and expands it, so there is one place to configure the database.

    python -m corach.transform_run              # dbt build (run + test)
    python -m corach.transform_run run          # models only
    python -m corach.transform_run test
    python -m corach.transform_run build --vars '{max_hr: 195}'
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .envfile import load_env
from .store import dsn_from_env

DBT_DIR = Path(__file__).resolve().parents[2] / "dbt"
COMMANDS = ("build", "run", "test", "compile", "debug")


def pg_env_from_dsn(dsn: str) -> dict[str, str]:
    """Split a ``postgresql://`` URL into libpq environment variables."""
    parsed = urlparse(dsn)
    if parsed.scheme not in ("postgresql", "postgres") or not parsed.hostname:
        raise ValueError("dbt needs a Postgres DSN (postgresql://...); SQLite is not supported")
    env = {
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": unquote(parsed.path.lstrip("/")),
    }
    sslmode = parse_qs(parsed.query).get("sslmode")
    env["PGSSLMODE"] = sslmode[0] if sslmode else "require"
    return env


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = "build"
    if args and args[0] in COMMANDS:
        command = args.pop(0)

    load_env()
    dsn = dsn_from_env()
    if not dsn:
        print("transform FAILED: set DATABASE_URL (or PGHOST/PGUSER/PGPASSWORD/PGDATABASE)")
        return 1
    try:
        os.environ.update(pg_env_from_dsn(dsn))
    except ValueError as exc:
        print(f"transform FAILED: {exc}")
        return 1

    try:
        from dbt.cli.main import dbtRunner
    except ImportError:
        print("transform FAILED: dbt is not installed. Run: pip install -r requirements-dev.txt")
        return 1

    result = dbtRunner().invoke(
        [command, "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR), *args]
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
