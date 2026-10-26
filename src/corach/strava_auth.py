"""One-time Strava authorisation: get a refresh token that can read your activities.

    python -m corach.strava_auth                 # prints the URL to open in a browser
    python -m corach.strava_auth "<redirect URL or code>"   # exchanges it, saves to .env

Strava's settings page shows a refresh token with ``read`` scope only, which cannot
list activities. Authorising through the URL below grants ``activity:read_all``.
The browser ends on a page that fails to load (``http://localhost/?code=...``); that is
expected. Paste that address bar URL (or just the code) as the argument. The code is
single-use and short-lived. Nothing secret is printed, and the new token is written to
``.env`` only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Sequence

from .envfile import load_env
from .sources.strava import TOKEN_URL, https_open, persist_refresh_token

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
REDIRECT_URI = "http://localhost"
SCOPE = "read,activity:read_all"


def authorize_url(client_id: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "approval_prompt": "force", "scope": SCOPE,
    })
    return f"{AUTHORIZE_URL}?{query}"


def extract_code(value: str) -> str:
    """Accept a bare code or the whole redirected URL."""
    value = value.strip().strip("'\"")
    if "code=" in value:
        parsed = urllib.parse.urlparse(value)
        codes = urllib.parse.parse_qs(parsed.query).get("code")
        if codes:
            return codes[0]
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    load_env()
    client_id = os.environ.get("STRAVA_CLIENT_ID", "")
    secret = os.environ.get("STRAVA_CLIENT_SECRET", "")
    if not client_id or not secret:
        print("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first.")
        return 1
    if not args:
        print("Open this URL, click Authorize, then pass the final address-bar URL back:\n")
        print(authorize_url(client_id))
        return 0

    code = extract_code(args[0])
    body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": secret, "code": code,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    try:
        request = urllib.request.Request(TOKEN_URL, data=body, method="POST")
        data = json.loads(https_open(request, 30.0))
    except Exception as exc:  # noqa: BLE001
        text = str(exc).replace(secret, "***").replace(code, "***")
        print(f"Authorisation failed ({type(exc).__name__}: {text}). Codes are single-use; "
              "generate a fresh one.")
        return 1

    token = data.get("refresh_token")
    if not token:
        print("Strava did not return a refresh token.")
        return 1
    if not persist_refresh_token(token):
        print("No .env with a STRAVA_REFRESH_TOKEN line to update; add the token by hand.")
        return 1
    granted = (data.get("scope") or "").strip()
    print(f"Saved a new refresh token to .env. Scopes granted: {granted or 'unknown'}")
    if "activity" not in granted:
        print("Warning: activity access was not granted; tick the activity checkbox on the "
              "Strava authorisation page and try again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
