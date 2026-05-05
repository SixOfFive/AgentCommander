"""Minimal ANSI markdown renderer.

Pure stdlib. Converts a subset of markdown into ANSI-styled text that
matches the Claude Code linux console look. Designed for the *final*
assistant message — streaming partial tokens render plain, then the
finished message re-renders through this module.

Supported subset:
  - # / ## / ### headings              → bright + bold
  - **bold** / __bold__                → bold
  - *italic* / _italic_                → italic
  - `inline code`                      → 256-color tinted background
  - ```fenced code blocks```           → indented + dim border, language tagged
  - - / * bullets, 1. numbered lists   → ANSI bullets, indented
  - > blockquote                       → vertical bar + dim
  - --- horizontal rule                → faint rule
  - links [text](url)                  → underline (text) + dim parenthetical url

Anything outside this subset is passed through unchanged. Trade safety for
simplicity — we never run user-supplied HTML/JS.
"""
from __future__ import annotations

import re
import textwrap
from typing import Iterator

from agentcommander.tui.ansi import (
    BOLD,
    DIM,
    ITALIC,
    RESET,
    UNDERLINE,
    bg256,
    fg256,
    style,
    supports_color,
    term_size,
)

# ─── Inline transforms ─────────────────────────────────────────────────────

# Underscore emphasis is intentionally NOT supported. CommonMark allows
# `_italic_` and `__bold__`, but in a CLI tool where chat output is full
# of filenames (`__pycache__`, `__init__.py`, `binary_search_tree29.py`,
# `_private_var`) and Python identifiers, underscore emphasis routinely
# eats the underscores and renders garbled names. The narrow
# intraword-aware lookbehind/lookahead approach (CommonMark's spec
# answer) still mishandles `__pycache__` because the leading `__` has
# no preceding char to flank against. Killing the underscore variants
# entirely is the predictable rule: filenames preserved, model still
# has `**bold**` and `*italic*` which don't collide with identifiers.
_BOLD_RX = re.compile(r"(?<!\\)\*\*([^*\n]+?)\*\*")
_ITALIC_RX = re.compile(r"(?<!\\)\*([^*\n]+?)\*")
_INLINE_CODE_RX = re.compile(r"`([^`\n]+?)`")
_LINK_RX = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_STRIKE_RX = re.compile(r"~~([^~\n]+?)~~")


_INLINE_CODE_FG = fg256(216)   # warm yellow
_INLINE_CODE_BG = bg256(236)   # dark grey
_HEADING_1 = fg256(75) + BOLD
_HEADING_2 = fg256(141) + BOLD
_HEADING_3 = fg256(180) + BOLD
_LINK_URL = DIM


def _bullet() -> str:
    return fg256(245) + "• " + RESET if supports_color() else "* "


def _quote_bar() -> str:
    return fg256(238) + "│ " + RESET if supports_color() else "| "


def _fence_border() -> str:
    return fg256(238) + "│ " + RESET if supports_color() else "  "


def _rule() -> str:
    return (fg256(238) + "─" * 60 + RESET) if supports_color() else "-" * 60


def _render_inline(line: str) -> str:
    """Apply inline transforms (bold / italic / code / link / strike)."""
    if not supports_color():
        # Strip the markup but keep text.
        line = _BOLD_RX.sub(lambda m: m.group(1) or m.group(2), line)
        line = _ITALIC_RX.sub(lambda m: m.group(1) or m.group(2), line)
        line = _INLINE_CODE_RX.sub(lambda m: m.group(1), line)
        line = _LINK_RX.sub(lambda m: f"{m.group(1)} ({m.group(2)})", line)
        line = _STRIKE_RX.sub(lambda m: m.group(1), line)
        return line

    # Inline code first (so its content isn't bolded).
    line = _INLINE_CODE_RX.sub(
        lambda m: f"{_INLINE_CODE_FG}{_INLINE_CODE_BG} {m.group(1)} {RESET}",
        line,
    )
    line = _BOLD_RX.sub(
        lambda m: f"{BOLD}{m.group(1) or m.group(2)}{RESET}", line,
    )
    line = _ITALIC_RX.sub(
        lambda m: f"{ITALIC}{m.group(1) or m.group(2)}{RESET}", line,
    )
    line = _STRIKE_RX.sub(
        lambda m: f"{DIM}{m.group(1)}{RESET}", line,
    )
    line = _LINK_RX.sub(
        lambda m: f"{UNDERLINE}{m.group(1)}{RESET} {_LINK_URL}({m.group(2)}){RESET}",
        line,
    )
    return line


