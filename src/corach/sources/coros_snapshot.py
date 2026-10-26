"""COROS snapshot source: parses saved COROS MCP ``querySportRecords`` output.

The COROS MCP server only runs inside a Claude session, so scheduled jobs cannot
call it. Instead a session saves the tool's text output to ``data/coros_raw/``
and this source turns it into raw bronze payloads. The files hold private
athlete data and are gitignored.

Format (verified against real output, 443 records, 2025-03 to 2026-10)::

    1. Outdoor Run — 2026-10-17
       Location: Central Jakarta Run
       Start Coordinates: -6.216000, 106.797997          (optional)
       Time Window: startTimestamp=1792195056 | endTimestamp=1792197887
       Duration: 47:10 | Distance: 6.10 km               (or "351 m", or "Sets: 5")
       Average Pace: 7:44 /km | Avg HR: 162 bpm | Calories: 678 kcal
       LabelId: 481089519385346349 | SportType: 100

The date in the heading is the athlete's local calendar date; the timestamps are
Unix seconds (UTC). Fields that are absent stay ``None`` -- they are never zero.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import PermanentSourceError

SCHEMA = "coros_record_v1"
DEFAULT_DIR = "data/coros_raw"

_HEADER = re.compile(r"^\s*\d+\.\s+(?P<sport>.+?)\s+[—-]\s+(?P<date>\d{4}-\d{2}-\d{2})\s*$",
                     re.MULTILINE)
_TS = re.compile(r"startTimestamp=(\d+)\s*\|\s*endTimestamp=(\d+)")
_COORDS = re.compile(r"Start Coordinates:\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)")
_LOCATION = re.compile(r"Location:\s*(.+)")
_DURATION = re.compile(r"Duration:\s*(\d+(?::\d{1,2}){1,2})")
_DISTANCE = re.compile(r"Distance:\s*([\d.]+)\s*(km|m)\b")
_SETS = re.compile(r"Sets:\s*(\d+)")
_PACE = re.compile(r"Average Pace:\s*(\d+):(\d{2})\s*/km")
_HR = re.compile(r"Avg HR:\s*(\d+)\s*bpm")
_CAL = re.compile(r"Calories:\s*(\d+)\s*kcal")
_IDS = re.compile(r"LabelId:\s*(\d+)\s*\|\s*SportType:\s*(\d+)")


def _duration_s(text: str) -> int:
    parts = [int(p) for p in text.split(":")]
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def _first(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    return pattern.search(text)


def parse_block(sport_name: str, activity_date: str, block: str) -> dict[str, Any]:
    """Parse one record. Missing fields become ``None``; nothing is guessed."""
    ts = _first(_TS, block)
    ids = _first(_IDS, block)
    dur = _first(_DURATION, block)
    dist = _first(_DISTANCE, block)
    sets = _first(_SETS, block)
    pace = _first(_PACE, block)
    hr = _first(_HR, block)
    cal = _first(_CAL, block)
    coords = _first(_COORDS, block)
    loc = _first(_LOCATION, block)

    distance_m: float | None = None
    if dist:
        value = float(dist.group(1))
        distance_m = value * 1000.0 if dist.group(2) == "km" else value

    return {
        "schema": SCHEMA,
        "activity_id": ids.group(1) if ids else None,
        "sport_type": int(ids.group(2)) if ids else None,
        "sport_name": sport_name,
        "activity_date": activity_date,
        "start_ts": int(ts.group(1)) if ts else None,
        "end_ts": int(ts.group(2)) if ts else None,
        "location": loc.group(1).strip() if loc else None,
        "start_lat": float(coords.group(1)) if coords else None,
        "start_lon": float(coords.group(2)) if coords else None,
        "duration_s": _duration_s(dur.group(1)) if dur else None,
        "distance_m": distance_m,
        "sets": int(sets.group(1)) if sets else None,
        "reported_pace_s_per_km": (int(pace.group(1)) * 60 + int(pace.group(2))) if pace else None,
        "avg_hr": int(hr.group(1)) if hr else None,
        "calories": int(cal.group(1)) if cal else None,
    }


def parse_records(text: str) -> list[dict[str, Any]]:
    """Parse the full text of one ``querySportRecords`` response."""
    text = text.strip()
    if text.startswith('"'):  # the MCP client saves the response as a JSON string
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, str):
            text = decoded

    headers = list(_HEADER.finditer(text))
    out: list[dict[str, Any]] = []
    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        out.append(parse_block(m.group("sport"), m.group("date"), text[m.end():end]))
    return out


class CorosSnapshotSource:
    """Reads ``sport_records_*.txt`` files from a directory."""

    name = "coros_snapshot"

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self.directory = Path(directory or os.environ.get("CORACH_SNAPSHOT_DIR") or DEFAULT_DIR)

    def fetch(self, since: str | None = None) -> Iterable[dict[str, Any]]:
        files = sorted(self.directory.glob("sport_records_*.txt"))
        if not files:
            raise PermanentSourceError(
                f"no sport_records_*.txt files in {self.directory}. Ask Claude to refresh the "
                "COROS snapshot (see docs/DATA_IMPORT.md)."
            )
        by_id: dict[str, dict[str, Any]] = {}
        for path in files:  # later files win, so a re-pull supersedes an older one
            for rec in parse_records(path.read_text(encoding="utf-8")):
                key = rec["activity_id"] or f"unknown-{path.name}-{rec['start_ts']}"
                by_id[key] = rec
        rows = list(by_id.values())
        if since:
            rows = [r for r in rows if r["activity_date"] >= since[:10]]
        return sorted(rows, key=lambda r: (r["activity_date"], r["start_ts"] or 0))
