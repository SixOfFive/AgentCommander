"""Output guards — sanitize execution output before it enters the scratchpad.

Pure functions; no scratchpad mutation, no verdict object.

Ported verbatim from EngineCommander/src/main/orchestration/guards/output-guards.ts.
"""
from __future__ import annotations

import re

MAX_OUTPUT_LENGTH = 15_000

_ANSI_CSI_RX = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ANSI_OSC_RX = re.compile(r"\x1b\][^\x07]*\x07")
_ANSI_CHARSET_RX = re.compile(r"\x1b[()][AB012]")
_OTHER_CONTROL_RX = re.compile(r"[\x00-\x08\x0e-\x1f]")
_BIN_RUN_RX = re.compile(r"[^\x20-\x7e\n\r\t]{10,}")
_BASE64_RX = re.compile(r"(?:data:[a-z]+/[a-z]+;base64,)?[A-Za-z0-9+/=]{200,}")
_BLANK_LINES_RX = re.compile(r"\n{4,}")
_BLANK_3PLUS_RX = re.compile(r"\n{3,}")
_PROGRESS_DOWNLOAD_RX = re.compile(r"^\s*Downloading\s+\S+.*$", re.MULTILINE)
_PROGRESS_BAR_RX = re.compile(r"^\s*━+.*$", re.MULTILINE)
_PROGRESS_CLASSIC_RX = re.compile(r"^\s*\|[█░▒▓ ]+\|.*$", re.MULTILINE)
_NPM_WARN_RX = re.compile(r"^npm warn\b.*$", re.MULTILINE)
_DEPRECATION_RX = re.compile(r"^.*DeprecationWarning:.*$", re.MULTILINE)
_FUTURE_RX = re.compile(r"^.*FutureWarning:.*$", re.MULTILINE)
_PEND_DEPR_RX = re.compile(r"^.*PendingDeprecationWarning:.*$", re.MULTILINE)
_RUNTIME_RX = re.compile(r"^.*RuntimeWarning:.*$", re.MULTILINE)
_USER_WARN_RX = re.compile(r"^.*UserWarning:.*$", re.MULTILINE)
_WARNINGS_WARN_RX = re.compile(r"^\s*warnings\.warn\(.*$", re.MULTILINE)
_TRAILING_WS_RX = re.compile(r"\s+$")


def strip_ansi_codes(text: str) -> str:
    text = _ANSI_CSI_RX.sub("", text)
    text = _ANSI_OSC_RX.sub("", text)
    text = _ANSI_CHARSET_RX.sub("", text)
    return _OTHER_CONTROL_RX.sub("", text)


def strip_binary_content(text: str) -> str:
    if not text:
        return text
    non_printable = sum(1 for ch in text if not (ch in "\n\r\t" or " " <= ch <= "~"))
    if non_printable > len(text) * 0.2 and len(text) > 100:
        return "[Binary content stripped — output contained non-text data]"
    return _BIN_RUN_RX.sub("[binary data]", text)


def truncate_output(text: str) -> str:
    if len(text) <= MAX_OUTPUT_LENGTH:
        return text
    head_size = int(MAX_OUTPUT_LENGTH * 0.7)
    tail_size = int(MAX_OUTPUT_LENGTH * 0.25)
    head = text[:head_size]
    tail = text[-tail_size:]
    skipped = len(text) - head_size - tail_size
    return f"{head}\n\n... [{skipped} characters omitted] ...\n\n{tail}"


def strip_base64(text: str) -> str:
    return _BASE64_RX.sub("[base64 data stripped]", text)


def redact_secrets(text: str) -> str:
    text = re.sub(r"sk-[a-zA-Z0-9]{20,}", "sk-[REDACTED]", text)
    text = re.sub(r"sk-or-v1-[a-f0-9]{20,}", "sk-or-v1-[REDACTED]", text)
    text = re.sub(r"ghp_[a-zA-Z0-9]{36}", "ghp_[REDACTED]", text)
    text = re.sub(r"glpat-[a-zA-Z0-9\-_]{20,}", "glpat-[REDACTED]", text)
    text = re.sub(r"xox[bposa]-[a-zA-Z0-9\-]{10,}", "xox_-[REDACTED]", text)
    text = re.sub(r"AKIA[A-Z0-9]{16}", "AKIA[REDACTED]", text)
    text = re.sub(r"AIza[a-zA-Z0-9_\-]{35}", "AIza[REDACTED]", text)
    return re.sub(
        r"(password|passwd|pwd|secret|token)\s*[:=]\s*\S+",
        r"\1=[REDACTED]", text, flags=re.IGNORECASE,
    )


def normalize_whitespace(text: str) -> str:
    text = _BLANK_LINES_RX.sub("\n\n\n", text)
    text = "\n".join(_TRAILING_WS_RX.sub("", line) for line in text.split("\n"))
    return text.strip()


def strip_install_progress(text: str) -> str:
    text = _PROGRESS_DOWNLOAD_RX.sub("", text)
    text = _PROGRESS_BAR_RX.sub("", text)
    text = _PROGRESS_CLASSIC_RX.sub("", text)
    text = _NPM_WARN_RX.sub("", text)
    return _BLANK_3PLUS_RX.sub("\n\n", text)


def strip_warnings(text: str) -> str:
    text = _DEPRECATION_RX.sub("", text)
    text = _FUTURE_RX.sub("", text)
    text = _PEND_DEPR_RX.sub("", text)
    text = _RUNTIME_RX.sub("", text)
    text = _USER_WARN_RX.sub("", text)
    text = _WARNINGS_WARN_RX.sub("", text)
    return _BLANK_3PLUS_RX.sub("\n\n", text)


def sanitize_output(text: str) -> str:
    """Run all output sanitization in sequence. The engine calls this on
    every tool result before scratchpad insertion.
    """
    if not text:
        return text
    text = strip_ansi_codes(text)
    text = strip_binary_content(text)
    text = strip_base64(text)
    text = redact_secrets(text)
    text = strip_install_progress(text)
    text = strip_warnings(text)
    text = normalize_whitespace(text)
    text = truncate_output(text)
    return text
