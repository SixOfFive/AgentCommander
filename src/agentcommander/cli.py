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
    from agentcommander.db.connection import init_db, DBAlreadyOpen
    from agentcommander.db.repos import set_config
    from agentcommander.tui.app import run_tui

    try:
        init_db()
    except DBAlreadyOpen as exc:
        # Single-instance lock: another ac.bat is using this DB. Friendly
        # message, no traceback. Exit code 2 is the "config / locked"
        # convention used elsewhere in this CLI.
        print(
            "\nAgentCommander is already running against this DB.",
            file=sys.stderr,
        )
        print(f"  {exc}", file=sys.stderr)
        print(
            "\nClose the other process and try again, or run from a "
            "different working directory (each project has its own DB).",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        if args.debug:
            raise
        print(f"\nfailed to open DB: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    if args.working_dir:
        import os
        if not os.path.isdir(args.working_dir):
            print(f"error: not a directory: {args.working_dir}", file=sys.stderr)
            return 2
        set_config("working_directory", os.path.abspath(args.working_dir))

    return run_tui()


if __name__ == "__main__":
    sys.exit(main())
