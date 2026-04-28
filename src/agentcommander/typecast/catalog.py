"""TypeCast catalog loader.

On every app startup:
  1. Try to fetch the latest models-catalog.json from GitHub.
  2. If success → write to local cache and use it.
  3. If failure → fall back to local cache.
  4. If no cache → fall back to bundled snapshot.
  5. If no bundled snapshot → return an empty catalog.

The user explicitly requested: "every startup, pull the agent/model hint
file .. if it fails, use the old one" — that's the contract here.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentcommander.db.connection import _default_db_dir as _user_data_dir

CATALOG_URL = "https://raw.githubusercontent.com/SixOfFive/TypeCast/main/models-catalog.json"
FETCH_TIMEOUT_S = 10.0

_cache: "CatalogLoadResult | None" = None


@dataclass
class CatalogLoadResult:
    catalog: dict[str, Any] = field(default_factory=dict)
    source: str = "bundled"  # "remote" | "cache" | "bundled" | "empty"
    fetched_at: float = 0.0
    remote_error: str | None = None
    model_count: int = 0


def _cache_path() -> Path:
    return _user_data_dir() / "typecast" / "models-catalog.json"


def _bundled_path() -> Path:
    """Look for a bundled snapshot inside the package."""
    here = Path(__file__).resolve().parent
    return here / "bundled-catalog.json"


def _count_models(catalog: dict[str, Any]) -> int:
    return sum(1 for k in catalog if k != "_meta")


def _load_json_file(p: Path) -> dict[str, Any] | None:
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fetch_remote() -> dict[str, Any]:
    req = urllib.request.Request(url=CATALOG_URL, method="GET")
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        body = resp.read()
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"invalid catalog JSON: {exc}") from exc


def _write_cache(catalog: dict[str, Any]) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(catalog), encoding="utf-8")


def refresh_catalog() -> CatalogLoadResult:
    """Refresh from remote → cache → bundled (per startup contract).

    Always returns a result; never raises.
    """
    import time

    global _cache
    fetched_at = time.time()
    remote_error: str | None = None

    # 1. Remote
    try:
        remote = _fetch_remote()
        if not isinstance(remote, dict):
            raise RuntimeError("catalog root is not a JSON object")
        try:
            _write_cache(remote)
        except OSError:
            pass  # cache write failure is not fatal
        result = CatalogLoadResult(catalog=remote, source="remote",
                                   fetched_at=fetched_at,
                                   model_count=_count_models(remote))
        _cache = result
        return result
    except (urllib.error.URLError, OSError, RuntimeError, TimeoutError) as exc:
        remote_error = f"{type(exc).__name__}: {exc}"

    # 2. Cache
    cached = _load_json_file(_cache_path())
    if cached is not None:
        result = CatalogLoadResult(catalog=cached, source="cache",
                                   fetched_at=fetched_at,
                                   remote_error=remote_error,
                                   model_count=_count_models(cached))
        _cache = result
        return result

    # 3. Bundled
    bundled = _load_json_file(_bundled_path())
    if bundled is not None:
        result = CatalogLoadResult(catalog=bundled, source="bundled",
                                   fetched_at=fetched_at,
                                   remote_error=remote_error,
                                   model_count=_count_models(bundled))
        _cache = result
        return result

    # 4. Empty
    result = CatalogLoadResult(catalog={}, source="empty",
                               fetched_at=fetched_at,
                               remote_error=remote_error or "no cache or bundled catalog",
                               model_count=0)
    _cache = result
    return result


def get_catalog() -> CatalogLoadResult | None:
    return _cache


def cache_path() -> Path:
    return _cache_path()
