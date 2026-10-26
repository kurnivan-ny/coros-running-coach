"""Strava source: mapping, pagination, auth failures, secret redaction (fake transport)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from typing import Any

from corach.sources import PermanentSourceError, TransientSourceError
from corach.sources.strava import (
    PAGE_SIZE,
    StravaSource,
    persist_refresh_token,
    to_record,
)

SECRETS = {"client_id": "12345", "client_secret": "s3cr3t-client", "refresh_token": "r3fresh-tok"}


def act(i: int, **kw: Any) -> dict[str, Any]:
    base = {
        "id": i, "name": "Morning Run", "sport_type": "Run", "trainer": False,
        "start_date": "2026-10-17T00:30:00Z", "start_date_local": "2026-10-17T07:30:00Z",
        "elapsed_time": 3000, "moving_time": 2900, "distance": 10000.0,
        "average_heartrate": 151.4, "has_heartrate": True, "start_latlng": [-6.2, 106.8],
    }
    base.update(kw)
    return base


class Script:
    """Replays responses (dicts/lists, or exceptions) and records requests."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item).encode()


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://www.strava.com", code, "x", {}, io.BytesIO(b""))


def source(*responses: Any, **kw: Any) -> tuple[StravaSource, Script]:
    script = Script(*responses)
    return StravaSource(**SECRETS, transport=script, **kw), script


TOKEN = {"access_token": "acc-tok-1", "refresh_token": SECRETS["refresh_token"]}


class MappingTest(unittest.TestCase):
    def test_outdoor_run(self) -> None:
        r = to_record(act(1))
        self.assertEqual(r["schema"], "coros_record_v1")
        self.assertEqual(r["activity_id"], "strava-1")
        self.assertEqual(r["sport_type"], 100)
        self.assertEqual(r["activity_date"], "2026-10-17")  # local date, not UTC
        self.assertEqual(r["end_ts"] - r["start_ts"], 3000)
        self.assertEqual(r["duration_s"], 2900)  # moving time, like COROS workout time
        self.assertEqual(r["distance_m"], 10000.0)
        self.assertEqual(r["avg_hr"], 151)
        self.assertEqual((r["start_lat"], r["start_lon"]), (-6.2, 106.8))

    def test_treadmill_run_is_indoor(self) -> None:
        self.assertEqual(to_record(act(2, trainer=True))["sport_type"], 101)

    def test_strength_has_no_distance_and_no_invented_fields(self) -> None:
        r = to_record(act(3, sport_type="WeightTraining", distance=0.0))
        self.assertEqual(r["sport_type"], 402)
        self.assertIsNone(r["distance_m"])  # not applicable, so NULL (never 0)
        self.assertIsNone(r["sets"])
        self.assertIsNone(r["calories"])

    def test_zero_distance_run_stays_zero_so_it_is_quarantined(self) -> None:
        self.assertEqual(to_record(act(4, distance=0.0))["distance_m"], 0.0)

    def test_missing_heart_rate_is_none_not_zero(self) -> None:
        for kw in ({"has_heartrate": False, "average_heartrate": None},
                   {"has_heartrate": False, "average_heartrate": 120.0}):
            self.assertIsNone(to_record(act(5, **kw))["avg_hr"])

    def test_unknown_sport_maps_to_other(self) -> None:
        self.assertEqual(to_record(act(6, sport_type="Kitesurf"))["sport_type"], 9999)

    def test_missing_gps_is_none(self) -> None:
        r = to_record(act(7, start_latlng=[]))
        self.assertIsNone(r["start_lat"])


class FetchTest(unittest.TestCase):
    def test_authenticates_then_lists(self) -> None:
        src, script = source(TOKEN, [act(1), act(2)])
        rows = list(src.fetch())
        self.assertEqual([r["activity_id"] for r in rows], ["strava-1", "strava-2"])
        token_req, list_req = script.requests
        body = urllib.parse.parse_qs(token_req.data.decode())
        self.assertEqual(body["grant_type"], ["refresh_token"])
        self.assertEqual(list_req.get_header("Authorization"), "Bearer acc-tok-1")
        self.assertTrue(list_req.full_url.startswith("https://www.strava.com/api/v3/"))

    def test_pages_until_a_short_page(self) -> None:
        full = [act(i, start_date=f"2026-10-{1 + i % 28:02d}T00:00:00Z") for i in range(PAGE_SIZE)]
        src, script = source(TOKEN, full, [act(9999)])
        self.assertEqual(len(list(src.fetch())), PAGE_SIZE + 1)
        self.assertIn("page=2", script.requests[-1].full_url)

    def test_since_becomes_an_after_filter(self) -> None:
        src, script = source(TOKEN, [])
        list(src.fetch(since="2026-10-01"))
        self.assertIn("after=1790812800", script.requests[-1].full_url)

    def test_results_are_sorted_by_date(self) -> None:
        a = act(1, start_date="2026-10-20T00:00:00Z", start_date_local="2026-10-20T07:00:00Z")
        b = act(2, start_date="2026-10-10T00:00:00Z", start_date_local="2026-10-10T07:00:00Z")
        src, _ = source(TOKEN, [a, b])
        self.assertEqual([r["activity_id"] for r in src.fetch()], ["strava-2", "strava-1"])


