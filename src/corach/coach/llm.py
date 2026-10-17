"""Minimal OpenAI-compatible chat client built on the standard library.

Works with any provider exposing /chat/completions (Groq, Cloudflare Workers AI,
OpenRouter, Gemini's OpenAI-compatible endpoint, local Ollama).

Two hard rules:
  * https is required unless the host is localhost, so a key never crosses a
    plaintext link.
  * The API key is never included in an exception message. Errors are rewritten
    through :func:`_redact` before they propagate.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")
DEFAULT_TIMEOUT = 20.0


class LLMError(RuntimeError):
    """LLM call failed. Message is always redacted."""


class LLMNotConfigured(LLMError):
    """No LLM configured; the caller should fall back to deterministic phrasing."""


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LLMConfig | None:
        """Build from LLM_BASE_URL / LLM_MODEL / LLM_API_KEY. None when unset."""
        env = env if env is not None else dict(os.environ)
        base = (env.get("LLM_BASE_URL") or "").strip()
        model = (env.get("LLM_MODEL") or "").strip()
        key = (env.get("LLM_API_KEY") or "").strip()
        if not base or not model:
            return None
        try:
            timeout = float(env.get("LLM_TIMEOUT", DEFAULT_TIMEOUT))
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        return cls(base_url=base, model=model, api_key=key, timeout=timeout)


def _is_local(host: str) -> bool:
    return host.split(":")[0].lower() in LOCAL_HOSTS or host.lower() in LOCAL_HOSTS


def validate_base_url(base_url: str) -> str:
    """Require https unless the host is loopback. Returns the normalised URL."""
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise LLMError(f"unsupported URL scheme {parsed.scheme!r}; use https")
    host = parsed.netloc
    if not host:
        raise LLMError("LLM_BASE_URL has no host")
    if parsed.scheme == "http" and not _is_local(host):
        raise LLMError("http is only allowed for localhost; use https")
    return base_url.rstrip("/")


def _redact(text: str, secret: str) -> str:
    """Remove the API key from any string headed for a log or exception."""
    if secret and secret in text:
        text = text.replace(secret, "***REDACTED***")
    return text


class LLMClient:
    """Chat client. ``transport`` is injectable so tests never touch the network."""

    def __init__(self, config: LLMConfig,
                 transport: Callable[[urllib.request.Request, float], bytes] | None = None) -> None:
        self.config = config
        self.base_url = validate_base_url(config.base_url)
        self._transport = transport or self._urlopen

    @staticmethod
    def _urlopen(request: urllib.request.Request, timeout: float) -> bytes:
        # Re-check the scheme here, not just at config time: urlopen would
        # otherwise honour file:// and other local schemes.
        if request.type not in ("http", "https"):
            raise LLMError(f"refusing to open scheme {request.type!r}")
        opener = urllib.request.build_opener(
            urllib.request.HTTPHandler, urllib.request.HTTPSHandler
        )
        with opener.open(request, timeout=timeout) as resp:
            return resp.read()

    def complete(self, system: str, user: str, max_tokens: int = 300,
                 temperature: float = 0.2) -> str:
        """Return the assistant's message text. Raises LLMError (redacted) on failure."""
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            raw = self._transport(request, self.config.timeout)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001 - body is best-effort context only
                pass
            raise LLMError(_redact(f"HTTP {exc.code}: {body}", self.config.api_key)) from None
        except Exception as exc:
            raise LLMError(_redact(f"{type(exc).__name__}: {exc}", self.config.api_key)) from None

        try:
            data: Any = json.loads(raw)
            return str(data["choices"][0]["message"]["content"])
        except Exception as exc:
            raise LLMError(
                _redact(f"unexpected response shape: {type(exc).__name__}", self.config.api_key)
            ) from None


def build_client(env: dict[str, str] | None = None,
                 transport: Callable[..., bytes] | None = None) -> LLMClient | None:
    """Build a client from the environment, or None when no LLM is configured."""
    config = LLMConfig.from_env(env)
    if config is None:
        return None
    return LLMClient(config, transport=transport)
