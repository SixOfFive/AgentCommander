"""TypeCast catalog loader with conditional-GET (ETag + Last-Modified) freshness.

Behavior contract:
  1. On every startup, send a conditional GET to the catalog URL using the
     cached ETag/Last-Modified headers. If the server returns 304 Not Modified,
     keep the cached copy untouched (no body downloaded).
  2. If the server returns 200 with a fresh body, write it + the new headers
     to the user-data folder.
  3. If the network is unavailable, fall back to the cached copy.
  4. If no cache exists, fall back to a bundled snapshot (if present).
  5. If neither exists, return an empty catalog so the rest of the app can
     still run.

The cache lives at:
  ${XDG_DATA_HOME or %APPDATA% or ~/Library/Application Support}/AgentCommander/typecast/
    ├── models-catalog.json
    └── models-catalog.meta.json   (etag, last_modified, fetched_at, source_url)

Both files are gitignored (see project root .gitignore).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentcommander.db.connection import _default_db_dir as _user_data_dir

CATALOG_URL = "https://raw.githubusercontent.com/SixOfFive/TypeCast/main/models-catalog.json"
FETCH_TIMEOUT_S = 10.0
USER_AGENT = "AgentCommander/0.1 (+local CLI)"

_cache: "CatalogLoadResult | None" = None


@dataclass
class CatalogLoadResult:
    catalog: dict[str, Any] = field(default_factory=dict)
    source: str = "bundled"            # "remote" | "cache-fresh" | "cache" | "bundled" | "empty"
    fetched_at: float = 0.0
    remote_error: str | None = None
    model_count: int = 0
    etag: str | None = None
    last_modified: str | None = None


def _cache_path() -> Path:
    return _user_data_dir() / "typecast" / "models-catalog.json"


def _meta_path() -> Path:
    return _user_data_dir() / "typecast" / "models-catalog.meta.json"


def _bundled_path() -> Path:
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


def _read_meta() -> dict[str, Any]:
    data = _load_json_file(_meta_path())
    return data or {}


def _write_cache(body_bytes: bytes, etag: str | None, last_modified: str | None) -> None:
    cp = _cache_path()
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_bytes(body_bytes)
    meta: dict[str, Any] = {
        "etag": etag,
        "last_modified": last_modified,
        "fetched_at": time.time(),
        "source_url": CATALOG_URL,
    }
    _meta_path().write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _conditional_fetch() -> tuple[bytes | None, str | None, str | None, bool]:
    """Send a conditional GET. Returns (body_bytes, etag, last_modified, not_modified).

    If `not_modified` is True, body_bytes is None — caller should keep the cache.
    Raises urllib.error.URLError / OSError on transport failure.
    """
    meta = _read_meta()
    headers = {"User-Agent": USER_AGENT}
    if isinstance(meta.get("etag"), str):
        headers["If-None-Match"] = meta["etag"]
    if isinstance(meta.get("last_modified"), str):
        headers["If-Modified-Since"] = meta["last_modified"]

    req = urllib.request.Request(url=CATALOG_URL, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            body = resp.read()
            new_etag = resp.headers.get("ETag")
            new_lm = resp.headers.get("Last-Modified")
            return body, new_etag, new_lm, False
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, None, None, True
        raise


def refresh_catalog() -> CatalogLoadResult:
    """Refresh from remote (conditional GET) → cache → bundled. Always returns a result."""
    global _cache
    fetched_at = time.time()
    remote_error: str | None = None

    # 1) Conditional GET
    try:
        body, etag, last_modified, not_modified = _conditional_fetch()

        if not_modified:
            cached = _load_json_file(_cache_path())
            if cached is not None:
                meta = _read_meta()
                _cache = CatalogLoadResult(
                    catalog=cached,
                    source="cache-fresh",  # remote confirmed our cache is current
                    fetched_at=fetched_at,
                    model_count=_count_models(cached),
                    etag=meta.get("etag"),
                    last_modified=meta.get("last_modified"),
                )
                return _cache
            # 304 with no cache shouldn't happen in practice — fall through.

        if body is not None:
            try:
                catalog = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError(f"invalid catalog JSON: {exc}") from exc
            if not isinstance(catalog, dict):
                raise RuntimeError("catalog root is not a JSON object")
            try:
                _write_cache(body, etag, last_modified)
            except OSError:
                pass
            _cache = CatalogLoadResult(
                catalog=catalog, source="remote", fetched_at=fetched_at,
                model_count=_count_models(catalog),
                etag=etag, last_modified=last_modified,
            )
            return _cache
    except (urllib.error.URLError, OSError, RuntimeError, TimeoutError) as exc:
        remote_error = f"{type(exc).__name__}: {exc}"

    # 2) Cache fallback (network failed)
    cached = _load_json_file(_cache_path())
    if cached is not None:
        meta = _read_meta()
        _cache = CatalogLoadResult(
            catalog=cached, source="cache", fetched_at=fetched_at,
            remote_error=remote_error, model_count=_count_models(cached),
            etag=meta.get("etag"), last_modified=meta.get("last_modified"),
        )
        return _cache

    # 3) Bundled fallback
    bundled = _load_json_file(_bundled_path())
    if bundled is not None:
        _cache = CatalogLoadResult(
            catalog=bundled, source="bundled", fetched_at=fetched_at,
            remote_error=remote_error, model_count=_count_models(bundled),
        )
        return _cache

    # 4) Empty
    _cache = CatalogLoadResult(
        catalog={}, source="empty", fetched_at=fetched_at,
        remote_error=remote_error or "no cache or bundled catalog",
        model_count=0,
    )
    return _cache


def get_catalog() -> CatalogLoadResult | None:
    return _cache


def cache_path() -> Path:
    return _cache_path()
