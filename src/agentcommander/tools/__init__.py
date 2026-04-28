"""Tool layer — pluggable action handlers.

Each verb the orchestrator can dispatch (read_file, write_file, execute,
fetch, start_process, ...) is a tool. Builtins are auto-registered on import
of `tools.bootstrap`. User plugins can self-register the same way.

  - types.py: ToolDescriptor, ToolContext, ToolResult, handler signature
  - dispatcher.py: registry + invoke() entry point
  - bootstrap.py: import all builtins so they register
  - file_tool.py / code_tool.py / web_tool.py / process_tool.py / ...
"""

from agentcommander.tools.dispatcher import (
    bootstrap_builtins,
    get_tool,
    invoke,
    list_tools,
    register,
    register_external,
    unregister,
)
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolHandler, ToolResult

__all__ = [
    "ToolContext",
    "ToolDescriptor",
    "ToolHandler",
    "ToolResult",
    "bootstrap_builtins",
    "get_tool",
    "invoke",
    "list_tools",
    "register",
    "register_external",
    "unregister",
]
