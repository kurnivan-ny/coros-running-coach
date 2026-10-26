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
    "Rephrase them in at most four sentences of plain English. "
    "Use ONLY numbers that appear in the facts. Never invent, estimate, round "
    "differently, or compute new numbers. If a value is null, say it is not "
    "available. Do not give medical advice."
)

# Matches integers and decimals, including M:SS pace values.
_NUMBER_RE = re.compile(r"\d+(?:[.:]\d+)*")

_SPORT_WORDS = {
    "strength": ("gym", "strength", "weights", "lifting", "lift"),
    "walk": ("walk", "walking"),
    "swim": ("swim", "swimming", "pool"),
    "cycling": ("cycling", "bike", "ride"),
}


@dataclass
class ChatAnswer:
    question: str
    intent: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    mode: str = "deterministic"  # deterministic | llm | llm_rejected | error
    note: str | None = None


def _sport(q: str) -> str:
    for sport, words in _SPORT_WORDS.items():
        if any(w in q for w in words):
            return sport
    return "run"


def route(question: str) -> tuple[str, dict[str, Any]]:
    """Map a question to an intent and bounded tool arguments."""
    q = question.lower().strip()
    sport = _sport(q)

    if any(w in q for w in ("plan", "schedule", "next week", "what should i", "workout for")):
        return "training_plan", {}
    if any(w in q for w in ("load", "acwr", "overtrain", "injury", "risk", "fatigue",
                            "tired", "overdoing", "too much", "ready to train")):
        return "workload", {}
    if any(w in q for w in ("longest", "furthest", "farthest", "biggest run")):
        return "longest_run", {}
    if any(w in q for w in ("breakdown", "which sports", "what sports", "types of",
                            "mix of", "cross-training", "cross training")):
        return "sport_breakdown", {"days": 28}
    if any(w in q for w in ("last run", "recent", "latest", "last few", "past runs",
                            "last workout", "last session")):
        return "recent_activities", {"limit": 5, "sport": sport if sport != "run" else None}
    if any(w in q for w in ("coverage", "how much data", "how far back", "fresh",
                            "up to date", "up-to-date", "stale")):
        return "data_coverage", {}
    if any(w in q for w in ("trend", "improving", "progress", "compared", "better",
                            "worse", "getting")):
        days = 90 if "90" in q else (7 if "7" in q or "week" in q else 28)
        return "trend", {"days": days, "sport": sport}
    if ("how many" in q or "sessions" in q) and "per week" not in q:
        days = 90 if ("90" in q or "three months" in q) else (
            28 if any(w in q for w in ("month", "28")) else 7)
        return "window", {"days": days, "sport": sport}
    if any(w in q for w in ("week", "weekly", "volume", "mileage", "per week")):
        return "weekly_volume", {"weeks": 8, "sport": sport}
    if any(w in q for w in ("month", "28", "last 30", "90", "three months")):
        days = 90 if ("90" in q or "three months" in q) else 28
        return "window", {"days": days, "sport": sport}
    if any(w in q for w in ("total", "how far", "distance", "km", "pace", "heart",
                            "hr", "average", "how many", "sessions")):
        return "window", {"days": 7, "sport": sport}

    return "data_coverage", {}


def _fmt_km(value: Any) -> str:
    return "unknown" if value is None else f"{value} km"


def _describe_coverage(facts: dict[str, Any]) -> str:
    if not facts.get("activity_count"):
        return "There are no validated activities in the database yet."
    sports = ", ".join(f"{n} {s}" for s, n in facts.get("sport_counts", {}).items())
    parts = [
        f"There are {facts['activity_count']} validated activities "
        f"from {facts['first_activity_date']} to {facts['last_activity_date']} ({sports})."
    ]
    if facts.get("is_synthetic"):
        parts.append("This is SYNTHETIC demo data, not real activity.")
    if facts.get("rejected_rows"):
        parts.append(f"{facts['rejected_rows']} rows failed validation and were quarantined.")
    if facts.get("pipeline_is_fresh"):
        parts.append(f"The pipeline last succeeded {facts['pipeline_age_hours']} hours ago.")
    elif facts.get("last_successful_run_at"):
        parts.append(
            f"The pipeline is stale: last success was {facts['pipeline_age_hours']} hours ago."
        )
    else:
        parts.append("No successful pipeline run has been recorded.")
    return " ".join(parts)


def _describe_window(facts: dict[str, Any]) -> str:
    sport = facts["sport"]
    if facts.get("sessions", 0) == 0:
        return f"No {sport} sessions recorded in the last {facts['days']} days."
    out = f"In the last {facts['days']} days you did {facts['sessions']} {sport} sessions"
    if sport == "strength":
        out += f" totalling {facts['duration_min']} minutes"
        if facts.get("sets") is not None:
            out += f" and {facts['sets']} sets"
        out += "."
    else:
        out += f" covering {_fmt_km(facts['distance_km'])}"
        out += (f" at {facts['pace_per_km']} per km." if facts.get("pace_per_km")
                else ", with pace unavailable.")
    out += (f" Duration-weighted average heart rate was {facts['avg_hr']} bpm."
            if facts.get("avg_hr") is not None
            else " No heart-rate data was recorded.")
    if not facts.get("sufficient_history"):
        out += f" Note: history is shorter than {facts['days']} days, so this window is incomplete."
    return out


