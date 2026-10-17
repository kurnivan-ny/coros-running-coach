"""COROS MCP adapter -- STUB. Not implemented, and deliberately fails loudly.

STATUS as of 2026-10-17: UNVERIFIED. No COROS MCP server is configured in this
environment, so its tool list, auth flow and response fields have not been
observed. Writing a parser now would mean inventing field names.

To implement, first record the real answers to:
  1. Tool names exposed by the server and their arguments.
  2. Auth flow (token? OAuth? refresh/expiry?).
  3. Exact response field names and units (metres vs km, seconds vs minutes).
  4. Pagination and whether an incremental "since" filter is supported.
  5. Timezone semantics of the start-time field.

Then map that response to ``transform.SCHEMA_V1`` and add replay tests built from
a captured real response. Until then this raises.

Fallback (per project decision): manual COROS FIT/CSV export through a file
parser feeding the same bronze layer.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import PermanentSourceError


class CorosMCPSource:
    name = "coros_mcp"

    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.url = url
        self.token = token  # never logged

    def fetch(self, since: str | None = None) -> Iterable[dict[str, Any]]:
        raise PermanentSourceError(
            "COROS MCP adapter is not implemented. Its tools, auth flow and response "
            "fields are UNVERIFIED (no MCP server configured), and guessing field names "
            "would fabricate data. Use CORACH_SOURCE=synthetic, or implement this "
            "adapter after capturing a real MCP response. See module docstring."
        )
