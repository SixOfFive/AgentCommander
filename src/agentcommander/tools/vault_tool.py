"""Vault recall tools — read-only access to a local Obsidian-style notes vault.

Gives the orchestrator long-term memory: it can search the user's distilled
notes (projects, decisions, infra, technical patterns) and read individual
notes, instead of relying on the per-turn scratchpad alone. Mirrors the recall
protocol a capable agent uses — search first, then read the relevant note.

Two verbs:
  - ``vault_search`` — find notes matching a query. Semantic when an embeddings
    index + an embedding endpoint are available (cosine over the index, query
    embedded via Ollama ``nomic-embed-text``); otherwise lexical (keyword
    scan over the markdown). Returns the top matches as ``[[Note name]]`` with
    short snippets.
  - ``vault_read`` — read one note's full body by name or relative path.

Privacy / safety:
  - The vault PATH is configuration (project-local DB, gitignored) — set with
    ``/vault set <path>``. This module ships no path and no vault content.
  - **Read-only and sandboxed**: every file access is confined to the vault
    root via ``safety.sandbox.is_path_within`` (rejects ``../`` escapes and
    out-of-tree symlinks). The tools never write.
  - Output is budgeted (top-k results, truncated snippets/bodies) so a 100+ MB
    vault can't blow the model's context.

Pure stdlib: ``os``/``json``/``math``/``urllib`` only. No vector library — the
index is ~1600 small vectors, so cosine in plain Python is instant.
"""
from __future__ import annotations

import json
import math
import os
import urllib.request
from typing import Any

from agentcommander.safety.sandbox import is_path_within
from agentcommander.tools.dispatcher import register
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult

# Output budgets — keep recall from flooding the model's context.
DEFAULT_TOP_K = 5
SNIPPET_CHARS = 240
MAX_READ_CHARS = 6000
EMBED_TIMEOUT_S = 30.0
DEFAULT_EMBED_MODEL = "nomic-embed-text"


# ─── Config (project-local DB; gitignored) ──────────────────────────────────


def _cfg(key: str, default: Any = None) -> Any:
    try:
        from agentcommander.db.repos import get_config
        return get_config(key, default)
    except Exception:  # noqa: BLE001
        return default


def vault_root() -> str | None:
    """Configured vault directory, or None if unset / not a directory."""
    p = _cfg("vault_path")
    if isinstance(p, str) and p and os.path.isdir(p):
        return p
    return None


def _index_path(root: str) -> str:
    custom = _cfg("vault_index_path")
    if isinstance(custom, str) and custom:
        return custom
    return os.path.join(root, "_index", "embeddings.json")


def _embed_endpoint() -> str | None:
    """Where to embed the query. Explicit config wins; else the first enabled
    Ollama provider's endpoint (the vault's own embeddings were built with
    Ollama nomic-embed-text, so reuse that fleet)."""
    ep = _cfg("vault_embed_endpoint")
    if isinstance(ep, str) and ep:
        return ep
    try:
        from agentcommander.providers.base import list_active
        for p in list_active():
            if getattr(p, "type", None) == "ollama" and getattr(p, "endpoint", None):
                return p.endpoint  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return None


# ─── Note file resolution (sandboxed) ───────────────────────────────────────


