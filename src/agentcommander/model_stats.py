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


# Approximate chars-per-token for the fallback when the provider doesn't
# report usage (some llama.cpp builds via streaming /v1/chat/completions).
# We pick the divisor based on rough content shape because BPE token
# rates differ a lot across content types:
#
#   English prose:  ~4.0 chars/token  (the canonical default)
#   Code / heavy punctuation:  ~3.0   (`def foo():` is 4 tokens, 11 chars)
#   CJK / Japanese / Korean:   ~1.5   (one CJK char often = 1 token)
#
# Misclassifying skews tok/s reporting by a multiplicative factor — bad
# for the user's mental model of how fast their model is. The detector
# is intentionally conservative: it only switches off the prose default
# when the signal is strong (>20% CJK, or code-heavy punctuation).
DEFAULT_CHARS_PER_TOKEN = 4.0
CODE_CHARS_PER_TOKEN = 3.0
CJK_CHARS_PER_TOKEN = 1.5

# Punctuation that's denser-than-prose-average in code: braces, semicolons,
# parentheses with operators, etc. Used as a heuristic for "is this code?".
_CODE_HEAVY_PUNCT = "{}();[]:=<>!&|+*/-"


def _looks_like_cjk(text: str) -> bool:
    """True when at least 20% of chars are in CJK Unicode blocks.

    Covers Hiragana / Katakana / CJK Unified Ideographs / Hangul, which
    is the bulk of what gets misestimated by the chars/4 default.
    """
    if not text:
        return False
    cjk = 0
    for ch in text:
        cp = ord(ch)
        if (0x3040 <= cp <= 0x30FF        # Hiragana + Katakana
                or 0x3400 <= cp <= 0x4DBF      # CJK Ext A
                or 0x4E00 <= cp <= 0x9FFF      # CJK Unified
                or 0xAC00 <= cp <= 0xD7AF      # Hangul Syllables
                or 0xF900 <= cp <= 0xFAFF):    # CJK Compatibility
            cjk += 1
    return cjk * 5 >= len(text)


def _looks_like_code(text: str) -> bool:
    """True when ≥10% of chars are code-heavy punctuation.

    English prose runs ~3-4% on this set; code runs 12-25%.
    """
    if not text:
        return False
    hits = sum(1 for ch in text if ch in _CODE_HEAVY_PUNCT)
    return hits * 10 >= len(text)


def _chars_per_token_for(text: str) -> float:
    """Pick the right divisor based on rough content shape."""
    if _looks_like_cjk(text):
        return CJK_CHARS_PER_TOKEN
    if _looks_like_code(text):
        return CODE_CHARS_PER_TOKEN
    return DEFAULT_CHARS_PER_TOKEN


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


def estimate_tokens_from_chars(chars: int, sample_text: str | None = None) -> int:
    """Coarse estimate when the provider doesn't report usage.

    When ``sample_text`` is supplied, the divisor adapts to content
    shape (CJK ≈ 1.5, code ≈ 3.0, prose ≈ 4.0). Otherwise falls back
    to the prose default. Always returns at least 1 for non-empty
    input so a tiny output still produces a measurable rate.
    """
    if chars <= 0:
        return 0
    divisor = (_chars_per_token_for(sample_text)
               if sample_text else DEFAULT_CHARS_PER_TOKEN)
    return max(1, int(chars / divisor))


def record_observation(
    model: str | None,
    *,
    completion_tokens: int | None = None,
    duration_ms: int | None = None,
    chars_completed: int | None = None,
    sample_text: str | None = None,
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
