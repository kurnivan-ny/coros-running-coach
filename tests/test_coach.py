"""Coach tools, router, number guard, and the LLM client (fake transport only)."""

from __future__ import annotations

import json
import unittest
import urllib.error
from datetime import date
from typing import Any

from corach.coach.chat import (
    answer,
    extract_numbers,
    guard_llm_answer,
    route,
)
from corach.coach.llm import (
    LLMClient,
    LLMConfig,
    LLMError,
    build_client,
    validate_base_url,
)
from corach.coach.tools import TOOLS, ToolArgumentError, bounded_int, call_tool
from corach.ingest import run_ingest
from corach.store import Store
from corach.transform import SCHEMA_V1

TODAY = date(2026, 10, 17)


def activity(aid: str, day: str, dist: float = 10000.0,
             dur: float = 3000.0, hr: float | None = 150.0) -> dict[str, Any]:
    return {
        "schema": SCHEMA_V1, "activity_id": aid, "start_time": f"{day}T06:00:00+00:00",
        "distance_m": dist, "duration_s": dur, "avg_hr": hr,
    }


class FixedSource:
    name = "synthetic"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetch(self, since: str | None = None):
        return list(self.rows)


def seeded_store(rows: list[dict[str, Any]] | None = None) -> Store:
    store = Store(":memory:")
    store.init_schema()
    run_ingest(store, FixedSource(rows if rows is not None else [
        activity("a", "2026-10-13", 10000.0, 3000.0, 150.0),
        activity("b", "2026-10-15", 12000.0, 3900.0, None),
        activity("c", "2026-09-20", 21000.0, 7200.0, 160.0),
    ]))
    return store


class BoundedIntTest(unittest.TestCase):
    def test_accepts_in_range(self) -> None:
        self.assertEqual(bounded_int(5, "n", 1, 10), 5)
        self.assertEqual(bounded_int("7", "n", 1, 10), 7)

    def test_rejects_out_of_range(self) -> None:
        with self.assertRaises(ToolArgumentError):
            bounded_int(99, "n", 1, 10)
        with self.assertRaises(ToolArgumentError):
            bounded_int(0, "n", 1, 10)

    def test_rejects_non_integer_and_bool(self) -> None:
        with self.assertRaises(ToolArgumentError):
            bounded_int("abc", "n", 1, 10)
        with self.assertRaises(ToolArgumentError):
            bounded_int(True, "n", 1, 10)

    def test_default_applies_when_missing(self) -> None:
        self.assertEqual(bounded_int(None, "n", 1, 10, default=3), 3)
        with self.assertRaises(ToolArgumentError):
            bounded_int(None, "n", 1, 10)


class ToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = seeded_store()

    def tearDown(self) -> None:
        self.store.close()

    def test_unknown_tool_is_rejected(self) -> None:
        with self.assertRaises(ToolArgumentError):
            call_tool(self.store, "DROP TABLE")

    def test_whitelist_contains_only_expected_tools(self) -> None:
        self.assertEqual(
            set(TOOLS),
            {"data_coverage", "window", "weekly_volume", "longest_run",
             "recent_runs", "trend"},
        )

    def test_data_coverage_flags_synthetic(self) -> None:
        facts = call_tool(self.store, "data_coverage")
        self.assertEqual(facts["activity_count"], 3)
        self.assertTrue(facts["is_synthetic"])
        self.assertEqual(facts["last_activity_date"], "2026-10-15")

    def test_window_rejects_disallowed_days(self) -> None:
        with self.assertRaises(ToolArgumentError):
            call_tool(self.store, "window", days=14)

    def test_window_reports_null_hr_when_absent(self) -> None:
        store = seeded_store([activity("x", "2026-10-15", 10000.0, 3000.0, None)])
        facts = call_tool(store, "window", days=7, today=TODAY)
        self.assertEqual(facts["runs"], 1)
        self.assertIsNone(facts["avg_hr"])  # not 0
        store.close()

    def test_window_flags_insufficient_history(self) -> None:
        facts = call_tool(self.store, "window", days=90, today=TODAY)
        self.assertFalse(facts["sufficient_history"])

    def test_longest_run_picks_max_distance(self) -> None:
        facts = call_tool(self.store, "longest_run")
        self.assertTrue(facts["found"])
        self.assertAlmostEqual(facts["distance_km"], 21.0)

    def test_longest_run_on_empty_db(self) -> None:
        store = Store(":memory:")
        store.init_schema()
        self.assertFalse(call_tool(store, "longest_run")["found"])
        store.close()

    def test_recent_runs_limit_is_bounded(self) -> None:
        with self.assertRaises(ToolArgumentError):
            call_tool(self.store, "recent_runs", limit=10_000)
        facts = call_tool(self.store, "recent_runs", limit=2)
        self.assertEqual(len(facts["runs"]), 2)
        self.assertEqual(facts["runs"][0]["date"], "2026-10-15")  # newest first

    def test_trend_change_is_none_against_empty_prior_window(self) -> None:
        store = seeded_store([activity("only", "2026-10-15")])
        facts = call_tool(store, "trend", days=7, today=TODAY)
        self.assertIsNone(facts["change_pct"])
        store.close()

    def test_weekly_volume_series_length(self) -> None:
        facts = call_tool(self.store, "weekly_volume", weeks=4, today=TODAY)
        self.assertEqual(len(facts["series"]), 4)