def _iter_markdown(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip Obsidian internals + the embeddings index dir.
        dirnames[:] = [d for d in dirnames if d not in (".obsidian", ".git", "_index")]
        for fn in filenames:
            if fn.lower().endswith(".md"):
                yield os.path.join(dirpath, fn)


def _resolve_note(root: str, name: str) -> str | None:
    """Map a note name / relative path to an absolute .md path under root.

    Accepts a bare title ("AgentCommander project overview"), a filename
    ("foo.md"), or a relative path ("Topics/foo.md"). Returns None if not
    found or if it would escape the sandbox.
    """
    name = (name or "").strip().strip("[]")
    if not name:
        return None
    # Direct relative path?
    cand = os.path.join(root, name if name.lower().endswith(".md") else name + ".md")
    if os.path.isfile(cand) and is_path_within(cand, root):
        return cand
    # Fall back to a stem match across the tree (case-insensitive).
    target = os.path.splitext(os.path.basename(name))[0].lower()
    for path in _iter_markdown(root):
        if os.path.splitext(os.path.basename(path))[0].lower() == target:
            if is_path_within(path, root):
                return path
    return None


def _read_note(path: str, limit: int = MAX_READ_CHARS) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(limit + 1)
    except OSError as exc:
        return f"[could not read note: {exc}]"
    if len(text) > limit:
        text = text[:limit] + "\n…[truncated]"
    return text


def _snippet(path: str) -> str:
    text = _read_note(path, SNIPPET_CHARS * 3)
    # Drop YAML front-matter and collapse whitespace for a readable preview.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    text = " ".join(text.split())
    return text[:SNIPPET_CHARS]


def _rel(root: str, path: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return os.path.basename(path)


# ─── Semantic search (reuse the vault's embeddings index) ────────────────────


def _embed_query(text: str, endpoint: str, model: str) -> "list[float] | None":
    """Embed ``text`` via Ollama. Tries the newer /api/embed then the older
    /api/embeddings. Returns the vector or None on any failure."""
    for path, body, pick in (
        ("/api/embed", {"model": model, "input": text},
         lambda d: (d.get("embeddings") or [None])[0]),
        ("/api/embeddings", {"model": model, "prompt": text},
         lambda d: d.get("embedding")),
    ):
        try:
            req = urllib.request.Request(
                endpoint.rstrip("/") + path,
                data=json.dumps(body).encode("utf-8"),
                method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            vec = pick(data)
            if isinstance(vec, list) and vec:
                return [float(x) for x in vec]
        except Exception:  # noqa: BLE001 - try the next endpoint shape
            continue
    return None


def _cosine(a: "list[float]", b: "list[float]") -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


def _semantic_search(root: str, query: str, top_k: int) -> "list[tuple[str, float]] | None":
    """Return [(note_name, score)] or None if semantic search isn't available."""
    index_path = _index_path(root)
    if not os.path.isfile(index_path):
        return None
    endpoint = _embed_endpoint()
    if not endpoint:
        return None
    model = _cfg("vault_embed_model") or DEFAULT_EMBED_MODEL
    qvec = _embed_query(query, endpoint, str(model))
    if qvec is None:
        return None
    try:
        with open(index_path, "r", encoding="utf-8") as fh:
            index = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(index, dict):
        return None
    scored: list[tuple[str, float]] = []
    for name, entry in index.items():
        vec = entry.get("vec") if isinstance(entry, dict) else None
        if isinstance(vec, list) and vec:
            scored.append((name, _cosine(qvec, vec)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:top_k]


# ─── Lexical search (fallback / no embeddings) ───────────────────────────────


def _lexical_search(root: str, query: str, top_k: int) -> "list[tuple[str, float]]":
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        return []
    scored: list[tuple[str, float]] = []
    for path in _iter_markdown(root):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read().lower()
        except OSError:
            continue
        name = os.path.splitext(os.path.basename(path))[0]
        name_l = name.lower()
        score = 0.0
        for t in terms:
            score += body.count(t)
            score += 5 * name_l.count(t)   # title hits weigh more
        if score > 0:
            scored.append((name, score))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:top_k]


# ─── Tool handlers ───────────────────────────────────────────────────────────


def _vault_search(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    root = vault_root()
    if root is None:
        return ToolResult(ok=False, error=(
            "vault not configured — set it with `/vault set <path>` "
            "(stored in the project-local DB, never committed)."))
    query = payload.get("input") or payload.get("query") or ""
    if not isinstance(query, str) or not query.strip():
        return ToolResult(ok=False, error="vault_search needs a query in 'input'.")
    top_k = DEFAULT_TOP_K
    try:
        if payload.get("top_k"):
            top_k = max(1, min(20, int(payload["top_k"])))
    except (TypeError, ValueError):
        pass

    mode = (payload.get("mode") or "auto").lower()
    results: list[tuple[str, float]] | None = None
    used = "lexical"
    if mode in ("auto", "semantic"):
        results = _semantic_search(root, query, top_k)
        if results is not None:
            used = "semantic"
    if results is None:  # semantic unavailable, or mode=lexical
        results = _lexical_search(root, query, top_k)
        used = "lexical"

    ctx.audit("vault.search", {"mode": used, "query": query[:120], "hits": len(results)})
    if not results:
        return ToolResult(ok=True, output=f"No vault notes matched '{query}'.",
                          data={"mode": used, "hits": 0})

    lines = [f"Vault search ({used}) — top {len(results)} for \"{query}\":", ""]
    for i, (name, score) in enumerate(results, 1):
        path = _resolve_note(root, name)
        snip = _snippet(path) if path else ""
        scoretxt = f"{score:.2f}" if used == "semantic" else f"{int(score)} hits"
        lines.append(f"{i}. [[{name}]] ({scoretxt})")
        if snip:
            lines.append(f"   {snip}")
    lines.append("")
    lines.append("Use vault_read with a note name to get its full body.")
    return ToolResult(ok=True, output="\n".join(lines),
                      data={"mode": used, "hits": len(results),
                            "notes": [n for n, _ in results]})


def _vault_read(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    root = vault_root()
    if root is None:
        return ToolResult(ok=False, error=(
            "vault not configured — set it with `/vault set <path>`."))
    name = payload.get("input") or payload.get("path") or payload.get("name") or ""
    if not isinstance(name, str) or not name.strip():
        return ToolResult(ok=False, error="vault_read needs a note name/path in 'input'.")
    path = _resolve_note(root, name)
    if path is None:
        return ToolResult(ok=False, error=(
            f"note not found in the vault (or outside it): {name!r}. "
            f"Try vault_search first to find the exact name."))
    body = _read_note(path)
    ctx.audit("vault.read", {"note": _rel(root, path), "chars": len(body)})
    return ToolResult(ok=True, output=f"# {_rel(root, path)}\n\n{body}",
                      data={"note": _rel(root, path)})


register(ToolDescriptor(
    name="vault_search",
    description=("Search the user's local notes vault (long-term memory: "
                 "projects, decisions, infra, patterns). Semantic when an "
                 "embeddings index is available, else keyword. Returns top "
                 "matching notes as [[name]] + snippets. Read-only."),
    privileged=False,
    input_schema={
        "type": "object",
        "properties": {
            "input": {"type": "string"},
            "mode": {"type": "string", "enum": ["auto", "semantic", "lexical"]},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["input"],
    },
    handler=_vault_search,
))

register(ToolDescriptor(
    name="vault_read",
    description=("Read one note's full body from the local vault by name or "
                 "relative path (run vault_search first to find the name). "
                 "Read-only, sandboxed to the vault directory."),
    privileged=False,
    input_schema={
        "type": "object",
        "properties": {"input": {"type": "string"}},
        "required": ["input"],
    },
    handler=_vault_read,
))
