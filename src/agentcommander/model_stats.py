"""Self-measured model statistics — side-by-side JSON file.

Lives at ``<cwd>/.agentcommander/model_stats.json`` next to the DB. Captures
what AC observes about each model (tokens/sec, prompt-eval throughput,
sample count, source) so even uncatalogued models — like a llama.cpp GGUF
the TypeCast catalog has never seen — get their real performance numbers
displayed in the UI.

Why a JSON file separate from the SQLite ``model_throughput`` table:
  * The DB throughput table is keyed off catalog presence in some flows
    and was originally seeded with a 100 t/s default; that default
    masked the "we never measured" state for uncatalogued models.
  * A JSON file is human-readable / hand-editable / easy to reset, and
    can be diffed across runs for performance regression hunting.
  * Source-of-truth for *self-measured* numbers — the DB still holds the
    operational running average that ``/status`` uses; this file mirrors
    those observations in a transparent format.

API is intentionally tiny:
  * ``record_observation(model, completion_tokens, duration_ms,
    chars_completed=None, prompt_tokens=None, prompt_eval_ms=None)``
  * ``get_stats(model) -> dict | None``
  * ``all_stats() -> dict``

All operations are best-effort: filesystem errors never propagate. The DB
throughput table remains the operational source-of-truth for the bar / UI.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


# Approximate chars-per-token for the fallback when the provider
# doesn't report usage (e.g. some llama.cpp builds with streaming
# /v1/chat/completions). 4 is a widely-used English approximation for
# BPE-style tokenizers; it's coarse but consistent enough for tok/s
# rate tracking, which is what the bar shows. Math/code skews lower
# (closer to 3); freeform prose skews higher (closer to 4.5).
DEFAULT_CHARS_PER_TOKEN = 4.0


def _stats_path() -> Path:
    return Path.cwd() / ".agentcommander" / "model_stats.json"


def _load() -> dict[str, Any]:
    p = _stats_path()
    if not p.exists():
        return {"models": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "models" not in data:
            return {"models": {}}
        if not isinstance(data["models"], dict):
            data["models"] = {}
        return data
    except (OSError, ValueError):
        return {"models": {}}


def _save(data: dict[str, Any]) -> None:
    """Atomic-write so a crashed process doesn't leave a half-written file."""
    p = _stats_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def estimate_tokens_from_chars(chars: int) -> int:
    """Coarse estimate when the provider doesn't report usage."""
    if chars <= 0:
        return 0
    return max(1, int(chars / DEFAULT_CHARS_PER_TOKEN))


def record_observation(
    model: str | None,
    *,
    completion_tokens: int | None = None,
    duration_ms: int | None = None,
    chars_completed: int | None = None,
    prompt_tokens: int | None = None,
    prompt_eval_ms: int | None = None,
) -> dict[str, Any] | None:
    """Update the JSON file with one observation. Returns the updated row.

    When ``completion_tokens`` is missing/0 but ``chars_completed`` is
    supplied, we estimate via ``estimate_tokens_from_chars``. The ``source``
    field reflects whether the row was last updated from a measured count
    or an estimate.

    Skips silently when there's nothing useful to learn (no model name, no
    duration, neither tokens nor chars).
    """
    if not model:
        return None
    if not duration_ms or duration_ms <= 0:
        return None

    tokens = completion_tokens or 0
    estimated = False
    if tokens <= 0 and chars_completed and chars_completed > 0:
        tokens = estimate_tokens_from_chars(chars_completed)
        estimated = True
    if tokens <= 0:
        return None

    seconds = duration_ms / 1000.0
    rate = float(tokens) / seconds

    data = _load()
    row = data["models"].get(model)
    if row is None or not isinstance(row, dict):
        new_avg = rate
        samples = 1
    else:
        old_avg = float(row.get("tokens_per_second") or 0.0)
        if old_avg <= 0:
            new_avg = rate
        else:
            # Same 50/50 EMA the DB version uses so the two stay in sync.
            new_avg = (old_avg + rate) / 2.0
        samples = int(row.get("samples") or 0) + 1

    record: dict[str, Any] = {
        "tokens_per_second": round(new_avg, 2),
        "samples": samples,
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "source": "estimated" if estimated else "measured",
        "last_completion_tokens": tokens,
        "last_duration_ms": duration_ms,
    }
    if prompt_tokens and prompt_eval_ms and prompt_eval_ms > 0:
        record["prompt_eval_tps"] = round(
            float(prompt_tokens) / (prompt_eval_ms / 1000.0), 2,
        )
        record["last_prompt_tokens"] = prompt_tokens

    data["models"][model] = record
    _save(data)
    return record


def get_stats(model: str | None) -> dict[str, Any] | None:
    if not model:
        return None
    data = _load()
    row = data["models"].get(model)
    if isinstance(row, dict):
        return row
    return None


def all_stats() -> dict[str, Any]:
    """Return the full ``{"models": {...}}`` blob."""
    return _load()
