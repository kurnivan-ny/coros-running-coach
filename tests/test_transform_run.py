"""The dbt runner wrapper: DSN expansion and failure modes (dbt itself is not invoked)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from corach.transform_run import main, pg_env_from_dsn


class PgEnvTest(unittest.TestCase):
    def test_splits_a_neon_style_url(self) -> None:
        env = pg_env_from_dsn("postgresql://me:p%40ss%2Fw@ep-x-pooler.neon.tech:5432/db?sslmode=require")
        self.assertEqual(env["PGHOST"], "ep-x-pooler.neon.tech")
        self.assertEqual(env["PGPORT"], "5432")
        self.assertEqual(env["PGUSER"], "me")
        self.assertEqual(env["PGPASSWORD"], "p@ss/w")  # percent-decoded
        self.assertEqual(env["PGDATABASE"], "db")
        self.assertEqual(env["PGSSLMODE"], "require")

    def test_defaults_port_and_forces_ssl(self) -> None:
        env = pg_env_from_dsn("postgres://u:p@h/d")
        self.assertEqual(env["PGPORT"], "5432")
        self.assertEqual(env["PGSSLMODE"], "require")

    def test_sqlite_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pg_env_from_dsn("data/corach.db")


class MainTest(unittest.TestCase):
    def test_fails_cleanly_without_a_database(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["build"]), 1)


if __name__ == "__main__":
    unittest.main()
