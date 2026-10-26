"""Optional Claude API coach with secure tool calling (standard library only).

Claude is given the same whitelisted, read-only tools the rule-based coach uses
and decides which to call. It cannot run SQL, cannot name a table, and cannot
reach anything outside :data:`corach.coach.tools.TOOLS`:

* the tool name must be in the whitelist and every input key must be declared in
  the tool's schema (undeclared keys such as ``today`` or SQL are refused);
* each argument is validated and bounded again by the tool itself, so a hostile
  or confused model gets an error result, never an unbounded scan;
* the final text goes through the same number guard as every other LLM answer:
  any figure not present in the tool results discards it in favour of the
  deterministic answer.

Only aggregates leave the machine. The endpoint is a fixed https constant, so a
configuration value cannot redirect the key elsewhere, and the key is redacted
from every error. Cost: Claude Pro does **not** include API credit; the API is
billed per token. The dashboard works fully without it.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from datetime import date
from typing import Any

from ..store import Store
from .chat import SYSTEM_PROMPT, ChatAnswer, answer, guard_llm_answer
from .llm import LLMClient, LLMError, _redact
from .tools import SPORT_GROUPS, TOOLS, ToolArgumentError, call_tool

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-5-5"  # the cheapest current model; override with CLAUDE_MODEL
DEFAULT_TIMEOUT = 30.0
MAX_TURNS = 4
MAX_TOOL_CALLS = 6

_SPORT = {"type": "string", "enum": list(SPORT_GROUPS), "description": "Sport group."}
_DAYS = {"type": "integer", "enum": [7, 28, 90], "description": "Trailing window in days."}

# name -> (description, input properties). Must cover exactly the tools in TOOLS.
TOOL_SPECS: dict[str, tuple[str, dict[str, Any]]] = {
    "data_coverage": ("How much history exists, which sports, and whether the pipeline is "
                      "fresh. Call first if unsure what data there is.", {}),
    "window": ("Totals (sessions, distance, time, pace, heart rate, load) for one sport over "
               "the last 7, 28 or 90 days.", {"days": _DAYS, "sport": _SPORT}),
    "weekly_volume": ("Per-week totals for one sport, plus the last full week and "
                      "week-over-week change.",
                      {"weeks": {"type": "integer", "minimum": 1, "maximum": 52}, "sport": _SPORT}),
    "longest_run": ("The single longest run on record.", {}),
    "recent_activities": ("Most recent activities, newest first, optionally one sport.",
                          {"limit": {"type": "integer", "minimum": 1, "maximum": 50},
                           "sport": _SPORT}),
    "trend": ("Compare the last 7, 28 or 90 days with the window before it.",
              {"days": _DAYS, "sport": _SPORT}),
    "sport_breakdown": ("Sessions and time per sport over the last 7, 28 or 90 days.",
                        {"days": _DAYS}),
    "workload": ("Current acute and chronic training load, acute:chronic ratio and zone, "
                 "monotony and strain.", {}),
    "training_plan": ("Rule-based plan for the next seven days from recent volume and load.",
                      {"sessions_per_week": {"type": "integer", "minimum": 1, "maximum": 6},
                       "strength_per_week": {"type": "integer", "minimum": 0, "maximum": 2},
                       "weeks_to_race": {"type": "integer", "minimum": 0, "maximum": 52}}),
}

CLAUDE_SYSTEM_PROMPT = (
    SYSTEM_PROMPT.replace("You are given VERIFIED FACTS as JSON. ",
                          "Use the provided tools to fetch VERIFIED FACTS; call a tool for "
                          "every number you state. ")
    + " If the tools cannot answer the question, say so instead of guessing."
)


def tool_definitions() -> list[dict[str, Any]]:
    """Anthropic tool schemas for the whitelisted tools."""
    return [
        {"name": name, "description": desc,
         "input_schema": {"type": "object", "properties": props, "additionalProperties": False}}
        for name, (desc, props) in TOOL_SPECS.items()
    ]


def run_tool(store: Store, name: str, args: Any, today: date | None,
             defaults: dict[str, Any]) -> dict[str, Any]:
    """Execute one model-requested tool call safely. Raises ToolArgumentError on misuse."""
    if name not in TOOLS or name not in TOOL_SPECS:
        raise ToolArgumentError(f"unknown tool {name!r}")
    if not isinstance(args, dict):
        raise ToolArgumentError("tool input must be an object")
    allowed = TOOL_SPECS[name][1]
    extra = set(args) - set(allowed)
    if extra:
        raise ToolArgumentError(f"undeclared arguments: {sorted(extra)}")
    merged = {**(defaults if name == "training_plan" else {}), **args}
    return call_tool(store, name, today=today, **merged)


class ClaudeClient:
    """Minimal Messages API client. ``transport`` is injectable for tests."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 timeout: float = DEFAULT_TIMEOUT,
                 transport: Callable[[urllib.request.Request, float], bytes] | None = None) -> None:
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is empty")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._transport = transport or LLMClient._urlopen

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            API_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"content-type": "application/json", "x-api-key": self.api_key,
                     "anthropic-version": ANTHROPIC_VERSION},
        )
        try:
            raw = self._transport(request, self.timeout)
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - re-raised redacted
            raise LLMError(_redact(f"{type(exc).__name__}: {exc}", self.api_key)) from None
        if not isinstance(data, dict) or "content" not in data:
            raise LLMError("unexpected response shape from the Claude API")
        return data

    def ask(self, store: Store, question: str, today: date | None = None,
            **defaults: Any) -> ChatAnswer:
        """Answer via tool calling; fall back to the rule-based answer on any problem."""
        fallback = answer(store, question, client=None, today=today, **defaults)
        facts: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        calls = 0
        text = ""
        try:
            for _ in range(MAX_TURNS):
                resp = self._post({
                    "model": self.model, "max_tokens": 600, "system": CLAUDE_SYSTEM_PROMPT,
                    "tools": tool_definitions(), "messages": messages,
                })
                blocks = resp["content"]
                messages.append({"role": "assistant", "content": blocks})
                uses = [b for b in blocks if b.get("type") == "tool_use"]
                if not uses:
                    text = "".join(b.get("text", "") for b in blocks
                                   if b.get("type") == "text").strip()
                    break
                results = []
                for use in uses:
                    calls += 1
                    if calls > MAX_TOOL_CALLS:
                        raise LLMError("too many tool calls")
                    try:
                        out = run_tool(store, str(use.get("name")), use.get("input"), today,
                                       defaults)
                        facts.append(out)
                        results.append({"type": "tool_result", "tool_use_id": use["id"],
                                        "content": json.dumps(out, default=str)})
                    except ToolArgumentError as exc:
                        results.append({"type": "tool_result", "tool_use_id": use["id"],
                                        "content": str(exc), "is_error": True})
                messages.append({"role": "user", "content": results})
        except LLMError as exc:
            fallback.note = f"Claude unavailable, used rule-based answer ({exc})"
            return fallback

        if not text:
            fallback.note = "Claude returned no answer, used rule-based answer"
            return fallback
        if not facts:
            fallback.mode = "llm_rejected"
            fallback.note = "Claude answered without consulting the data; discarded"
            return fallback
        ok, reason = guard_llm_answer(text, facts)
        if not ok:
            fallback.mode = "llm_rejected"
            fallback.note = f"Claude answer rejected: {reason}"
            return fallback
        return ChatAnswer(question=question, intent="claude_tools", facts=facts, answer=text,
                          mode="claude", note=f"{calls} tool call(s)")


def build_claude_client(env: dict[str, str] | None = None,
                        transport: Callable[..., bytes] | None = None) -> ClaudeClient | None:
    """Client from ANTHROPIC_API_KEY (and optional CLAUDE_MODEL), or None when unset."""
    env = env if env is not None else dict(os.environ)
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return None
    return ClaudeClient(key, (env.get("CLAUDE_MODEL") or DEFAULT_MODEL).strip(),
                        transport=transport)
