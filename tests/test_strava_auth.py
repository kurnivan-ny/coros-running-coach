"""Strava authorisation helper: URL building and code extraction (no network)."""

from __future__ import annotations

import os
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from corach.strava_auth import authorize_url, extract_code, main


class AuthHelperTest(unittest.TestCase):
    def test_authorize_url_requests_activity_scope(self) -> None:
        url = authorize_url("12345")
        q = parse_qs(urlparse(url).query)
        self.assertEqual(urlparse(url).netloc, "www.strava.com")
        self.assertEqual(q["client_id"], ["12345"])
        self.assertIn("activity:read_all", q["scope"][0])
        self.assertEqual(q["response_type"], ["code"])

    def test_extracts_code_from_a_redirect_url(self) -> None:
        url = "http://localhost/?state=&code=abc123def&scope=read,activity:read_all"
        self.assertEqual(extract_code(url), "abc123def")

    def test_accepts_a_bare_code_and_quotes(self) -> None:
        self.assertEqual(extract_code("abc123"), "abc123")
        self.assertEqual(extract_code("'abc123'"), "abc123")

    def test_requires_client_credentials(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("corach.strava_auth.load_env"):
            self.assertEqual(main([]), 1)

    def test_no_arguments_prints_the_url_without_secrets(self) -> None:
        env = {"STRAVA_CLIENT_ID": "999", "STRAVA_CLIENT_SECRET": "top-secret-value"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("corach.strava_auth.load_env"), \
                mock.patch("builtins.print") as out:
            self.assertEqual(main([]), 0)
        printed = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("client_id=999", printed)
        self.assertNotIn("top-secret-value", printed)


if __name__ == "__main__":
    unittest.main()
