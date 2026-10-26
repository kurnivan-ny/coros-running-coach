"""Minimal ``.env`` loader (standard library) for the command-line entry points.

Existing environment variables win, and empty values are skipped, so a blank
``DATABASE_URL=`` line in ``.env`` never shadows a real one. The dashboard does
not use this: on Streamlit Cloud configuration arrives through ``st.secrets``.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path


def load_env(path: str | os.PathLike[str] = ".env",
             environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Load KEY=VALUE lines into ``environ``. Returns the names that were set."""
    environ = os.environ if environ is None else environ
    file = Path(path)
    if not file.is_file():
        return []
    loaded: list[str] = []
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and not environ.get(key):
            environ[key] = value
            loaded.append(key)
    return loaded
