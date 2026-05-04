"""Command-line entry point.

Usage:
  ac                      # launch the TUI
  ac --mirror             # read-only follower of a primary `ac` in this dir
  ac --version            # print version
  ac --working-dir PATH   # set working dir before launching
  ac --debug              # show tracebacks on errors
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agentcommander import __version__


def _detect_program_folder() -> Path | None:
    """If cwd is the AgentCommander source repo root, return that path.

    Detected by: cwd resolves to the directory that contains both
    ``pyproject.toml`` AND ``src/agentcommander/__init__.py`` (the
    development install layout). For pip-installed packages this returns
    ``None`` because site-packages won't have ``pyproject.toml`` next to
    the package.
    """
    try:
        import agentcommander as _ac
        pkg_init = Path(_ac.__file__).resolve()
    except Exception:  # noqa: BLE001
        return None
    candidate = pkg_init.parent.parent.parent
    if not (candidate / "pyproject.toml").exists():
        return None
    if not (candidate / "src" / "agentcommander" / "__init__.py").exists():
        return None
    if Path(os.getcwd()).resolve() == candidate.resolve():
        return candidate.resolve()
    return None


def _refuse_to_run_in_program_folder(repo_root: Path) -> None:
    """Print a loud red banner and exit. Called when cwd is the source repo
    root — running there pollutes the tree with ``.agentcommander/``,
    ``logs/``, and tool-created files, and breaks the project-local DB
    model. The fix is to run from a separate working directory."""
    bar = "═" * 70
    red = "\x1b[1;31m"
    cyan = "\x1b[1;36m"
    reset = "\x1b[0m"
    sys.stderr.write("\n")
    sys.stderr.write(f"{red}{bar}{reset}\n")
    sys.stderr.write(f"{red}  REFUSING TO RUN IN THE AGENTCOMMANDER SOURCE DIRECTORY{reset}\n")
    sys.stderr.write(f"{red}{bar}{reset}\n\n")
    sys.stderr.write(f"  cwd: {Path.cwd()}\n\n")
    sys.stderr.write(
        "  Running ac in the source folder pollutes the repo with\n"
        "  .agentcommander/ DB files, logs/, and any tool-created files,\n"
        "  and breaks the project-local-DB model (each working directory\n"
        "  gets its own state).\n\n"
        "  Move to a separate working directory first:\n\n"
    )
    sys.stderr.write(f"      {cyan}cd AgentTesting{reset}\n")
    sys.stderr.write(f"      {cyan}./ac.bat{reset}\n\n")
    sys.stderr.write(
        f"  Or use {cyan}ac --working-dir <path>{reset} from elsewhere.\n\n"
    )
    sys.exit(2)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ac",
        description="AgentCommander — local multi-agent LLM orchestration CLI",
    )
    p.add_argument("--version", action="version", version=f"AgentCommander {__version__}")
    p.add_argument("--working-dir", "-C", help="Set the working directory at launch")
    p.add_argument("--debug", action="store_true", help="Print tracebacks on errors")
    p.add_argument(
        "--mirror",
        action="store_true",
        help="Run as a read-only follower of a primary `ac` in this project. "
             "Skips the single-instance lock and opens the DB read-only, so "
             "it coexists with — or starts before — a primary process. "
             "Only /exit and /quit are accepted as input.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Refuse to run in the AgentCommander source folder. The DB lives at
    # <cwd>/.agentcommander/ regardless of mode (primary or mirror) and
    # regardless of --working-dir, so running here pollutes the source
    # repo with DB files, logs/, and tool-created scratch.
    repo_root = _detect_program_folder()
    if repo_root is not None:
        _refuse_to_run_in_program_folder(repo_root)

    # ── Mirror mode: bypasses init_db entirely; mirror.py opens RO itself.
    # Writing config (e.g. --working-dir) is meaningless without a primary,
    # so reject that combination explicitly.
    if args.mirror:
        if args.working_dir:
            print("error: --mirror and --working-dir can't be combined "
                  "(mirror is read-only). Set --working-dir on the primary.",
                  file=sys.stderr)
            return 2
        from agentcommander.tui.mirror import run_mirror
        try:
            return run_mirror()
        except Exception as exc:  # noqa: BLE001
            if args.debug:
                raise
            print(f"\nmirror failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1

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
            "different working directory (each project has its own DB). "
            "To watch the running process, start `ac --mirror` instead.",
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
