"""Safety layer ported from EngineCommander.

Four modules, all pure Python:
  - dangerous_patterns: regex-based defense against destructive commands
  - sandbox: filesystem boundary + path-traversal/symlink-escape guard
  - host_validator: SSRF-safe URL/host check
  - prompt_injection: detect injection attempts in fetched content
"""

from agentcommander.safety.dangerous_patterns import (
    DangerousMatch,
    scan_dangerous_code,
    scan_dangerous_command,
)
from agentcommander.safety.host_validator import (
    HostCheck,
    validate_provider_host,
    validate_user_host,
)
from agentcommander.safety.prompt_injection import InjectionMatch, detect_prompt_injection
from agentcommander.safety.sandbox import (
    FilesystemSecurityError,
    is_path_within,
    is_valid_directory,
    require_working_directory,
    safe_path,
    scan_for_filesystem_risks,
    validate_file_access,
)

__all__ = [
    "DangerousMatch",
    "FilesystemSecurityError",
    "HostCheck",
    "InjectionMatch",
    "detect_prompt_injection",
    "is_path_within",
    "is_valid_directory",
    "require_working_directory",
    "safe_path",
    "scan_dangerous_code",
    "scan_dangerous_command",
    "scan_for_filesystem_risks",
    "validate_file_access",
    "validate_provider_host",
    "validate_user_host",
]
