"""browser tool — fetch HTML and extract readable text + links.

Differs from ``fetch`` (web_tool):

  - ``fetch`` returns the raw HTTP body — fine for plain-text or JSON
    APIs, but a 500 KB blob of HTML burns context for the model.
  - ``browser`` runs the body through a stdlib HTML parser
    (``html.parser``), strips ``<script>`` / ``<style>``, collapses
    whitespace, and returns the visible page text + an extracted link
    list. Much closer to what the model actually wants to read.

Pure stdlib — ``html.parser`` is built in. No JavaScript execution
(would need a headless browser dependency); we explicitly tell the
caller via the description so models don't expect JS-rendered content
to come back.

Same SSRF guard + injection scan as fetch — text extracted from any
arbitrary page can still try to hijack the agent.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from agentcommander.safety.host_validator import validate_user_host
from agentcommander.safety.prompt_injection import detect_prompt_injection
from agentcommander.tools.dispatcher import register
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult


BROWSER_TIMEOUT_S = 30.0
MAX_BODY_BYTES = 5_000_000
MAX_OUTPUT_CHARS = 16_000  # Cap final text payload to keep prompt budget sane.
MAX_LINKS = 50  # Surface a manageable number of links to the model.
USER_AGENT = (
    "Mozilla/5.0 (compatible; AgentCommander/0.1; "
    "+https://github.com/SixOfFive/AgentCommander)"
)

# Tags whose content should be SKIPPED entirely (never appears in output).
# Includes <script>/<style> (executable noise), and a few SEO tags whose
# content isn't meant for human reading.
_SKIP_CONTENT_TAGS = {"script", "style", "noscript", "template", "head"}

# Tags that introduce a paragraph break in the text output. Without these,
# the rendered page reads as one giant blob.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "nav", "main", "aside",
    "blockquote", "pre", "ul", "ol", "table", "tbody", "thead",
}


class _TextExtractor(HTMLParser):
    """Walk the DOM, collecting visible text + extracted hrefs.

    State machine:
      - ``_skip_depth`` increments inside ``<script>`` etc.; we drop
        all data tokens while it's positive.
      - ``_chunks`` accumulates visible text segments separated by
        block-tag newlines.
      - ``_links`` collects ``(href, anchor_text)`` pairs.

    We're permissive about malformed HTML (the parser already handles
    missing close tags) but we don't try to repair broken nesting
    beyond what stdlib does for free. Garbage-in, partial-out is fine
    here — perfect HTML extraction isn't the goal, useful text is.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []
        self._links: list[tuple[str, str]] = []
        self._current_link_href: str | None = None
        self._current_link_text: list[str] = []
        # Title is rendered separately so we can surface it as the
        # ``data["title"]`` field even if the body extraction is large.
        self._in_title = False
        self._title_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        if tag_l in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if tag_l == "title":
            self._in_title = True
            return
        if tag_l in _BLOCK_TAGS:
            self._chunks.append("\n")
        if tag_l == "a":
            href = next((v for k, v in attrs if k.lower() == "href" and v), None)
            if href:
                self._current_link_href = href
                self._current_link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in _SKIP_CONTENT_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag_l == "title":
            self._in_title = False
            return
        if tag_l in _BLOCK_TAGS:
            self._chunks.append("\n")
        if tag_l == "a" and self._current_link_href is not None:
            anchor = "".join(self._current_link_text).strip()
            self._links.append((self._current_link_href, anchor[:200]))
            self._current_link_href = None
            self._current_link_text = []

    def handle_data(self, data: str) -> None:
        # ``<title>`` lives inside ``<head>``, and ``head`` is in
        # ``_SKIP_CONTENT_TAGS`` — so by the time we're parsing title
        # text we ALSO have ``_skip_depth > 0``. Check the title flag
        # FIRST so that title text still gets captured even though we'd
        # otherwise be skipping head content.
        if self._in_title:
            self._title_chunks.append(data)
            return
        if self._skip_depth > 0:
            return
        self._chunks.append(data)
        if self._current_link_href is not None:
            self._current_link_text.append(data)

    def get_text(self) -> str:
        """Collapsed text: per-line whitespace squeezed, blank-line gaps
        normalized to a single blank line."""
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.split("\n")]
        # Drop empty runs > 1: preserve paragraph breaks but compress any
        # 5-blank-line gaps that block-tag overuse can produce.
        out: list[str] = []
        prev_blank = False
        for line in lines:
            if not line:
                if prev_blank:
                    continue
                prev_blank = True
                out.append("")
                continue
            prev_blank = False
            out.append(line)
        return "\n".join(out).strip()

    def get_title(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._title_chunks)).strip()

    def get_links(self) -> list[dict[str, str]]:
        return [{"href": h, "text": t} for h, t in self._links]


