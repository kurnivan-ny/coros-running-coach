"""Parse raw source payloads into the canonical typed shape.

Only ``canonical_v1`` is understood. Any payload without that schema marker is
rejected rather than guessed at -- real COROS field names are UNVERIFIED, so a
tolerant parser here would silently invent data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA_V1 = "canonical_v1"


class SchemaError(ValueError):
    """Payload does not declare a schema this parser understands."""


@dataclass(frozen=True)
class CanonicalActivity:
    activity_id: str
    source: str
    start_time_utc: str
    start_date_utc: str
    distance_m: float | None
    duration_s: float | None
    avg_hr: float | None


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable hash of a payload. Key order must not change the result."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _as_float(value: Any) -> float | None:
    """Coerce to float. Absent/blank/non-numeric -> None, never 0.0."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):  # bool is an int subclass; not a measurement
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # NaN/inf are not usable measurements.
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def parse_start_time(value: Any) -> tuple[str, str]:
    """Return (ISO UTC timestamp, UTC date). Naive input is assumed UTC."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    else:
        raise ValueError(f"unparseable start_time: {value!r}")
    return dt.isoformat(), dt.date().isoformat()


def parse_activity(payload: dict[str, Any], source: str) -> CanonicalActivity:
    """Parse one payload. Raises SchemaError/ValueError; validation is quality.py's job."""
    if not isinstance(payload, dict):
        raise SchemaError(f"payload is {type(payload).__name__}, expected object")
    schema = payload.get("schema")
    if schema != SCHEMA_V1:
        raise SchemaError(f"unsupported schema {schema!r}; expected {SCHEMA_V1!r}")

    raw_id = payload.get("activity_id")
    if raw_id is None or str(raw_id).strip() == "":
        raise ValueError("missing activity_id")

    start_iso, start_date = parse_start_time(payload.get("start_time"))

    return CanonicalActivity(
        activity_id=str(raw_id).strip(),
        source=source,
        start_time_utc=start_iso,
        start_date_utc=start_date,
        # None means "not reported" and must stay None through to the database.
        distance_m=_as_float(payload.get("distance_m")),
        duration_s=_as_float(payload.get("duration_s")),
        avg_hr=_as_float(payload.get("avg_hr")),
    )
