"""Keyword router -> whitelisted tools -> verified facts -> optional LLM phrasing.

The deterministic answer is always computed first. An LLM, when configured, may
only rephrase it; if its text contains any number that is not in the verified
facts, the LLM answer is discarded and the deterministic one is returned.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..store import Store
from .llm import LLMClient, LLMError
from .tools import ToolArgumentError, call_tool

SYSTEM_PROMPT = (
    "You are a running coach. You are given VERIFIED FACTS as JSON. "
    "Rephrase them in at most three sentences of plain English. "
    "Use ONLY numbers that appear in the facts. Never invent, estimate, round "
    "differently, or compute new numbers. If a value is null, say it is not "
    "available. Do not give medical advice."
)

# Matches integers and decimals, including a leading sign and M:SS pace values.
_NUMBER_RE = re.compile(r"\d+(?:[.:]\d+)*")


@dataclass
class ChatAnswer:
    question: str
    intent: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    mode: str = "deterministic"  # deterministic | llm | llm_rejected | error
    note: str | None = None


def route(question: str) -> tuple[str, dict[str, Any]]:
    """Map a question to an intent and bounded tool arguments."""
    q = question.lower().strip()

    if any(w in q for w in ("longest", "furthest", "farthest", "biggest run")):
        return "longest_run", {}
    if any(w in q for w in ("last run", "recent", "latest", "last few", "past runs")):
        return "recent_runs", {"limit": 5}
    if any(w in q for w in ("coverage", "how much data", "how far back", "fresh",
                            "up to date", "up-to-date", "stale")):
        return "data_coverage", {}
    if any(w in q for w in ("trend", "improving", "progress", "compared", "better",
                            "worse", "getting")):
        days = 90 if "90" in q else (7 if "7" in q or "week" in q else 28)
        return "trend", {"days": days}
    if any(w in q for w in ("week", "weekly", "volume", "mileage", "per week")):
        return "weekly_volume", {"weeks": 8}
    if any(w in q for w in ("month", "28", "last 30", "90", "three months")):
        days = 90 if ("90" in q or "three months" in q) else 28
        return "window", {"days": days}
    if any(w in q for w in ("total", "how far", "distance", "km", "pace", "heart",
                            "hr", "average")):
        return "window", {"days": 7}

    return "data_coverage", {}


def _fmt_km(value: Any) -> str:
    return "unknown" if value is None else f"{value} km"


def _describe(intent: str, facts: dict[str, Any]) -> str:
    """Deterministic natural-language answer built only from verified facts."""
    if intent == "data_coverage":
        if not facts.get("activity_count"):
            return "There are no validated activities in the database yet."
        parts = [
            f"There are {facts['activity_count']} validated runs "
            f"from {facts['first_activity_date']} to {facts['last_activity_date']}."
        ]
        if facts.get("is_synthetic"):
            parts.append("This is SYNTHETIC demo data, not real activity.")
        if facts.get("pipeline_is_fresh"):
            parts.append(f"The pipeline last succeeded {facts['pipeline_age_hours']} hours ago.")
        elif facts.get("last_successful_run_at"):
            parts.append(
                f"The pipeline is stale: last success was {facts['pipeline_age_hours']} hours ago."
            )
        else:
            parts.append("No successful pipeline run has been recorded.")
        return " ".join(parts)

    if intent == "window":
        if facts.get("runs", 0) == 0:
            return f"No runs recorded in the last {facts['days']} days."
        out = (
            f"In the last {facts['days']} days you ran {facts['runs']} times "
            f"for {_fmt_km(facts['distance_km'])}"
        )
        out += (f" at {facts['pace_per_km']} per km." if facts.get("pace_per_km")
                else ", with pace unavailable.")
        out += (f" Duration-weighted average heart rate was {facts['avg_hr']} bpm."
                if facts.get("avg_hr") is not None
                else " No heart-rate data was recorded for those runs.")
        if not facts.get("sufficient_history"):
            out += (f" Note: history is shorter than {facts['days']} days, "
                    "so this window is incomplete.")
        return out

    if intent == "weekly_volume":
        latest = facts.get("latest_week_km")
        out = f"Your latest ISO week totals {_fmt_km(latest)}."
        wow = facts.get("week_over_week_pct")
        if wow is None:
            out += " Week-over-week change is not available (the prior week had no runs)."
        else:
            direction = "up" if wow >= 0 else "down"
            out += f" That is {direction} {abs(wow)}% versus the previous week."
        return out

    if intent == "longest_run":
        if not facts.get("found"):
            return "No validated runs are available yet."
        out = (f"Your longest run was {_fmt_km(facts['distance_km'])} "
               f"on {facts['date']}")
        out += f" at {facts['pace_per_km']} per km." if facts.get("pace_per_km") else "."
        if facts.get("avg_hr") is not None:
            out += f" Average heart rate was {facts['avg_hr']} bpm."
        return out

    if intent == "recent_runs":
        runs = facts.get("runs", [])
        if not runs:
            return "No validated runs are available yet."
        lines = [
            f"{r['date']}: {_fmt_km(r['distance_km'])}"
            + (f" at {r['pace_per_km']}/km" if r.get("pace_per_km") else "")
            + (f", {r['avg_hr']} bpm" if r.get("avg_hr") is not None else ", no HR")
            for r in runs
        ]
        return f"Your {len(runs)} most recent runs: " + "; ".join(lines) + "."

    if intent == "trend":
        cur, prior = facts.get("current_km"), facts.get("prior_km")
        days = facts.get("days")
        change = facts.get("change_pct")
        if change is None:
            return (f"You covered {_fmt_km(cur)} in the last {days} days. "
                    f"The preceding {days} days had no runs, so no change can be computed.")
        direction = "up" if change >= 0 else "down"
        out = (f"Over the last {days} days you covered {_fmt_km(cur)}, "
               f"{direction} {abs(change)}% from {_fmt_km(prior)} in the preceding "
               f"{days} days.")
        if not facts.get("sufficient_history"):
            out += " History does not fully cover both periods, so treat this as provisional."
        return out

    return "I do not have a verified answer for that question."


def extract_numbers(text: str) -> set[str]:
    """All numeric tokens in a string, normalised for comparison."""
    out: set[str] = set()
    for match in _NUMBER_RE.findall(text):
        out.add(match)
        if "." in match:  # 10.0 and 10 are the same figure
            out.add(match.rstrip("0").rstrip("."))
    return {t for t in out if t}


def _fact_numbers(facts: Any) -> set[str]:
    """Every number appearing in the verified facts, including nested values."""
    out: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, int | float):
            text = f"{node}"
            out.add(text)
            out.add(text.rstrip("0").rstrip(".") if "." in text else text)
            out.add(str(abs(node)))
            if isinstance(node, float) and node.is_integer():
                out.add(str(int(node)))
        elif isinstance(node, str):
            out.update(extract_numbers(node))

    walk(facts)
    return {t for t in out if t}


def guard_llm_answer(text: str, facts: Any) -> tuple[bool, str | None]:
    """Reject an LLM answer containing any number absent from the facts."""
    allowed = _fact_numbers(facts)
    for token in extract_numbers(text):
        if token in allowed:
            continue
        if token.rstrip("0").rstrip(".") in allowed:
            continue
        return False, f"unverified number {token!r} in model output"
    return True, None


def answer(store: Store, question: str, client: LLMClient | None = None,
           today: date | None = None) -> ChatAnswer:
    """Answer a question from verified facts, optionally rephrased by an LLM."""
    intent, kwargs = route(question)
    try:
        facts = call_tool(store, intent, today=today, **kwargs)
    except ToolArgumentError as exc:
        return ChatAnswer(question=question, intent=intent, answer=f"Invalid request: {exc}",
                          mode="error", note=str(exc))

    deterministic = _describe(intent, facts)
    result = ChatAnswer(question=question, intent=intent, facts=[facts],
                        answer=deterministic, mode="deterministic")

    if client is None:
        return result

    try:
        text = client.complete(
            SYSTEM_PROMPT,
            f"Question: {question}\n\nVERIFIED FACTS:\n{json.dumps(facts, default=str)}",
        ).strip()
    except LLMError as exc:
        result.note = f"LLM unavailable, used deterministic answer ({exc})"
        return result

    if not text:
        result.note = "LLM returned empty text, used deterministic answer"
        return result

    ok, reason = guard_llm_answer(text, facts)
    if not ok:
        result.mode = "llm_rejected"
        result.note = f"LLM answer rejected: {reason}"
        return result

    result.answer = text
    result.mode = "llm"
    return result
