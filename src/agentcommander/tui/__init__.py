"""Terminal UI — mimics Claude Code's linux console look.

Pure stdlib (ANSI escapes + input()). No prompt_toolkit, no rich, no curses.

  - ansi.py:    ANSI escape sequences, color helpers, terminal size
  - render.py:  message renderers (user / assistant / tool / guard / status)
  - commands.py: slash-command registry (/help, /providers, /roles, /typecast, ...)
  - app.py:     main REPL loop (read → run pipeline → render events)
"""

from agentcommander.tui.app import run_tui
from agentcommander.tui.commands import COMMANDS, SlashCommand

__all__ = ["COMMANDS", "SlashCommand", "run_tui"]
