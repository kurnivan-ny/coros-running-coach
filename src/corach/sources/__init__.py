"""Activity sources. Every source yields raw payload dicts; parsing happens later."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable


class TransientSourceError(RuntimeError):
    """Temporary failure (network, rate limit). Only this is retried."""


class PermanentSourceError(RuntimeError):
    """Auth failure, bad request, unimplemented adapter. Never retried."""


@runtime_checkable
class Source(Protocol):
    name: str

    def fetch(self, since: str | None = None) -> Iterable[dict[str, Any]]:
        """Yield raw payloads with start_time >= ``since`` (ISO date) when given."""
        ...