def _resolve_links(base_url: str, links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Resolve relative ``href`` values against the page URL so the model
    sees absolute links it can pass back to ``fetch`` / ``browser``.

    Errors during urljoin (rare; malformed base URL) leave the original
    href in place — better than dropping the link entirely.
    """
    out: list[dict[str, str]] = []
    for link in links:
        href = link.get("href", "")
        try:
            abs_href = urllib.parse.urljoin(base_url, href)
        except Exception:  # noqa: BLE001
            abs_href = href
        out.append({"href": abs_href, "text": link.get("text", "")})
    return out


def _browser(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = payload.get("url") or payload.get("input")
    if not isinstance(url, str) or not url:
        return ToolResult(ok=False, error="url is required")

    host_check = validate_user_host(url)
    if not host_check.ok:
        ctx.audit("browser.blocked",
                  {"url": url, "reason": host_check.reason})
        return ToolResult(ok=False, error=f"BLOCKED: {host_check.reason}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.5",
    }
    req = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=BROWSER_TIMEOUT_S) as resp:
            content_type = resp.headers.get("Content-Type", "")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_BODY_BYTES:
                    break
            raw = b"".join(chunks)
            status = getattr(resp, "status", 200)
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        return ToolResult(
            ok=False,
            error=f"HTTP {exc.code}: {exc.reason}",
            data={"status": exc.code, "url": url},
        )
    except urllib.error.URLError as exc:
        return ToolResult(ok=False, error=f"network error: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    # Force-decode regardless of declared charset — html.parser is
    # tolerant. errors='replace' so a single bad byte doesn't fail the
    # whole extract.
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        body = raw.decode("utf-8", errors="replace")

    # If the response is plainly not HTML, fall back to text-passthrough
    # so the model gets SOMETHING useful (e.g. text/plain endpoints
    # served without HTML wrap).
    is_html = "html" in content_type.lower() or body.lstrip().startswith("<")
    if not is_html:
        text = body[:MAX_OUTPUT_CHARS]
        injection = detect_prompt_injection(text)
        if injection and injection.severity in ("definite", "likely"):
            ctx.audit("browser.prompt_injection", {
                "url": url, "severity": injection.severity,
                "pattern": injection.pattern,
            })
            return ToolResult(
                ok=False,
                error=(f"PROMPT INJECTION HALT [{injection.severity}]: "
                       f"{injection.pattern}"),
            )
        return ToolResult(
            ok=200 <= status < 400,
            output=text,
            data={
                "status": status, "url": final_url, "title": "",
                "links": [], "is_html": False, "content_type": content_type,
            },
        )

    extractor = _TextExtractor()
    try:
        extractor.feed(body)
        extractor.close()
    except Exception as exc:  # noqa: BLE001
        # html.parser raises on truly malformed input only rarely; fall
        # back to the raw body slice rather than abort.
        return ToolResult(
            ok=False,
            error=f"html parse failed: {type(exc).__name__}: {exc}",
            output=body[:MAX_OUTPUT_CHARS],
        )

    text = extractor.get_text()
    title = extractor.get_title()
    links = _resolve_links(final_url, extractor.get_links()[:MAX_LINKS])
    truncated = len(text) > MAX_OUTPUT_CHARS
    if truncated:
        text = text[:MAX_OUTPUT_CHARS]

    # Injection scan applies to the EXTRACTED TEXT (visible to the agent)
    # not the raw HTML — same reasoning as fetch's scan.
    injection = detect_prompt_injection(text)
    if injection and injection.severity in ("definite", "likely"):
        ctx.audit("browser.prompt_injection", {
            "url": url, "severity": injection.severity,
            "pattern": injection.pattern,
        })
        return ToolResult(
            ok=False,
            error=(f"PROMPT INJECTION HALT [{injection.severity}]: "
                   f"{injection.pattern} — content from {url} contains "
                   f"text that may be trying to override the agent. "
                   f"Snippet: {injection.snippet}"),
        )

    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"text truncated at {MAX_OUTPUT_CHARS} chars; full body was "
            f"{total} bytes"
        )
    if injection:
        warnings.append(
            f"Suspicious content ({injection.severity}): {injection.pattern}"
        )

    # Build the output: title + extracted text + a compact link list.
    # The model can request a deeper read of any link via fetch / browser
    # without re-rendering this whole page.
    out_parts: list[str] = []
    if title:
        out_parts.append(f"# {title}")
        out_parts.append("")
    out_parts.append(text)
    if links:
        out_parts.append("")
        out_parts.append("## Links")
        for link in links:
            href = link["href"]
            txt = link["text"] or "(no anchor text)"
            out_parts.append(f"- [{txt}]({href})")

    return ToolResult(
        ok=200 <= status < 400,
        output="\n".join(out_parts),
        warnings=warnings,
        data={
            "status": status,
            "url": final_url,
            "title": title,
            "links": links,
            "is_html": True,
            "truncated": truncated,
            "raw_bytes": total,
        },
    )


register(ToolDescriptor(
    name="browser",
    description=(
        "Fetch a URL, parse the HTML, return the visible text + extracted "
        "link list. Strips <script>/<style>, collapses whitespace. NO "
        "JavaScript execution — JS-rendered content won't appear. SSRF-"
        "guarded; injection-scanned. Use `fetch` for raw HTTP bodies, "
        "this for human-readable web pages."
    ),
    privileged=True,
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
        },
        "required": ["url"],
    },
    handler=_browser,
))
