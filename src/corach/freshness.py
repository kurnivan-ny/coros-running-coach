"""Pipeline freshness. Row validation lives in dbt (silver models and tests)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


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
