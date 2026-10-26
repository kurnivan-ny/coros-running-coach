"""Coach tools (against hand-written gold rows), router, number guard, LLM client."""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request
from datetime import date
from typing import Any

from corach.coach.chat import answer, extract_numbers, guard_llm_answer, route
from corach.coach.llm import (
    LLMClient,
    LLMConfig,
    LLMError,
    build_client,
    validate_base_url,
)
from corach.coach.tools import TOOLS, ToolArgumentError, bounded_int, call_tool
from helpers import add_activity, add_load, add_weekly, gold_store

TODAY = date(2026, 10, 14)  # a Wednesday


def seeded_store():
    store = gold_store()
    add_activity(store, "a", "2026-10-13", distance_km=10.0, duration_min=50, avg_hr=150, trimp=60)
    add_activity(store, "b", "2026-10-12", distance_km=12.0, duration_min=65, avg_hr=None,
                 trimp=None)
    add_activity(store, "c", "2026-09-20", distance_km=21.0, duration_min=120, avg_hr=160,
                 trimp=150)
    add_activity(store, "s1", "2026-10-11", sport="strength", name="Strength",
                 distance_km=None, duration_min=45, avg_hr=105, sets=20, trimp=15)
    add_activity(store, "w1", "2026-10-10", sport="walk", name="Walk",
                 distance_km=3.0, duration_min=40, avg_hr=110, trimp=8)
    add_weekly(store, "2026-10-05", sessions=3, distance_km=30.0)
    add_weekly(store, "2026-09-28", sessions=3, distance_km=25.0)
    add_weekly(store, "2026-09-21", sessions=2, distance_km=20.0)
    add_weekly(store, "2026-09-14", sessions=3, distance_km=28.0)
    add_load(store, "2026-10-13", acwr=1.4, zone="caution")
    add_load(store, "2026-10-14", acute=75.5, chronic=90.0, acwr=0.84, zone="optimal")
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
             "recent_activities", "trend", "sport_breakdown", "workload", "training_plan"},
        )

    def test_sql_in_arguments_is_rejected_not_executed(self) -> None:
        with self.assertRaises(ToolArgumentError):
            call_tool(self.store, "window", sport="run; DROP TABLE fct_activities;--")
        self.assertEqual(call_tool(self.store, "data_coverage")["activity_count"], 5)

    def test_data_coverage(self) -> None:
        facts = call_tool(self.store, "data_coverage")
        self.assertEqual(facts["activity_count"], 5)
        self.assertEqual(facts["sport_counts"], {"run": 3, "strength": 1, "walk": 1})
        self.assertFalse(facts["is_synthetic"])
        self.assertEqual(facts["last_activity_date"], "2026-10-13")

    def test_data_coverage_flags_synthetic(self) -> None:
        add_activity(self.store, "syn", "2026-10-01", source="synthetic")
        self.assertTrue(call_tool(self.store, "data_coverage")["is_synthetic"])

    def test_window_totals_for_runs(self) -> None:
        facts = call_tool(self.store, "window", days=7, today=TODAY)
        self.assertEqual(facts["sessions"], 2)
        self.assertEqual(facts["distance_km"], 22.0)
        self.assertEqual(facts["duration_min"], 115.0)
        # total duration / total distance, not a mean of paces: 6900 s over 22 km
        self.assertEqual(facts["pace_s_per_km"], 313.6)
        self.assertEqual(facts["pace_per_km"], "5:14")

    def test_window_hr_is_duration_weighted_and_skips_missing(self) -> None:
        facts = call_tool(self.store, "window", days=7, today=TODAY)
        self.assertEqual(facts["avg_hr"], 150)  # only run a reports HR; b is not counted as 0

    def test_window_reports_null_hr_when_none_reported(self) -> None:
        store = gold_store()
        add_activity(store, "x", "2026-10-13", avg_hr=None, trimp=None)
        facts = call_tool(store, "window", days=7, today=TODAY)
        self.assertEqual(facts["sessions"], 1)
        self.assertIsNone(facts["avg_hr"])  # not 0
        store.close()

    def test_window_for_strength_reports_sets_not_pace(self) -> None:
        facts = call_tool(self.store, "window", days=7, sport="strength", today=TODAY)
        self.assertEqual(facts["sessions"], 1)
        self.assertEqual(facts["sets"], 20)
        self.assertIsNone(facts["pace_s_per_km"])

    def test_window_rejects_disallowed_days_and_sports(self) -> None:
        with self.assertRaises(ToolArgumentError):
            call_tool(self.store, "window", days=14)
        with self.assertRaises(ToolArgumentError):
            call_tool(self.store, "window", sport="curling")

    def test_window_flags_insufficient_history(self) -> None:
        ninety = call_tool(self.store, "window", days=90, today=TODAY)
        seven = call_tool(self.store, "window", days=7, today=TODAY)
        self.assertFalse(ninety["sufficient_history"])
        self.assertTrue(seven["sufficient_history"])

    def test_longest_run_ignores_other_sports(self) -> None:
        add_activity(self.store, "hike", "2026-10-02", sport="walk", distance_km=40.0)
        facts = call_tool(self.store, "longest_run")
        self.assertEqual(facts["distance_km"], 21.0)
        self.assertEqual(facts["date"], "2026-09-20")

    def test_longest_run_on_empty_db(self) -> None:
        store = gold_store()
        self.assertFalse(call_tool(store, "longest_run")["found"])
        store.close()

    def test_recent_activities_limit_sport_filter_and_order(self) -> None:
        with self.assertRaises(ToolArgumentError):
            call_tool(self.store, "recent_activities", limit=10_000)
        facts = call_tool(self.store, "recent_activities", limit=2)
        self.assertEqual([a["date"] for a in facts["activities"]], ["2026-10-13", "2026-10-12"])
        strength = call_tool(self.store, "recent_activities", sport="strength")
        self.assertEqual(len(strength["activities"]), 1)
        self.assertEqual(strength["activities"][0]["sets"], 20)

    def test_trend_compares_adjacent_windows(self) -> None:
        facts = call_tool(self.store, "trend", days=28, today=TODAY)
        self.assertEqual(facts["current_km"], 43.0)   # Sep 17..Oct 14 includes the 21 km run
        self.assertIsNone(facts["change_pct"])        # nothing in the 28 days before that

    def test_trend_change_is_none_against_empty_prior_window(self) -> None:
        store = gold_store()
        add_activity(store, "only", "2026-10-13")
        self.assertIsNone(call_tool(store, "trend", days=7, today=TODAY)["change_pct"])
        store.close()

    def test_weekly_volume_zero_fills_and_reports_last_full_week(self) -> None:
        facts = call_tool(self.store, "weekly_volume", weeks=4, today=TODAY)
        self.assertEqual(len(facts["series"]), 4)
        self.assertEqual(facts["series"][-1]["sessions"], 0)       # this week: no row
        self.assertEqual(facts["last_full_week_km"], 30.0)
        self.assertEqual(facts["week_over_week_pct"], 20.0)        # 30 vs 25

    def test_sport_breakdown(self) -> None:
        facts = call_tool(self.store, "sport_breakdown", days=7, today=TODAY)
        sports = {s["sport"]: s for s in facts["sports"]}
        self.assertEqual(set(sports), {"run", "strength", "walk"})
        self.assertEqual(sports["run"]["sessions"], 2)

    def test_workload_uses_latest_day(self) -> None:
        facts = call_tool(self.store, "workload")
        self.assertEqual(facts["as_of"], "2026-10-14")
        self.assertEqual(facts["acwr"], 0.84)
        self.assertEqual(facts["acwr_zone"], "optimal")

    def test_workload_without_data(self) -> None:
        store = gold_store()
        self.assertFalse(call_tool(store, "workload")["found"])
        store.close()

    def test_training_plan_is_built_from_complete_weeks(self) -> None:
        plan = call_tool(self.store, "training_plan", today=TODAY)
        self.assertTrue(plan["found"])
        self.assertGreater(plan["basis_weekly_km"], 0)
        self.assertEqual(len(plan["days"]), 7)

    def test_training_plan_arguments_are_bounded(self) -> None:
        with self.assertRaises(ToolArgumentError):
            call_tool(self.store, "training_plan", sessions_per_week=40, today=TODAY)