# ─── Block-level transforms ────────────────────────────────────────────────


def _wrap_text(text: str, width: int, indent: str = "") -> Iterator[str]:
    """Wrap text but be mindful of ANSI escapes (don't count them as width)."""
    # textwrap.wrap doesn't know about ANSI escapes, so we strip them for
    # length calculation, wrap on the stripped form, and stitch back the
    # original. For simplicity here, wrap on visible text after inline
    # transforms — accept that wrap points may shift slightly when escapes
    # are dense. Good enough for terminal output.
    # Guard: deeply-nested lists or narrow terminals can produce width <= 0;
    # textwrap.wrap raises ValueError in that case, so just emit unwrapped.
    if width <= 0:
        yield indent + text
        return
    visible = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    if len(visible) <= width:
        yield indent + text
        return
    # Plain re-wrap; ANSI codes survive as embedded characters.
    for piece in textwrap.wrap(text, width=width,
                                replace_whitespace=False, drop_whitespace=False):
        yield indent + piece


def render_markdown(text: str, *, indent: str = "") -> str:
    """Render markdown to ANSI-styled text. Returns a multi-line string."""
    if not text:
        return text
    cols, _ = term_size()
    width = max(40, cols - len(indent) - 2)
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    fence_lang = ""
    fence_buf: list[str] = []

    def flush_fence() -> None:
        nonlocal fence_buf, fence_lang
        if not fence_buf:
            return
        # Tag the block with its language and add a dim left border.
        if fence_lang:
            out.append(indent + style("muted", f"  ┄ {fence_lang} ┄"))
        for cl in fence_buf:
            out.append(indent + _fence_border() + cl)
        out.append(indent + style("muted", "  ┄"))
        fence_buf.clear()
        fence_lang = ""

    for raw in lines:
        # Code fence boundaries
        m = re.match(r"^\s*```(\w*)\s*$", raw)
        if m:
            if in_fence:
                flush_fence()
                in_fence = False
            else:
                in_fence = True
                fence_lang = m.group(1)
            continue
        if in_fence:
            fence_buf.append(raw)
            continue

        stripped = raw.rstrip()
        if not stripped:
            out.append("")
            continue

        # Headings
        h = re.match(r"^(\s*)(#{1,3})\s+(.+?)\s*$", stripped)
        if h:
            level = len(h.group(2))
            content = _render_inline(h.group(3))
            color = _HEADING_1 if level == 1 else _HEADING_2 if level == 2 else _HEADING_3
            out.append(indent + h.group(1) + (color + content + RESET if supports_color() else content))
            continue

        # Horizontal rule
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", stripped):
            out.append(indent + _rule())
            continue

        # Block quote
        q = re.match(r"^(\s*)>\s?(.*)$", stripped)
        if q:
            content = _render_inline(q.group(2))
            for line in _wrap_text(content, width - 2, indent=""):
                out.append(indent + q.group(1) + _quote_bar() + style("muted", line))
            continue

        # Bullet
        b = re.match(r"^(\s*)[-*]\s+(.+?)\s*$", stripped)
        if b:
            content = _render_inline(b.group(2))
            first = True
            for line in _wrap_text(content, width - len(b.group(1)) - 2, indent=""):
                prefix = b.group(1) + (_bullet() if first else "  ")
                out.append(indent + prefix + line)
                first = False
            continue

        # Numbered list
        n = re.match(r"^(\s*)(\d+)\.\s+(.+?)\s*$", stripped)
        if n:
            num = n.group(2)
            content = _render_inline(n.group(3))
            first = True
            num_prefix = style("muted", f"{num}. ")
            for line in _wrap_text(content, width - len(n.group(1)) - len(num) - 2, indent=""):
                prefix = n.group(1) + (num_prefix if first else " " * (len(num) + 2))
                out.append(indent + prefix + line)
                first = False
            continue

        # Plain paragraph line — apply inline transforms + wrap
        content = _render_inline(stripped)
        for line in _wrap_text(content, width, indent=""):
            out.append(indent + line)

    if in_fence:
        # Unterminated fence — flush what we have.
        flush_fence()

    return "\n".join(out)
