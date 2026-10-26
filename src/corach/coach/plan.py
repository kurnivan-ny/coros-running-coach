"""Rule-based weekly training plan. Pure functions: no database, no network, no LLM.

The plan is derived only from what the athlete has actually done and how loaded
they are, using conservative, widely used rules (see README "Training plan rules"):

* Target weekly distance = mean of the last 4 complete weeks x a factor:
  1.10 normally (the "10% rule"), 1.00 when the acute:chronic ratio is in the
  caution zone, 0.75 when it is high-risk. A race inside 2 weeks tapers it.
* The long run never exceeds 110% of the longest recent run.
* One quality session and one long run per week, everything else easy.

It is general fitness guidance, not medical advice. It never invents history: with
no recent running it returns ``found=False`` rather than a made-up baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# weekday index -> run kind, by number of run days per week.
_RUN_TEMPLATES: dict[int, dict[int, str]] = {
    1: {6: "long"},
    2: {2: "easy", 6: "long"},
    3: {1: "quality", 3: "easy", 6: "long"},
    4: {1: "quality", 2: "easy", 4: "easy", 6: "long"},
    5: {1: "quality", 2: "easy", 3: "easy", 5: "easy", 6: "long"},
}
_STRENGTH_DAY_PREFERENCE = (3, 0, 5, 4)  # Thu, Mon, Sat, Fri
_KIND_WEIGHT = {"long": 0.35, "quality": 0.22, "easy": 0.21}
_FACTOR = {"optimal": 1.10, "undertraining": 1.10, "insufficient_history": 1.10,
           "caution": 1.00, "high_risk": 0.75}
_GUIDANCE = {
    "easy": "Easy aerobic run at a conversational pace. Keep it relaxed.",
    "quality": ("Warm up easy, then steady threshold-effort blocks (comfortably hard), "
                "then cool down easy."),
    "long": "Long run at an easy, even effort. Fuel and hydrate; finish feeling you could do more.",
    "strength": "Full-body strength session: legs, hips, core and upper body.",
}
MAX_RUN_DAYS = 5


@dataclass(frozen=True)
class PlanInputs:
    last_weeks_km: list[float]          # last complete weeks, oldest first (ideally 4)
    acwr_zone: str | None
    longest_recent_km: float | None
    typical_pace_s_per_km: float | None
    sessions_per_week: int = 5
    strength_per_week: int = 1
    weeks_to_race: int | None = None
    start: date | None = None


def _phase(weeks_to_race: int | None) -> str:
    if weeks_to_race is None:
        return "general"
    if weeks_to_race <= 1:
        return "race_week"
    if weeks_to_race == 2:
        return "taper"
    return "build" if weeks_to_race <= 8 else "base"


def _round_half(km: float) -> float:
    return round(km * 2) / 2


def build_plan(inp: PlanInputs) -> dict[str, Any]:
    """Seven days of sessions starting the day after ``inp.start`` (default: today)."""
    if not 1 <= inp.sessions_per_week <= 6:
        raise ValueError("sessions_per_week must be between 1 and 6")

    weeks = [w for w in inp.last_weeks_km if w is not None]
    base = sum(weeks) / len(weeks) if weeks else 0.0
    if base <= 0:
        return {"tool": "training_plan", "found": False, "reason": "no_recent_running"}

    zone = inp.acwr_zone or "insufficient_history"
    phase = _phase(inp.weeks_to_race)
    factor = _FACTOR.get(zone, 1.10)
    if phase == "race_week":
        factor = 0.5
    elif phase == "taper":
        factor = min(factor, 0.7)
    target = base * factor

    strength_days_n = max(0, min(inp.strength_per_week, inp.sessions_per_week - 1))
    run_days_n = min(MAX_RUN_DAYS, inp.sessions_per_week - strength_days_n)
    run_template = _RUN_TEMPLATES[run_days_n]

    free = [d for d in _STRENGTH_DAY_PREFERENCE if d not in run_template]
    strength_days = set(free[:strength_days_n])

    total_weight = sum(_KIND_WEIGHT[k] for k in run_template.values())
    km_by_kind = {k: target * w / total_weight for k, w in _KIND_WEIGHT.items()}
    if inp.longest_recent_km:
        km_by_kind["long"] = min(km_by_kind["long"], inp.longest_recent_km * 1.10)

    start = inp.start or date.today()
    days: list[dict[str, Any]] = []
    for offset in range(1, 8):
        day = start + timedelta(days=offset)
        wd = day.weekday()
        kind = run_template.get(wd)
        entry: dict[str, Any] = {
            "date": day.isoformat(), "weekday": WEEKDAYS[wd],
            "kind": "rest", "distance_km": None, "duration_min": None,
            "guidance": "Rest or an easy walk. Recovery is part of training.",
        }
        if kind:
            km = _round_half(km_by_kind[kind])
            entry.update(kind=kind, distance_km=km, guidance=_GUIDANCE[kind])
            if inp.typical_pace_s_per_km:
                entry["duration_min"] = round(km * inp.typical_pace_s_per_km / 60.0)
            if kind == "quality":
                entry["main_block_min"] = 20 if base < 25 else (25 if base < 40 else 30)
        elif wd in strength_days:
            entry.update(kind="strength", guidance=_GUIDANCE["strength"], duration_min=45)
        days.append(entry)

    planned_km = round(sum(d["distance_km"] or 0 for d in days), 1)
    return {
        "tool": "training_plan",
        "found": True,
        "phase": phase,
        "basis_weekly_km": round(base, 1),
        "load_zone": zone,
        "progression_factor": factor,
        "planned_weekly_km": planned_km,
        "run_sessions": run_days_n,
        "strength_sessions": len(strength_days),
        "weeks_to_race": inp.weeks_to_race,
        "days": days,
        "notes": [
            "Weekly distance is based on your last complete weeks, not a generic template.",
            "If any session feels too hard, swap it for an easy run or rest.",
            "This is general fitness guidance, not medical advice.",
        ],
    }