class RouterTest(unittest.TestCase):
    def test_routes_to_expected_intents(self) -> None:
        self.assertEqual(route("What was my longest run?")[0], "longest_run")
        self.assertEqual(route("show my recent runs")[0], "recent_activities")
        self.assertEqual(route("is my data fresh?")[0], "data_coverage")
        self.assertEqual(route("am I improving?")[0], "trend")
        self.assertEqual(route("weekly mileage please")[0], "weekly_volume")
        self.assertEqual(route("how is my training load?")[0], "workload")
        self.assertEqual(route("give me a plan for next week")[0], "training_plan")
        self.assertEqual(route("what sports did I do")[0], "sport_breakdown")

    def test_sport_is_detected(self) -> None:
        self.assertEqual(route("how many gym sessions last month")[1]["sport"], "strength")
        self.assertEqual(route("my swim distance this week")[1]["sport"], "swim")
        self.assertEqual(route("how far did I run this week")[1]["sport"], "run")

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
        result = answer(self.store, "longest run?", client=FakeClient(text), today=TODAY)
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
        store = gold_store()
        add_activity(store, "x", "2026-10-13", avg_hr=None, trimp=None)
        result = answer(store, "what was my average heart rate?", client=None, today=TODAY)
        store.close()
        self.assertEqual(result.intent, "window")
        self.assertIn("No heart-rate data", result.answer)
        self.assertNotIn("0 bpm", result.answer)  # NULL must never render as zero

    def test_workload_answer_names_the_zone(self) -> None:
        result = answer(self.store, "am I overdoing it?", client=None, today=TODAY)
        self.assertEqual(result.intent, "workload")
        self.assertIn("optimal", result.answer)
        self.assertIn("0.84", result.answer)

    def test_strength_question_reports_sets(self) -> None:
        result = answer(self.store, "how many gym sessions this week", client=None, today=TODAY)
        self.assertIn("20 sets", result.answer)

    def test_plan_answer_uses_only_plan_facts(self) -> None:
        result = answer(self.store, "plan my next week", client=None, today=TODAY)
        self.assertEqual(result.intent, "training_plan")
        ok, reason = guard_llm_answer(result.answer, result.facts)
        self.assertTrue(ok, reason)  # every number in the answer exists in the facts

    def test_every_deterministic_answer_passes_its_own_guard(self) -> None:
        for question in ("longest run", "recent", "fresh data", "trend", "weekly", "28 days",
                         "load", "what sports", "walk distance", "gym this month"):
            result = answer(self.store, question, client=None, today=TODAY)
            ok, reason = guard_llm_answer(result.answer, result.facts)
            self.assertTrue(ok, f"{question!r}: {reason}")


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
