"""Dangerous-command pattern defense.

Ported from EngineCommander/src/main/utils/dangerous-patterns.ts.

Shared between the `execute` tool, the `start_process` tool, and the
execute-guards pipeline stage. Defends against the common ways an
LLM-generated command could nuke the host:

  - Fork bombs (:(){ :|: & };:)
  - Disk fillers (yes, cat /dev/zero > file)
  - Destructive file ops (rm -rf /, mkfs, dd, chmod 777 /)
  - Credential exfiltration (~/.ssh/id_*, /etc/shadow, AWS/GCP creds)
  - Persistence writes (.bashrc, /etc/cron*, systemd, crontab)
  - Privilege escalation (sudo, su root, setuid)
  - Curl-to-shell (curl ... | sh, bash <(curl ...))
  - Reverse shells (nc -e, /dev/tcp, socket+pty.spawn)
  - System shutdown/reboot

Strategy: language-agnostic regex matchers with a second pass that scans
shell strings embedded in Python/Node/Ruby code (so a Python script that
calls os.system("rm -rf /") gets caught too). First match wins.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DangerousMatch:
    category: str
    reason: str


_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # Fork bombs
    ("fork_bomb", "classic fork bomb (:(){ :|: & };:)",
     re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:[^}]*&[^}]*}\s*;?\s*:")),
    ("fork_bomb", "recursive self-invoking function with backgrounding",
     re.compile(r"\b([a-zA-Z_]\w*)\s*\(\s*\)\s*\{[^}]*\1[^}]*&[^}]*}\s*;?\s*\1")),
    ("fork_bomb", "bash/sh process explosion via eval/exec loop",
     re.compile(r"while\s+true[^;]*;\s*do[^;]*(?:fork|bash|sh|\$0)[^;]*&\s*done", re.IGNORECASE)),
    ("fork_bomb", "python os.fork loop",
     re.compile(r"while\s+True[^:]*:\s*(?:[^\n]*\n\s*)*[^\n]*os\.fork\s*\(")),

    # Destructive file ops
    ("destructive", "rm -rf / (or variants)",
     re.compile(r"\brm\s+(?:-[a-zA-Z]*[rRfF][a-zA-Z]*\s+|--recursive\b[^;]*--force\b|--force\b[^;]*--recursive\b|--no-preserve-root\b)[^;|&]*(?:\s|/|\$|~|\*)", re.IGNORECASE)),
    ("destructive", "rm -rf of common system paths",
     re.compile(r"\brm\s+[^;|&]*(?:/\*|\s/\s|\s/$|\s/bin|\s/etc|\s/usr|\s/var|\s/home|\s~/?|\s\$HOME)")),
    ("destructive", "mkfs — filesystem format",
     re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b", re.IGNORECASE)),
    ("destructive", "dd writing to device or overwriting disk",
     re.compile(r"\bdd\s+[^;|&]*(?:if=/dev/(?:zero|urandom|random)|of=/dev/(?:sd|nvme|hd|xvd|vd))", re.IGNORECASE)),
    ("destructive", "write to raw block device",
     re.compile(r">\s*/dev/(?:sd[a-z]|nvme\d|hd[a-z]|xvd[a-z]|vd[a-z]|null\s*;\s*\w)", re.IGNORECASE)),
    ("destructive", "chmod 777 on system path",
     re.compile(r"\bchmod\s+(?:-R\s+)?777\s+(?:/|\s/|~/?)")),
    ("destructive", "chown of system path",
     re.compile(r"\bchown\s+[^;|&]*\s+/(?:etc|usr|bin|var|root)\b")),
    ("destructive", "shred/wipe on system path",
     re.compile(r"\b(?:shred|wipe|scrub)\b[^;|&]*\s+(?:/|~|\$HOME)")),

    # Disk fill
    ("disk_fill", "infinite yes pipe",
     re.compile(r"\byes\b[^;|&]*(?:>\s*|\|\s*tee|\|\s*dd)")),
    ("disk_fill", "cat /dev/zero or /dev/urandom to file",
     re.compile(r"\b(?:cat|dd\s+if=)\s*/dev/(?:zero|urandom|random)\b[^;|&]*>")),
    ("disk_fill", "fallocate large file",
     re.compile(r"\bfallocate\s+-l\s+\d+[TGP]", re.IGNORECASE)),

    # Credential exfiltration
    ("exfil", "reading SSH private keys",
     re.compile(r"(?:cat|less|more|tail|head|cp|mv|base64|xxd|od)\s+[^;|&]*~?/?\.ssh/(?:id_[a-z0-9]+|authorized_keys|known_hosts)(?!\.pub)")),
    ("exfil", "reading /etc/shadow or /etc/sudoers",
     re.compile(r"(?:cat|less|more|tail|head|cp|mv|base64|xxd)\s+[^;|&]*/etc/(?:shadow|sudoers)\b")),
    ("exfil", "reading password-store or cred files",
     re.compile(r"/etc/(?:passwd|gshadow)\s*(?:\||>|\&)")),
    ("exfil", "reading AWS/GCP/Azure credentials",
     re.compile(r"(?:cat|less|cp|base64)\s+[^;|&]*(?:~?/?\.aws/credentials|~?/?\.config/gcloud|~?/?\.azure/)")),

    # Persistence
    ("persistence", "write to shell rc files",
     re.compile(r"(?:>>?\s*|tee\s+(?:-a\s+)?)~?/?\.(?:bashrc|zshrc|profile|bash_profile|bash_login|zprofile)\b")),
    ("persistence", "writing to cron directories",
     re.compile(r"(?:>>?\s*|tee\s+|cp\s+[^;|&]+\s+)/(?:etc/cron\.(?:d|hourly|daily|weekly|monthly)|var/spool/cron)")),
    ("persistence", "crontab install",
     re.compile(r"\bcrontab\s+(?:-[a-zA-Z]*(?:r|e|l)|-u\s+\S+\s+)")),
    ("persistence", "systemd unit install",
     re.compile(r"(?:>>?\s*|tee\s+|cp\s+[^;|&]+\s+|ln\s+-s\s+[^;|&]+\s+)/(?:etc|lib|usr/lib)/systemd/")),
    ("persistence", "authorized_keys append",
     re.compile(r">>\s*~?/?\.ssh/authorized_keys")),

    # Privilege escalation
    ("privesc", "sudo command", re.compile(r"\bsudo\s+(?!-n\s+-l\b)")),
    ("privesc", "su to root", re.compile(r"\bsu\s+(?:-\s+)?(?:root|-)\b")),
    ("privesc", "setuid chmod", re.compile(r"\bchmod\s+[+u]s\s+")),

    # Curl-to-shell
    ("curl_to_shell", "curl/wget piped to shell",
     re.compile(r"\b(?:curl|wget|fetch)\b[^;|&]*\|\s*(?:bash|sh|zsh|ksh|dash|python[23]?|perl|ruby|node)\b", re.IGNORECASE)),
    ("curl_to_shell", "bash process substitution from network fetch",
     re.compile(r"\b(?:bash|sh|zsh|python[23]?|perl)\s+<\(\s*(?:curl|wget|fetch)\b", re.IGNORECASE)),
    ("curl_to_shell", "eval/exec of remote content",
     re.compile(r"\b(?:eval|exec)\s*[\"'`(]\s*(?:\$\(|`)?\s*(?:curl|wget|fetch)\b", re.IGNORECASE)),
    ("curl_to_shell", "bash -c with remote fetch substitution",
     re.compile(r"\bbash\s+-c\s+[\"'][^\"']*\$\(\s*(?:curl|wget)\b", re.IGNORECASE)),

    # Reverse shells
    ("reverse_shell", "netcat reverse shell",
     re.compile(r"\bnc\b[^;|&]*\s(?:-[a-zA-Z]*e|--exec)\b")),
    ("reverse_shell", "bash /dev/tcp reverse shell",
     re.compile(r"/dev/(?:tcp|udp)/[0-9a-zA-Z.\-]+/\d+")),
    ("reverse_shell", "python reverse shell pattern",
     re.compile(r"socket\.socket\s*\([^)]*\)[\s\S]{0,200}(?:connect|dup2)[\s\S]{0,200}(?:/bin/(?:bash|sh)|pty\.spawn)")),
    ("reverse_shell", "perl reverse shell pattern",
     re.compile(r"perl\s+-e\s+[\"']use\s+Socket[^\"']*(?:connect|exec\s*[\"']/bin)")),
    ("reverse_shell", "php reverse shell pattern",
     re.compile(r"\bphp\s+-r\s+[\"'][^\"']*fsockopen\b")),

    # Shutdown / reboot
    ("shutdown", "system shutdown/reboot",
     re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b(?:\s+(?:-[a-zA-Z]+|\d|now))")),
    ("shutdown", "init runlevel change", re.compile(r"\binit\s+[06]\b")),
    ("shutdown", "systemctl halt/reboot/poweroff",
     re.compile(r"\bsystemctl\s+(?:halt|reboot|poweroff|rescue|emergency)\b")),
]


_SHELL_CALL_RE = re.compile(
    r"(?:os\.system|os\.popen|subprocess\.(?:run|call|Popen|check_output|check_call)"
    r"|commands\.getoutput|child_process\.(?:exec|execSync|spawn|spawnSync)"
    r"|require\(\s*['\"]child_process['\"]\s*\)\.\w+|shell_exec|`)"
    r"\s*\(?\s*(['\"`])([\s\S]*?)\1"
)


def _extract_shell_calls(code: str) -> list[str]:
    return [m.group(2) for m in _SHELL_CALL_RE.finditer(code) if m.group(2)]


def scan_dangerous_code(code: str) -> DangerousMatch | None:
    """Scan a code string (bash, python, js, etc.) for dangerous patterns.

    Two passes:
      1. Direct: regex against the raw code.
      2. Embedded: extract string literals from os.system/subprocess.run/
         child_process.exec/etc. and regex those individually.

    Returns the first match, or None if clean.
    """
    if not code or not isinstance(code, str):
        return None
    for category, reason, pattern in _PATTERNS:
        if pattern.search(code):
            return DangerousMatch(category=category, reason=reason)
    for shell_string in _extract_shell_calls(code):
        for category, reason, pattern in _PATTERNS:
            if pattern.search(shell_string):
                return DangerousMatch(category=category, reason=f"{reason} (inside shell call)")
    return None


def scan_dangerous_command(command: str) -> DangerousMatch | None:
    """Scan a plain shell command (as passed to start_process). One pass only."""
    if not command or not isinstance(command, str):
        return None
    for category, reason, pattern in _PATTERNS:
        if pattern.search(command):
            return DangerousMatch(category=category, reason=reason)
    return None
