"""Web fetch tool — SSRF-guarded + prompt-injection-scanned.

Used by the researcher / orchestrator to read external URLs. Output is
scanned for prompt-injection patterns; on a definite/likely match the tool
returns ok=False so the engine can halt and surface to the user.

Pure stdlib — `urllib.request` for the HTTP call.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agentcommander.safety.host_validator import validate_user_host
from agentcommander.safety.prompt_injection import detect_prompt_injection
from agentcommander.tools.dispatcher import register
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult

FETCH_TIMEOUT_S = 20.0
MAX_BODY_BYTES = 2_000_000
USER_AGENT = "AgentCommander/0.1 (+local CLI)"


def _fetch(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = payload.get("url") or payload.get("input")
    method = (payload.get("method") or "GET").upper()
    headers = payload.get("headers") or {}
    body = payload.get("body")

    if not isinstance(url, str) or not url:
        return ToolResult(ok=False, error="url is required")
    if method not in {"GET", "POST", "HEAD", "PUT", "DELETE", "PATCH"}:
        return ToolResult(ok=False, error=f"unsupported method: {method}")

    host_check = validate_user_host(url)
    if not host_check.ok:
        ctx.audit("fetch.blocked", {"url": url, "reason": host_check.reason})
        return ToolResult(ok=False, error=f"BLOCKED: {host_check.reason}")

    request_headers = {"User-Agent": USER_AGENT}
    if isinstance(headers, dict):
        request_headers.update({str(k): str(v) for k, v in headers.items()})

    data: bytes | None = None
    if body is not None and method != "GET":
        if isinstance(body, str):
            data = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            return ToolResult(ok=False, error="body must be a string or bytes")

    req = urllib.request.Request(url=url, data=data, method=method,
                                 headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
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
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")[:2000]
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(
            ok=False,
            error=f"HTTP {exc.code}: {exc.reason}",
            output=body_text,
            data={"status": exc.code},
        )
    except urllib.error.URLError as exc:
        return ToolResult(ok=False, error=f"network error: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    is_text = "text" in content_type or "json" in content_type or "xml" in content_type
    if is_text:
        try:
            payload_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            payload_text = raw.decode("utf-8", errors="replace")
    else:
        payload_text = f"[non-text response: {content_type or 'unknown'}, {total} bytes]"

    warnings: list[str] = []
    if is_text:
        injection = detect_prompt_injection(payload_text)
        if injection and injection.severity in ("definite", "likely"):
            ctx.audit("fetch.prompt_injection", {
                "url": url,
                "severity": injection.severity,
                "pattern": injection.pattern,
            })
            return ToolResult(
                ok=False,
                error=(f"PROMPT INJECTION HALT [{injection.severity}]: {injection.pattern} — "
                       f"content from {url} contains text that may be trying to override the agent. "
                       f"Snippet: {injection.snippet}"),
            )
        if injection:
            warnings.append(f"Suspicious content ({injection.severity}): {injection.pattern}")

    return ToolResult(
        ok=200 <= status < 400,
        output=payload_text,
        warnings=warnings,
        data={"status": status, "content_type": content_type, "bytes": total},
    )


register(ToolDescriptor(
    name="fetch",
    description="HTTP GET/POST/HEAD a URL. Body returned as text up to 2MB; SSRF-guarded; injection-scanned.",
    privileged=True,
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string", "enum": ["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH"]},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "body": {"type": "string"},
        },
        "required": ["url"],
    },
    handler=_fetch,
))
