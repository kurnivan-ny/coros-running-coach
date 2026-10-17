"""Row validation and pipeline freshness checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .transform import CanonicalActivity

# Plausibility bounds. Outside these a row is quarantined, not silently clamped.
MAX_DISTANCE_M = 500_000.0   # 500 km
MAX_DURATION_S = 172_800.0   # 48 h
MIN_HR = 20.0
MAX_HR = 260.0


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None


def validate(activity: CanonicalActivity) -> ValidationResult:
    """Check one parsed activity. A missing avg_hr is valid; an implausible one is not."""
    a = activity

    if a.distance_m is None:
        return ValidationResult(False, "distance_missing")
    if a.distance_m <= 0:
        return ValidationResult(False, "distance_non_positive")
    if a.distance_m > MAX_DISTANCE_M:
        return ValidationResult(False, "distance_out_of_range")

    if a.duration_s is None:
        return ValidationResult(False, "duration_missing")
    if a.duration_s <= 0:
        return ValidationResult(False, "duration_non_positive")
    if a.duration_s > MAX_DURATION_S:
        return ValidationResult(False, "duration_out_of_range")

    # avg_hr is optional. Present-but-implausible is an error; absent is fine.
    if a.avg_hr is not None and not (MIN_HR <= a.avg_hr <= MAX_HR):
        return ValidationResult(False, "hr_out_of_range")

    try:
        datetime.fromisoformat(a.start_time_utc)
    except ValueError:
        return ValidationResult(False, "bad_start_time")

    return ValidationResult(True)


@dataclass(frozen=True)
class Freshness:
    last_success_at: str | None
    age_hours: float | None
    is_fresh: bool
    threshold_hours: float


def freshness(last_success_at: str | None, threshold_hours: float = 36.0,
              now: datetime | None = None) -> Freshness:
    """Age of the last successful run. Unknown age is never reported as fresh."""
    if not last_success_at:
        return Freshness(None, None, False, threshold_hours)
    try:
        ts = datetime.fromisoformat(last_success_at)
    except ValueError:
        return Freshness(last_success_at, None, False, threshold_hours)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    age = (now - ts) / timedelta(hours=1)
    return Freshness(last_success_at, age, age <= threshold_hours, threshold_hours)
