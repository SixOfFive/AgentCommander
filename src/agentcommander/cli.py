"""Command-line entry point.

Usage:
  ac                      # launch the TUI
  ac --version            # print version
  ac --working-dir PATH   # set working dir before launching
  ac --debug              # show tracebacks on errors
"""
from __future__ import annotations

import argparse
import sys

from agentcommander import __version__


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ac",
        description="AgentCommander — local multi-agent LLM orchestration CLI",
    )
    p.add_argument("--version", action="version", version=f"AgentCommander {__version__}")
    p.add_argument("--working-dir", "-C", help="Set the working directory at launch")
    p.add_argument("--debug", action="store_true", help="Print tracebacks on errors")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Lazy imports so `--version` / `--help` are instant.
    from agentcommander.db.connection import init_db
    from agentcommander.db.repos import set_config
    from agentcommander.tui.app import run_tui

    init_db()
    if args.working_dir:
        import os
        if not os.path.isdir(args.working_dir):
            print(f"error: not a directory: {args.working_dir}", file=sys.stderr)
            return 2
        set_config("working_directory", os.path.abspath(args.working_dir))

    return run_tui()


if __name__ == "__main__":
    sys.exit(main())
