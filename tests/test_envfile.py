"""The .env loader used by the command-line entry points."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from corach.envfile import load_env


class LoadEnvTest(unittest.TestCase):
    def load(self, text: str, environ: dict[str, str] | None = None):
        environ = {} if environ is None else environ
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(text, encoding="utf-8")
            names = load_env(path, environ)
        return names, environ

    def test_loads_plain_quoted_and_exported_values(self) -> None:
        _, env = self.load("A=1\nB='two'\nC=\"three\"\nexport D=four\n")
        self.assertEqual(env, {"A": "1", "B": "two", "C": "three", "D": "four"})

    def test_skips_comments_blanks_and_empty_values(self) -> None:
        names, env = self.load("# comment\n\nDATABASE_URL=\nNOEQUALS\nX=ok\n")
        self.assertEqual(names, ["X"])
        self.assertNotIn("DATABASE_URL", env)

    def test_existing_environment_wins(self) -> None:
        _, env = self.load("A=from-file\n", {"A": "from-shell"})
        self.assertEqual(env["A"], "from-shell")

    def test_value_may_contain_equals_signs(self) -> None:
        _, env = self.load("URL=postgresql://u:p@h/d?sslmode=require\n")
        self.assertEqual(env["URL"], "postgresql://u:p@h/d?sslmode=require")

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(load_env("definitely-not-here.env", {}), [])


if __name__ == "__main__":
    unittest.main()