class FailureTest(unittest.TestCase):
    def test_missing_configuration_is_permanent_and_names_the_variable(self) -> None:
        with self.assertRaises(PermanentSourceError) as ctx:
            StravaSource(client_id="1", client_secret="", refresh_token="x",
                         transport=Script()).fetch()
        self.assertIn("STRAVA_CLIENT_SECRET", str(ctx.exception))

    def test_rate_limit_is_transient_so_it_is_retried(self) -> None:
        src, _ = source(TOKEN, http_error(429))
        with self.assertRaises(TransientSourceError):
            src.fetch()

    def test_server_error_is_transient(self) -> None:
        src, _ = source(http_error(503))
        with self.assertRaises(TransientSourceError):
            src.fetch()

    def test_unauthorised_is_permanent_and_explains_the_scope(self) -> None:
        src, _ = source(TOKEN, http_error(401))
        with self.assertRaises(PermanentSourceError) as ctx:
            src.fetch()
        self.assertIn("activity:read_all", str(ctx.exception))

    def test_secrets_never_appear_in_errors(self) -> None:
        leak = RuntimeError(f"boom {SECRETS['client_secret']} {SECRETS['refresh_token']} acc-tok-1")
        src, _ = source(TOKEN, leak)
        with self.assertRaises(TransientSourceError) as ctx:
            src.fetch()
        for secret in (*SECRETS.values(), "acc-tok-1"):
            if len(secret) > 6:
                self.assertNotIn(secret, str(ctx.exception))
        self.assertIn("REDACTED", str(ctx.exception))

    def test_non_list_response_is_refused(self) -> None:
        src, _ = source(TOKEN, {"message": "Bad Request"})
        with self.assertRaises(PermanentSourceError):
            src.fetch()

    def test_non_https_requests_are_refused_by_default_transport(self) -> None:
        import urllib.request

        from corach.sources.strava import https_open

        with self.assertRaises(PermanentSourceError):
            https_open(urllib.request.Request("http://example.com"), 1.0)
        with self.assertRaises(PermanentSourceError):
            https_open(urllib.request.Request("file:///etc/passwd"), 1.0)


class TokenRotationTest(unittest.TestCase):
    def test_rotated_refresh_token_is_reported(self) -> None:
        seen: list[str] = []
        rotated = {**TOKEN, "refresh_token": "brand-new"}
        src, _ = source(rotated, [], on_new_refresh_token=seen.append)
        list(src.fetch())
        self.assertEqual(seen, ["brand-new"])

    def test_unchanged_refresh_token_is_not_reported(self) -> None:
        seen: list[str] = []
        src, _ = source(TOKEN, [], on_new_refresh_token=seen.append)
        list(src.fetch())
        self.assertEqual(seen, [])

    def test_persist_updates_only_the_token_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("A=1\nSTRAVA_REFRESH_TOKEN=old\nB=2\n")
            self.assertTrue(persist_refresh_token("new", path))
            self.assertEqual(open(path, encoding="utf-8").read(),
                             "A=1\nSTRAVA_REFRESH_TOKEN=new\nB=2\n")
            self.assertFalse(persist_refresh_token("x", os.path.join(tmp, "missing.env")))

    def test_persist_keeps_spaces_around_the_equals_sign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("STRAVA_CLIENT_ID = 1\nSTRAVA_REFRESH_TOKEN = old\n")
            self.assertTrue(persist_refresh_token("new", path))
            self.assertEqual(open(path, encoding="utf-8").read(),
                             "STRAVA_CLIENT_ID = 1\nSTRAVA_REFRESH_TOKEN = new\n")

    def test_persist_appends_when_the_line_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("A=1\n")
            self.assertTrue(persist_refresh_token("tok", path))
            self.assertEqual(open(path, encoding="utf-8").read(), "A=1\nSTRAVA_REFRESH_TOKEN=tok\n")


if __name__ == "__main__":
    unittest.main()
