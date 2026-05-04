"""HTTP tool — structured REST/JSON API caller.

Complements the existing ``fetch`` (web_tool) which is text-body oriented
for scraping. ``http_request`` is API-oriented:

  - Defaults Content-Type to ``application/json``
  - Serializes ``body`` (dict/list) to JSON automatically
  - Attempts to parse the response body as JSON, surfacing it in
    ``ToolResult.data`` so the orchestrator can branch on structured
    fields without re-parsing
  - Falls back to text body when the response isn't JSON
  - Same SSRF guard (``validate_user_host``) — calls into private IP
    space / cloud metadata endpoints are still blocked
  - Same prompt-injection scan as ``fetch`` so a returned JSON value
    smuggling a "ignore previous instructions" string can't slip past

The injection scan applies only to the TEXT view of the response so a
legitimate JSON document doesn't trigger on technical content like
``"role": "system"`` (which would otherwise look injection-shaped).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from agentcommander.safety.host_validator import validate_user_host
from agentcommander.safety.prompt_injection import detect_prompt_injection
from agentcommander.tools.dispatcher import register
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult


HTTP_TIMEOUT_S = 30.0
MAX_BODY_BYTES = 5_000_000  # APIs return more verbose payloads than scraping
USER_AGENT = "AgentCommander/0.1 (+http_request)"

# Methods accepted by the dispatcher schema. Mirrors fetch but re-listed
# locally so the two tools' input schemas stay independent.
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _http_request(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = payload.get("url")
    method = (payload.get("method") or "POST").upper()
    headers = payload.get("headers") or {}
    body = payload.get("body")
    json_body = payload.get("json")

    if not isinstance(url, str) or not url:
        return ToolResult(ok=False, error="url is required")
    if method not in _METHODS:
        return ToolResult(ok=False, error=f"unsupported method: {method}")

    # SSRF guard. validate_user_host rejects loopback, link-local, and the
    # cloud-metadata endpoint — same constraints fetch enforces.
    host_check = validate_user_host(url)
    if not host_check.ok:
        ctx.audit("http_request.blocked",
                  {"url": url, "reason": host_check.reason})
        return ToolResult(ok=False, error=f"BLOCKED: {host_check.reason}")

    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, */*;q=0.5",
    }
    if isinstance(headers, dict):
        request_headers.update({str(k): str(v) for k, v in headers.items()})

    # Body resolution: ``json`` (auto-serialize) wins over ``body`` (raw)
    # so callers can pass structured data without remembering to set
    # Content-Type or json.dumps it themselves. If both are present the
    # caller is being inconsistent; surface that instead of silently
    # picking one.
    if json_body is not None and body is not None:
        return ToolResult(ok=False,
                          error="provide either `body` or `json`, not both")

    data: bytes | None = None
    if json_body is not None and method != "GET":
        try:
            data = json.dumps(json_body).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return ToolResult(ok=False,
                              error=f"json body not serializable: {exc}")
        request_headers.setdefault("Content-Type", "application/json")
    elif body is not None and method != "GET":
        if isinstance(body, str):
            data = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            return ToolResult(ok=False,
                              error="body must be a string or bytes "
                                    "(use `json` for dict/list payloads)")

    req = urllib.request.Request(url=url, data=data, method=method,
                                 headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            content_type = resp.headers.get("Content-Type", "")
            chunks: list[bytes] = []
            total = 0
            truncated = False
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_BODY_BYTES:
                    truncated = True
                    break
            raw = b"".join(chunks)
            status = getattr(resp, "status", 200)
            response_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")[:4000]
        except Exception:  # noqa: BLE001
            pass
        # Surface error-body parsed-JSON when applicable so the model can
        # see structured error responses (the common case for REST APIs).
        err_data: dict[str, Any] | None = None
        try:
            parsed_err = json.loads(body_text)
            if isinstance(parsed_err, (dict, list)):
                err_data = {"status": exc.code, "json": parsed_err}
        except ValueError:
            err_data = {"status": exc.code}
        return ToolResult(
            ok=False,
            error=f"HTTP {exc.code}: {exc.reason}",
            output=body_text,
            data=err_data,
        )
    except urllib.error.URLError as exc:
        return ToolResult(ok=False, error=f"network error: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    # JSON-ness is decided by Content-Type first (authoritative), then
    # by a peek attempt (some APIs send ``text/plain`` for JSON bodies).
    parsed: Any = None
    looks_jsonish = "json" in content_type.lower()
    if looks_jsonish or (text and text.lstrip()[:1] in ("{", "[")):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None

    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"response truncated at {MAX_BODY_BYTES // 1_000_000} MB; "
            "tail data not delivered"
        )

    # Injection scan: only against the text view, only when the response
    # isn't JSON. JSON APIs returning legitimate fields like
    # {"role": "system"} would otherwise raise false positives.
    if parsed is None:
        injection = detect_prompt_injection(text)
        if injection and injection.severity in ("definite", "likely"):
            ctx.audit("http_request.prompt_injection", {
                "url": url,
                "severity": injection.severity,
                "pattern": injection.pattern,
            })
            return ToolResult(
                ok=False,
                error=(f"PROMPT INJECTION HALT [{injection.severity}]: "
                       f"{injection.pattern} — content from {url} contains "
                       f"text that may be trying to override the agent. "
                       f"Snippet: {injection.snippet}"),
            )
        if injection:
            warnings.append(
                f"Suspicious content ({injection.severity}): {injection.pattern}"
            )

    data_out: dict[str, Any] = {
        "status": status,
        "content_type": content_type,
        "bytes": total,
        "truncated": truncated,
        "headers": response_headers,
    }
    if parsed is not None:
        data_out["json"] = parsed

    # Output is the parsed-JSON pretty-printed (small enough for a model
    # to read) when parseable, else the raw text. Pretty-print uses
    # 2-space indent and sorts dict keys for stable diffs.
    if parsed is not None:
        try:
            output_text = json.dumps(parsed, indent=2, sort_keys=True,
                                     default=str)[:8000]
        except (TypeError, ValueError):
            output_text = text[:8000]
    else:
        output_text = text[:8000]

    return ToolResult(
        ok=200 <= status < 400,
        output=output_text,
        warnings=warnings,
        data=data_out,
    )


register(ToolDescriptor(
    name="http_request",
    description=(
        "Structured HTTP/REST API call. Sends JSON via `json` (or raw "
        "string via `body`); auto-parses JSON responses into structured "
        "data. SSRF-guarded; injection-scanned for non-JSON responses."
    ),
    privileged=True,
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string", "enum": list(_METHODS)},
            "headers": {"type": "object",
                        "additionalProperties": {"type": "string"}},
            "body": {"type": "string"},
            "json": {},  # any JSON-serializable shape (dict/list/scalar)
        },
        "required": ["url"],
    },
    handler=_http_request,
))
