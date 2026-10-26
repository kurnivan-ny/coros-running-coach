"""Strava source: pulls activities from the Strava API into ``coros_record_v1`` payloads.

Unlike the COROS snapshot, this works unattended (GitHub Actions can run it), because
Strava is an ordinary OAuth web API. COROS can sync to Strava, so most activities
arrive under both sources; silver keeps one copy (COROS wins), see the dbt models.

Configuration (never logged, redacted from every error)::

    STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN

The refresh token must have been authorised with the ``activity:read_all`` scope (or
``activity:read`` for public activities only). The token shown on Strava's API settings
page has only ``read`` scope and is not enough.

Strava's summary endpoint has no calories, no strength sets and no COROS-specific
metrics; those fields stay ``None`` here, never zero. Check Strava's current API
agreement before sending this data to an LLM.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from . import PermanentSourceError, TransientSourceError
from .coros_snapshot import SCHEMA

TOKEN_URL = "https://www.strava.com/oauth/token"  # nosec B105 - public endpoint, not a secret
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
PAGE_SIZE = 200
MAX_PAGES = 50  # 10,000 activities; a hard stop against a runaway loop
DEFAULT_TIMEOUT = 30.0

# Strava ``sport_type`` -> COROS sport code (so silver groups it the same way).
_SPORT_CODES = {
    "Run": 100, "TrailRun": 102, "VirtualRun": 101,
    "Walk": 900, "Hike": 104,
    "Ride": 200, "MountainBikeRide": 204, "GravelRide": 203, "EBikeRide": 202,
    "VirtualRide": 201,
    "Swim": 300,
    "WeightTraining": 402, "Crossfit": 402, "HighIntensityIntervalTraining": 402,
    "Yoga": 904, "Pilates": 905,
}
# No meaningful distance: a 0 here means "not applicable", so it becomes None.
_NO_DISTANCE = {"WeightTraining", "Crossfit", "HighIntensityIntervalTraining", "Yoga",
                "Pilates", "Workout"}

Transport = Callable[[urllib.request.Request, float], bytes]


def _redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


def https_open(request: urllib.request.Request, timeout: float) -> bytes:
    if request.type != "https":  # also blocks file:// and plaintext http
        raise PermanentSourceError(f"refusing to open scheme {request.type!r}")
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler)
    with opener.open(request, timeout=timeout) as resp:
        return resp.read()


def to_record(act: dict[str, Any]) -> dict[str, Any]:
    """Map one Strava activity (summary form) to a ``coros_record_v1`` payload."""
    sport = str(act.get("sport_type") or act.get("type") or "")
    trainer = bool(act.get("trainer"))
    code = _SPORT_CODES.get(sport, 9999)
    if sport == "Run" and trainer:
        code = 101  # treadmill
    if sport == "Ride" and trainer:
        code = 201

    start = datetime.fromisoformat(str(act["start_date"]).replace("Z", "+00:00"))
    start_ts = int(start.astimezone(UTC).timestamp())
    elapsed = act.get("elapsed_time")
    latlng = act.get("start_latlng") or [None, None]

    distance = act.get("distance")
    distance_m = None if sport in _NO_DISTANCE or distance is None else float(distance)
    hr = act.get("average_heartrate") if act.get("has_heartrate", True) else None

    return {
        "schema": SCHEMA,
        "activity_id": f"strava-{act['id']}",
        "sport_type": code,
        "sport_name": sport or None,
        "activity_date": str(act.get("start_date_local") or act["start_date"])[:10],
        "start_ts": start_ts,
        "end_ts": start_ts + int(elapsed) if elapsed is not None else None,
        "location": act.get("name"),
        "start_lat": latlng[0] if len(latlng) == 2 else None,
        "start_lon": latlng[1] if len(latlng) == 2 else None,
        "duration_s": act.get("moving_time"),
        "distance_m": distance_m,
        "sets": None,
        "reported_pace_s_per_km": None,
        "avg_hr": None if hr is None else round(float(hr)),
        "calories": None,
    }


class StravaSource:
    """Reads the athlete's activity list. ``transport`` is injectable for tests."""

    name = "strava"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 refresh_token: str | None = None, transport: Transport | None = None,
                 on_new_refresh_token: Callable[[str], None] | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.client_id = client_id or os.environ.get("STRAVA_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("STRAVA_CLIENT_SECRET", "")
        self.refresh_token = refresh_token or os.environ.get("STRAVA_REFRESH_TOKEN", "")
        self._transport = transport or https_open
        self._on_new_refresh_token = on_new_refresh_token
        self.timeout = timeout
        self._access_token: str | None = None

    # -- plumbing ----------------------------------------------------------
    def _secrets(self) -> tuple[str, ...]:
        return (self.client_secret, self.refresh_token, self._access_token or "")

    def _call(self, request: urllib.request.Request) -> Any:
        try:
            raw = self._transport(request, self.timeout)
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            msg = _redact(f"Strava HTTP {exc.code}", self._secrets())
            if exc.code == 429 or exc.code >= 500:
                raise TransientSourceError(f"{msg} (rate limit or server error)") from None
            if exc.code in (401, 403):
                raise PermanentSourceError(
                    f"{msg}: token rejected or missing scope. Re-authorise with scope "
                    "activity:read_all (the token on Strava's settings page is read-only)."
                ) from None
            raise PermanentSourceError(msg) from None
        except (TransientSourceError, PermanentSourceError):
            raise
        except Exception as exc:  # noqa: BLE001 - network errors are retried, redacted
            raise TransientSourceError(
                _redact(f"{type(exc).__name__}: {exc}", self._secrets())) from None

    def _authenticate(self) -> None:
        missing = [n for n, v in (("STRAVA_CLIENT_ID", self.client_id),
                                  ("STRAVA_CLIENT_SECRET", self.client_secret),
                                  ("STRAVA_REFRESH_TOKEN", self.refresh_token)) if not v]
        if missing:
            raise PermanentSourceError(f"missing Strava configuration: {', '.join(missing)}")
        body = urllib.parse.urlencode({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "refresh_token", "refresh_token": self.refresh_token,
        }).encode("utf-8")
        data = self._call(urllib.request.Request(TOKEN_URL, data=body, method="POST"))
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise PermanentSourceError("Strava token response had no access_token")
        self._access_token = str(token)
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = str(new_refresh)
            if self._on_new_refresh_token:
                self._on_new_refresh_token(self.refresh_token)

    # -- Source protocol ---------------------------------------------------
    def fetch(self, since: str | None = None) -> Iterable[dict[str, Any]]:
        self._authenticate()
        after = 0
        if since:
            after = int(datetime.fromisoformat(since[:10]).replace(tzinfo=UTC).timestamp())
        out: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            query = urllib.parse.urlencode(
                {"per_page": PAGE_SIZE, "page": page, **({"after": after} if after else {})})
            request = urllib.request.Request(
                f"{ACTIVITIES_URL}?{query}",
                headers={"Authorization": f"Bearer {self._access_token}"})
            batch = self._call(request)
            if not isinstance(batch, list):
                raise PermanentSourceError("unexpected Strava activities response")
            out.extend(to_record(a) for a in batch)
            if len(batch) < PAGE_SIZE:
                break
        return sorted(out, key=lambda r: (r["activity_date"], r["start_ts"] or 0))


def persist_refresh_token(token: str, env_path: str = ".env") -> bool:
    """Write a refresh token to a local ``.env``; False if there is no such file.

    Accepts ``KEY=value``, ``KEY = value`` and ``export KEY=value``, keeping the
    author's spacing, and appends the line if the file does not have one yet.
    """
    if not os.path.isfile(env_path):
        return False
    lines = open(env_path, encoding="utf-8").read().splitlines()
    pattern = re.compile(r"^(\s*(?:export\s+)?STRAVA_REFRESH_TOKEN\s*=\s*).*$")
    for i, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            lines[i] = match.group(1) + token
            break
    else:
        lines.append(f"STRAVA_REFRESH_TOKEN={token}")
    open(env_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return True