def _describe_weekly(facts: dict[str, Any]) -> str:
    sport = facts["sport"]
    if facts.get("last_full_week_km") is None:
        return f"Not enough history for a full week of {sport} yet."
    if sport == "strength":
        total, this_week = (f"{facts['last_full_week_min']} minutes",
                            f"{facts['this_week_min']} minutes")
    else:
        total, this_week = (_fmt_km(facts["last_full_week_km"]), _fmt_km(facts["this_week_km"]))
    out = (f"Last full week you did {facts['last_full_week_sessions']} {sport} sessions "
           f"totalling {total}.")
    wow = facts.get("week_over_week_pct")
    if wow is None:
        out += " Week-over-week change is not available (the week before had no distance)."
    else:
        direction = "up" if wow >= 0 else "down"
        out += f" That is {direction} {abs(wow)}% versus the week before."
    out += f" This week so far: {facts['this_week_sessions']} sessions, {this_week}."
    return out


def _describe_longest(facts: dict[str, Any]) -> str:
    if not facts.get("found"):
        return "No validated runs are available yet."
    out = f"Your longest run was {_fmt_km(facts['distance_km'])} on {facts['date']}"
    out += f" at {facts['pace_per_km']} per km." if facts.get("pace_per_km") else "."
    if facts.get("avg_hr") is not None:
        out += f" Average heart rate was {facts['avg_hr']} bpm."
    return out


def _describe_recent(facts: dict[str, Any]) -> str:
    acts = facts.get("activities", [])
    if not acts:
        return "No validated activities are available yet."
    lines = []
    for a in acts:
        line = f"{a['date']}: {a['sport']}"
        if a.get("distance_km") is not None:
            line += f" {_fmt_km(a['distance_km'])}"
        elif a.get("sets") is not None:
            line += f" {a['sets']} sets"
        line += f" in {a['duration_min']} min"
        line += f", {a['avg_hr']} bpm" if a.get("avg_hr") is not None else ", no HR"
        lines.append(line)
    return f"Your {len(acts)} most recent activities: " + "; ".join(lines) + "."


def _describe_trend(facts: dict[str, Any]) -> str:
    cur, prior = facts.get("current_km"), facts.get("prior_km")
    days, sport = facts.get("days"), facts.get("sport")
    change = facts.get("change_pct")
    if change is None:
        return (f"You covered {_fmt_km(cur)} of {sport} in the last {days} days. "
                f"The preceding {days} days had none, so no change can be computed.")
    direction = "up" if change >= 0 else "down"
    out = (f"Over the last {days} days you covered {_fmt_km(cur)} of {sport}, "
           f"{direction} {abs(change)}% from {_fmt_km(prior)} in the preceding {days} days.")
    if not facts.get("sufficient_history"):
        out += " History does not fully cover both periods, so treat this as provisional."
    return out


def _describe_breakdown(facts: dict[str, Any]) -> str:
    sports = facts.get("sports", [])
    if not sports:
        return f"No activities in the last {facts['days']} days."
    parts = [f"{s['sport']}: {s['sessions']} sessions, {s['duration_min']} min" for s in sports]
    return f"Over the last {facts['days']} days: " + "; ".join(parts) + "."


_ZONE_TEXT = {
    "optimal": "in the optimal range",
    "undertraining": "below your usual load (room to build)",
    "caution": "elevated: consider easing back",
    "high_risk": "high: prioritise recovery",
    "insufficient_history": "not yet reliable (too little history)",
}


def _describe_workload(facts: dict[str, Any]) -> str:
    if not facts.get("found"):
        return "No training-load data is available yet."
    out = f"As of {facts['as_of']}, your acute load is {facts['acute_load']}"
    if facts.get("chronic_load") is not None:
        out += f" against a chronic load of {facts['chronic_load']}"
    if facts.get("acwr") is not None:
        out += f", a ratio of {facts['acwr']}"
    out += f", which is {_ZONE_TEXT.get(facts['acwr_zone'], facts['acwr_zone'])}."
    if facts.get("monotony_7d") is not None:
        out += f" Weekly monotony is {facts['monotony_7d']}."
    return out


def _describe_plan(facts: dict[str, Any]) -> str:
    if not facts.get("found"):
        return ("I can't build a plan yet: there is no recent running to base it on. "
                "Log a few weeks of runs first.")
    lines = []
    for d in facts["days"]:
        if d["kind"] == "rest":
            continue
        detail = (f"{d['distance_km']} km" if d.get("distance_km") is not None
                  else f"{d['duration_min']} min")
        lines.append(f"{d['weekday']}: {d['kind']} ({detail})")
    zone = _ZONE_TEXT.get(facts["load_zone"], facts["load_zone"])
    return (f"Based on your last weeks (about {facts['basis_weekly_km']} km per week) "
            f"and a load that is {zone}, aim for {facts['planned_weekly_km']} km "
            f"over {facts['run_sessions']} runs and {facts['strength_sessions']} "
            f"strength sessions. " + "; ".join(lines) + ".")


_DESCRIBERS = {
    "data_coverage": _describe_coverage,
    "window": _describe_window,
    "weekly_volume": _describe_weekly,
    "longest_run": _describe_longest,
    "recent_activities": _describe_recent,
    "trend": _describe_trend,
    "sport_breakdown": _describe_breakdown,
    "workload": _describe_workload,
    "training_plan": _describe_plan,
}


def _describe(intent: str, facts: dict[str, Any]) -> str:
    """Deterministic natural-language answer built only from verified facts."""
    describer = _DESCRIBERS.get(intent)
    return describer(facts) if describer else "I do not have a verified answer for that question."


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
           today: date | None = None, **tool_defaults: Any) -> ChatAnswer:
    """Answer a question from verified facts, optionally rephrased by an LLM.

    ``tool_defaults`` carries user settings (for example ``sessions_per_week``) that
    are forwarded to the training-plan tool only.
    """
    intent, kwargs = route(question)
    if intent == "training_plan":
        kwargs = {**tool_defaults, **kwargs}
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
