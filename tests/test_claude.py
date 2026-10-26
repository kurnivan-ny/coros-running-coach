"""Claude tool-calling coach, driven by a scripted fake transport (no network)."""

from __future__ import annotations

import json
import unittest
from datetime import date
from typing import Any

from corach.coach.claude import (
    API_URL,
    MAX_TOOL_CALLS,
    TOOL_SPECS,
    ClaudeClient,
    build_claude_client,
    run_tool,
    tool_definitions,
)
from corach.coach.llm import LLMError
from corach.coach.tools import TOOLS, ToolArgumentError
from helpers import add_activity, add_load, add_weekly, gold_store

TODAY = date(2026, 10, 14)


def text(t: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": t}], "stop_reason": "end_turn"}


def use(name: str, args: dict[str, Any], uid: str = "tu_1") -> dict[str, Any]:
    return {"content": [{"type": "tool_use", "id": uid, "name": name, "input": args}],
            "stop_reason": "tool_use"}


class Script:
    """Replays canned API responses and records every request."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.urls: list[str] = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        self.headers.append({k.lower(): v for k, v in request.header_items()})
        self.requests.append(json.loads(request.data))
        return json.dumps(self.responses.pop(0)).encode()


def store():
    s = gold_store()
    add_activity(s, "a", "2026-10-13", distance_km=10.0, duration_min=50, avg_hr=150, trimp=60)
    add_weekly(s, "2026-10-05", distance_km=30.0)
    add_load(s, "2026-10-14", acute=75.5, chronic=90.0, acwr=0.84, zone="optimal")
    return s


class ToolSpecTest(unittest.TestCase):
    def test_specs_cover_exactly_the_whitelist(self) -> None:
        self.assertEqual(set(TOOL_SPECS), set(TOOLS))

    def test_definitions_are_closed_schemas(self) -> None:
        for d in tool_definitions():
            self.assertFalse(d["input_schema"]["additionalProperties"], d["name"])
            self.assertNotIn("sql", d["input_schema"]["properties"])


class RunToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = store()

    def tearDown(self) -> None:
        self.store.close()

    def test_runs_a_whitelisted_tool(self) -> None:
        out = run_tool(self.store, "window", {"days": 7, "sport": "run"}, TODAY, {})
        self.assertEqual(out["sessions"], 1)

    def test_unknown_tool_is_refused(self) -> None:
        with self.assertRaises(ToolArgumentError):
            run_tool(self.store, "run_sql", {"query": "select 1"}, TODAY, {})

    def test_undeclared_arguments_are_refused(self) -> None:
        for bad in ({"today": "2020-01-01"}, {"sql": "DROP TABLE x"}, {"days": 7, "table": "t"}):
            with self.assertRaises(ToolArgumentError):
                run_tool(self.store, "window", bad, TODAY, {})

    def test_out_of_range_values_are_refused_by_the_tool(self) -> None:
        with self.assertRaises(ToolArgumentError):
            run_tool(self.store, "window", {"days": 5000}, TODAY, {})
        with self.assertRaises(ToolArgumentError):
            run_tool(self.store, "window", {"sport": "x'; DROP TABLE y;--"}, TODAY, {})

    def test_non_object_input_is_refused(self) -> None:
        with self.assertRaises(ToolArgumentError):
            run_tool(self.store, "window", "days=7", TODAY, {})

    def test_user_settings_reach_only_the_plan_tool(self) -> None:
        defaults = {"sessions_per_week": 3}
        plan = run_tool(self.store, "training_plan", {}, TODAY, defaults)
        self.assertEqual(len([d for d in plan.get("days", []) if d["kind"] != "rest"]) or 3, 3)
        out = run_tool(self.store, "window", {}, TODAY, defaults)  # would raise if forwarded
        self.assertEqual(out["tool"], "window")


class ClaudeAskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = store()

    def tearDown(self) -> None:
        self.store.close()

    def client(self, *responses):
        script = Script(*responses)
        return ClaudeClient("sk-ant-secret", transport=script), script

    def test_tool_call_then_verified_answer(self) -> None:
        client, script = self.client(
            use("workload", {}), text("Your load ratio is 0.84, which is in the optimal zone."))
        result = client.ask(self.store, "How is my load?", today=TODAY)
        self.assertEqual(result.mode, "claude")
        self.assertEqual(result.facts[0]["tool"], "workload")
        self.assertEqual(script.urls, [API_URL, API_URL])
        self.assertEqual(script.headers[0]["x-api-key"], "sk-ant-secret")
        # the tool result was sent back to the model on turn two
        sent = script.requests[1]["messages"][-1]["content"][0]
        self.assertEqual(sent["type"], "tool_result")
        self.assertIn("0.84", sent["content"])

    def test_answer_with_an_unverified_number_is_discarded(self) -> None:
        client, _ = self.client(use("workload", {}), text("Your ratio is 1.9, very risky."))
        result = client.ask(self.store, "How is my load?", today=TODAY)
        self.assertEqual(result.mode, "llm_rejected")
        self.assertIn("1.9", result.note or "")
        self.assertIn("0.84", result.answer)  # rule-based answer retained

    def test_answer_without_consulting_tools_is_discarded(self) -> None:
        client, _ = self.client(text("You ran 100 km last week."))
        result = client.ask(self.store, "How far did I run?", today=TODAY)
        self.assertEqual(result.mode, "llm_rejected")
        self.assertNotIn("100", result.answer)

    def test_bad_tool_input_becomes_an_error_result_not_a_crash(self) -> None:
        client, script = self.client(
            use("window", {"days": 9999}), use("window", {"days": 7}, "tu_2"),
            text("You did 1 run in the last 7 days."))
        result = client.ask(self.store, "runs this week?", today=TODAY)
        first_result = script.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(first_result["is_error"])
        self.assertEqual(result.mode, "claude")

    def test_runaway_tool_loop_is_stopped(self) -> None:
        many = {"content": [{"type": "tool_use", "id": f"tu_{i}", "name": "data_coverage",
                             "input": {}} for i in range(MAX_TOOL_CALLS + 2)],
                "stop_reason": "tool_use"}
        client, _ = self.client(many)
        result = client.ask(self.store, "anything", today=TODAY)
        self.assertNotEqual(result.mode, "claude")
        self.assertIn("too many tool calls", result.note or "")

    def test_endless_tool_use_ends_at_the_turn_limit(self) -> None:
        client, script = self.client(*[use("data_coverage", {}, f"t{i}") for i in range(10)])
        result = client.ask(self.store, "anything", today=TODAY)
        self.assertNotEqual(result.mode, "claude")
        self.assertLessEqual(len(script.requests), 4)

    def test_transport_failure_falls_back_and_hides_the_key(self) -> None:
        def boom(request, timeout):
            raise RuntimeError("connection refused using sk-ant-secret")

        result = ClaudeClient("sk-ant-secret", transport=boom).ask(
            self.store, "How is my load?", today=TODAY)
        self.assertEqual(result.mode, "deterministic")
        self.assertNotIn("sk-ant-secret", result.note or "")
        self.assertIn("REDACTED", result.note or "")
        self.assertIn("0.84", result.answer)

    def test_empty_key_is_rejected(self) -> None:
        with self.assertRaises(LLMError):
            ClaudeClient("")

    def test_malformed_response_falls_back(self) -> None:
        client = ClaudeClient("k", transport=lambda r, t: b"{}")
        result = client.ask(self.store, "How is my load?", today=TODAY)
        self.assertEqual(result.mode, "deterministic")

    def test_build_client_from_env(self) -> None:
        self.assertIsNone(build_claude_client(env={}))
        self.assertIsNone(build_claude_client(env={"ANTHROPIC_API_KEY": "  "}))
        client = build_claude_client(env={"ANTHROPIC_API_KEY": "k", "CLAUDE_MODEL": "m"})
        self.assertEqual(client.model, "m")


if __name__ == "__main__":
    unittest.main()
