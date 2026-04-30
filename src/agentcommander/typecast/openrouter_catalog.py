"""OpenRouter Paid scores catalog — lives at the repo root, NOT cwd.

The user's earlier decision: scores are accumulated by AgentCommander's
specialized voting tests (separate, future work) and persisted to
``resources/typecast-openrouter-paid.json`` in the SOURCE TREE — not the
project's working directory. That way every install of AgentCommander
shares the same accumulated knowledge across projects.

This module provides:
  - ``catalog_path()`` — locate the JSON file, walking up from this file
    so dev (in-repo) and installed (next to package) layouts both work
  - ``load()`` / ``save(catalog)`` — read/write helpers with safe defaults
  - ``record_vote(model, role, increment, scope)`` — apply one
    preferred_for / avoid_for adjustment from a voting pass
  - ``empty_catalog()`` — the seed shape used when the file is missing

The schema mirrors the main TypeCast catalog so the existing
``autoconfig.py`` threshold-cascade picker can consume both files
seamlessly once the voting tests populate ``_models``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


CATALOG_FILENAME = "typecast-openrouter-paid.json"
VOTE_INCREMENT = 1
VOTE_MAX = 1_000_000
VOTE_MIN = -1_000_000


def empty_catalog() -> dict[str, Any]:
    """Return the seed shape used when the file is missing or corrupt.

    Matches the schema in ``resources/typecast-openrouter-paid.json`` so
    a save-after-empty produces a file equivalent to the bundled one.
    """
    return {
        "_meta": {
            "description": (
                "OpenRouter Paid model scores per agent role. "
                "Auto-populated by AgentCommander's voting tests — "
                "do not edit by hand."
            ),
            "registrySource": "openrouter.ai/models (live fetch on first vote)",
            "voteIncrement": VOTE_INCREMENT,
            "voteMax": VOTE_MAX,
            "voteMin": VOTE_MIN,
            "scoreFormula": (
                "preferred_for/avoid_for tags accumulate +/- voteIncrement "
                "per pass; threshold cascade picks highest-scoring per role"
            ),
            "lastVoteAt": None,
            "voteCount": 0,
            "modelCount": 0,
        },
        "_models": {},
    }


def catalog_path() -> Path:
    """Locate ``typecast-openrouter-paid.json`` in the AgentCommander repo.

    Search order matches ``agents/prompts.py:_prompt_dir``:
      1. env override (``AGENTCOMMANDER_OR_PAID_CATALOG`` — full path)
      2. installed-package neighbor: ``<pkg>/../../resources/<file>``
      3. repo-dev: walk up from this module until a ``resources/<file>`` is found
      4. fallback to a Path that doesn't exist — ``load`` returns the
         empty catalog and ``save`` will create the file at this path.
    """
    import os

    env = os.environ.get("AGENTCOMMANDER_OR_PAID_CATALOG")
    if env:
        return Path(env)

    pkg_neighbor = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "resources" / CATALOG_FILENAME
    )
    if pkg_neighbor.is_file():
        return pkg_neighbor

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "resources" / CATALOG_FILENAME
        if candidate.is_file():
            return candidate
        # Fall through to first parent that has a `resources/` dir even
        # if the file isn't there yet — that's where we'll save.
        if (parent / "resources").is_dir():
            return parent / "resources" / CATALOG_FILENAME

    return Path("resources") / CATALOG_FILENAME


def load() -> dict[str, Any]:
    """Read the OR Paid catalog. Returns an empty catalog when the file
    is missing or unreadable so callers can treat the API uniformly.

    A corrupt file is treated like a missing file — the program never
    crashes on a malformed JSON catalog. Voting writes will silently
    overwrite a corrupt file with the empty shape on the next save.
    """
    path = catalog_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return empty_catalog()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return empty_catalog()
    if not isinstance(data, dict):
        return empty_catalog()
    if "_models" not in data or not isinstance(data["_models"], dict):
        data["_models"] = {}
    if "_meta" not in data or not isinstance(data["_meta"], dict):
        data["_meta"] = empty_catalog()["_meta"]
    return data


def save(catalog: dict[str, Any]) -> bool:
    """Persist the catalog to disk. Returns True on success, False on
    write failure (read-only filesystem, missing parent dir, etc.).

    Updates ``_meta.lastVoteAt`` and ``_meta.modelCount`` so a glance
    at the file tells the user how recent the data is and how many
    models the voting has touched so far.
    """
    path = catalog_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    meta = catalog.setdefault("_meta", empty_catalog()["_meta"])
    meta["lastVoteAt"] = int(time.time() * 1000)
    meta["modelCount"] = len(catalog.get("_models", {}))
    try:
        path.write_text(
            json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def _empty_model_entry() -> dict[str, Any]:
    """Per-model row default — matches main TypeCast catalog shape."""
    return {
        "preferred_for": [],
        "avoid_for": [],
        "score": 0,
        "runs": 0,
        "lastBumpAt": 0,
    }


def record_vote(model_id: str, role: str, *, scope: str = "preferred",
                increment: int = VOTE_INCREMENT) -> int:
    """Apply one vote to ``(model_id, role)``.

    ``scope`` is ``"preferred"`` (boost) or ``"avoid"`` (penalty). The
    role is added to (or removed from) the corresponding tag list and
    the model's overall score is bumped by ``increment``. Returns the
    model's new aggregate score (clamped to [VOTE_MIN, VOTE_MAX]).

    Future voting tests will call this from per-role benchmark passes:
      - good output → record_vote(model, role, scope="preferred")
      - guard rejection / parse failure → record_vote(model, role, scope="avoid")
    """
    catalog = load()
    models = catalog["_models"]
    entry = models.setdefault(model_id, _empty_model_entry())

    pref = entry.setdefault("preferred_for", [])
    avoid = entry.setdefault("avoid_for", [])

    if scope == "preferred":
        if role not in pref:
            pref.append(role)
        if role in avoid:
            avoid.remove(role)
        delta = abs(increment)
    elif scope == "avoid":
        if role not in avoid:
            avoid.append(role)
        if role in pref:
            pref.remove(role)
        delta = -abs(increment)
    else:
        raise ValueError(f'scope must be "preferred" or "avoid"; got {scope!r}')

    entry["score"] = max(VOTE_MIN, min(VOTE_MAX, int(entry.get("score", 0)) + delta))
    entry["runs"] = int(entry.get("runs", 0)) + 1
    entry["lastBumpAt"] = int(time.time() * 1000)

    catalog["_meta"]["voteCount"] = int(catalog["_meta"].get("voteCount", 0)) + 1
    save(catalog)
    return int(entry["score"])


def has_data() -> bool:
    """True when the catalog has at least one scored model.

    Used by the autoconfig dispatch to decide whether OR Paid can be
    auto-configured (data present) or must defer to manual setup
    (still empty — voting hasn't run yet).
    """
    return bool(load().get("_models"))