class RouterTest(unittest.TestCase):
    def test_routes_to_expected_intents(self) -> None:
        self.assertEqual(route("What was my longest run?")[0], "longest_run")
        self.assertEqual(route("show my recent runs")[0], "recent_runs")
        self.assertEqual(route("is my data fresh?")[0], "data_coverage")
        self.assertEqual(route("am I improving?")[0], "trend")
        self.assertEqual(route("weekly mileage please")[0], "weekly_volume")

    def test_unknown_question_falls_back_to_coverage(self) -> None:
        self.assertEqual(route("what is the capital of France")[0], "data_coverage")

    def test_router_arguments_stay_in_whitelist(self) -> None:
        _, kwargs = route("90 day trend")
        self.assertEqual(kwargs["days"], 90)


class NumberGuardTest(unittest.TestCase):
    def test_extracts_decimals_and_paces(self) -> None:
        tokens = extract_numbers("You ran 10.5 km at 5:30 per km")
        self.assertIn("10.5", tokens)
        self.assertIn("5:30", tokens)

    def test_accepts_answer_using_only_verified_numbers(self) -> None:
        facts = {"distance_km": 10.0, "runs": 3}
        ok, reason = guard_llm_answer("You ran 3 times for 10 km.", facts)
        self.assertTrue(ok, reason)

    def test_rejects_hallucinated_number(self) -> None:
        facts = {"distance_km": 10.0, "runs": 3}
        ok, reason = guard_llm_answer("You ran 3 times for 42 km.", facts)
        self.assertFalse(ok)
        self.assertIn("42", reason or "")

    def test_rejects_invented_heart_rate_when_hr_is_null(self) -> None:
        facts = {"runs": 1, "distance_km": 10.0, "avg_hr": None}
        ok, _ = guard_llm_answer("You averaged 155 bpm over 1 run of 10 km.", facts)
        self.assertFalse(ok)  # null must not become a number

    def test_accepts_numbers_nested_in_lists(self) -> None:
        facts = {"runs": [{"distance_km": 7.5}]}
        ok, reason = guard_llm_answer("Your run was 7.5 km.", facts)
        self.assertTrue(ok, reason)


class ChatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = seeded_store()

    def tearDown(self) -> None:
        self.store.close()

    def test_deterministic_answer_without_llm(self) -> None:
        result = answer(self.store, "What was my longest run?", client=None, today=TODAY)
        self.assertEqual(result.mode, "deterministic")
        self.assertIn("21.0 km", result.answer)
        self.assertEqual(result.facts[0]["tool"], "longest_run")

    def test_llm_answer_is_used_when_it_passes_the_guard(self) -> None:
        facts = call_tool(self.store, "longest_run")
        text = f"Nice work: {facts['distance_km']} km on {facts['date']}."
        client = FakeClient(text)
        result = answer(self.store, "longest run?", client=client, today=TODAY)
        self.assertEqual(result.mode, "llm")
        self.assertEqual(result.answer, text)

    def test_llm_answer_with_bad_number_falls_back(self) -> None:
        client = FakeClient("You ran a massive 99.9 km personal best.")
        result = answer(self.store, "longest run?", client=client, today=TODAY)
        self.assertEqual(result.mode, "llm_rejected")
        self.assertIn("21.0 km", result.answer)  # deterministic text retained
        self.assertIn("99.9", result.note or "")

    def test_llm_failure_falls_back_to_deterministic(self) -> None:
        result = answer(self.store, "longest run?", client=FailingClient(), today=TODAY)
        self.assertEqual(result.mode, "deterministic")
        self.assertIn("LLM unavailable", result.note or "")

    def test_empty_llm_response_falls_back(self) -> None:
        result = answer(self.store, "longest run?", client=FakeClient("   "), today=TODAY)
        self.assertEqual(result.mode, "deterministic")

    def test_null_hr_is_described_as_unavailable(self) -> None:
        # Routes to the `window` tool, which is the one that reports heart rate.
        store = seeded_store([activity("x", "2026-10-15", 10000.0, 3000.0, None)])
        result = answer(store, "what was my average heart rate?", client=None, today=TODAY)
        store.close()
        self.assertEqual(result.intent, "window")
        self.assertIn("No heart-rate data", result.answer)
        self.assertNotIn("0 bpm", result.answer)  # NULL must never render as zero

    def test_weekly_question_routes_to_weekly_volume(self) -> None:
        result = answer(self.store, "how far did I run this week?", client=None, today=TODAY)
        self.assertEqual(result.intent, "weekly_volume")


class FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, system: str, user: str, **kwargs: Any) -> str:
        return self.text


class FailingClient:
    def complete(self, system: str, user: str, **kwargs: Any) -> str:
        raise LLMError("transport exploded")


class LLMClientTest(unittest.TestCase):
    def test_https_is_required_for_remote_hosts(self) -> None:
        with self.assertRaises(LLMError):
            validate_base_url("http://api.example.com/v1")

    def test_http_is_allowed_for_localhost(self) -> None:
        self.assertEqual(validate_base_url("http://localhost:11434/v1"),
                         "http://localhost:11434/v1")
        self.assertTrue(validate_base_url("http://127.0.0.1:11434/v1"))

    def test_rejects_unknown_scheme(self) -> None:
        with self.assertRaises(LLMError):
            validate_base_url("ftp://example.com")

    def test_complete_parses_openai_shape(self) -> None:
        captured: dict[str, Any] = {}

        def transport(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            return json.dumps(
                {"choices": [{"message": {"content": "hello"}}]}
            ).encode()

        client = LLMClient(LLMConfig("https://api.example.com/v1", "m", "secret-key"),
                           transport=transport)
        self.assertEqual(client.complete("sys", "user"), "hello")
        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "m")

    def test_api_key_never_leaks_in_errors(self) -> None:
        secret = "sk-super-secret-value"

        def transport(request, timeout):
            raise RuntimeError(f"connection refused using {secret}")

        client = LLMClient(LLMConfig("https://api.example.com/v1", "m", secret),
                           transport=transport)
        with self.assertRaises(LLMError) as ctx:
            client.complete("sys", "user")
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIn("REDACTED", str(ctx.exception))

    def test_http_error_body_is_redacted(self) -> None:
        secret = "sk-leaky"

        def transport(request, timeout):
            raise urllib.error.HTTPError(
                "https://api.example.com", 401,
                "Unauthorized", {}, None,
            )

        client = LLMClient(LLMConfig("https://api.example.com/v1", "m", secret),
                           transport=transport)
        with self.assertRaises(LLMError) as ctx:
            client.complete("sys", "user")
        self.assertNotIn(secret, str(ctx.exception))

    def test_malformed_response_raises_llm_error(self) -> None:
        client = LLMClient(LLMConfig("https://api.example.com/v1", "m", "k"),
                           transport=lambda r, t: b"{}")
        with self.assertRaises(LLMError):
            client.complete("sys", "user")

    def test_default_transport_refuses_non_http_schemes(self) -> None:
        # Guards against urlopen honouring file:// if a bad URL ever slips through.
        request = urllib.request.Request("file:///etc/passwd")
        with self.assertRaises(LLMError):
            LLMClient._urlopen(request, 1.0)

    def test_build_client_returns_none_when_unconfigured(self) -> None:
        self.assertIsNone(build_client(env={}))
        self.assertIsNone(build_client(env={"LLM_BASE_URL": "https://x/v1"}))  # no model

    def test_build_client_from_env(self) -> None:
        client = build_client(
            env={"LLM_BASE_URL": "https://api.example.com/v1",
                 "LLM_MODEL": "llama", "LLM_API_KEY": "k"},
            transport=lambda r, t: b'{"choices":[{"message":{"content":"ok"}}]}',
        )
        self.assertIsNotNone(client)
        self.assertEqual(client.complete("s", "u"), "ok")


if __name__ == "__main__":
    unittest.main()
