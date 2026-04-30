"""Role-prompt loader.

Reads system-prompt markdown files from `resources/prompts/{ROLE}.md`.
Cached after first load. Source: copied verbatim from
`EngineCommander/resources/prompts/`.

If a prompt file is missing for a role, returns a sensible fallback so the
engine doesn't crash — the role just operates with a generic prompt and the
guards still apply.
"""
from __future__ import annotations

from pathlib import Path

from agentcommander.types import Role

_ROLE_TO_FILE: dict[Role, str] = {
    Role.ROUTER: "ROUTER.md",
    Role.ORCHESTRATOR: "ORCHESTRATOR.md",
    Role.PLANNER: "PLANNER.md",
    Role.CODER: "CODER.md",
    Role.REVIEWER: "REVIEWER.md",
    Role.SUMMARIZER: "SUMMARIZER.md",
    Role.VISION: "VISION.md",
    Role.AUDIO: "AUDIO.md",
    Role.IMAGE_GEN: "IMAGE_GEN.md",
    Role.ARCHITECT: "ARCHITECT.md",
    Role.CRITIC: "CRITIC.md",
    Role.TESTER: "TESTER.md",
    Role.DEBUGGER: "DEBUGGER.md",
    Role.RESEARCHER: "RESEARCHER.md",
    Role.REFACTORER: "REFACTORER.md",
    Role.TRANSLATOR: "TRANSLATOR.md",
    Role.DATA_ANALYST: "DATA_ANALYST.md",
    Role.PREFLIGHT: "PREFLIGHT.md",
    Role.POSTMORTEM: "POSTMORTEM.md",
}

_cache: dict[Role, str] = {}


def _prompt_dir() -> Path:
    """Locate the prompts directory.

    Search order: env override → installed-package neighbor → repo root.
    Modular: drop a `.md` here and `get_role_prompt` picks it up.
    """
    import os

    env = os.environ.get("AGENTCOMMANDER_PROMPTS_DIR")
    if env:
        return Path(env)

    # Installed: alongside the package
    pkg_resources = Path(__file__).resolve().parent.parent.parent.parent / "resources" / "prompts"
    if pkg_resources.is_dir():
        return pkg_resources

    # Repo dev: walk up from this file
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "resources" / "prompts"
        if candidate.is_dir():
            return candidate
    # Fallback that won't exist — get_role_prompt's missing-file path handles it
    return Path("resources") / "prompts"


def _fallback_prompt(role: Role) -> str:
    return (
        f"You are the {role.value} agent in a multi-agent pipeline. "
        f"Respond concisely with output appropriate to your role."
    )


def get_role_prompt(role: Role) -> str:
    """Return the system prompt for a role. Cached per role.

    Special case: the orchestrator prompt has a "Workflow Recipes" section
    that points at ``RECIPES.md`` ("loaded from RECIPES.md"). The loader
    doesn't process include directives, so we explicitly read RECIPES.md
    and concatenate its body onto the orchestrator prompt at first load.
    Without this the orchestrator's prompt promises content it never
    delivers — the recipes never reach the model.
    """
    if role in _cache:
        return _cache[role]
    filename = _ROLE_TO_FILE.get(role)
    if not filename:
        content = _fallback_prompt(role)
    else:
        path = _prompt_dir() / filename
        try:
            content = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            content = _fallback_prompt(role)

    if role is Role.ORCHESTRATOR:
        recipes_path = _prompt_dir() / "RECIPES.md"
        try:
            recipes = recipes_path.read_text(encoding="utf-8")
            # Strip RECIPES.md's leading "# Workflow Recipes" H1 since the
            # orchestrator prompt already has its own "## Workflow Recipes"
            # header — keeping both produces a duplicate heading the model
            # has to skim past.
            stripped = recipes.lstrip()
            if stripped.startswith("# "):
                first_break = stripped.find("\n\n")
                if first_break != -1:
                    stripped = stripped[first_break + 2:]
            content = content.rstrip() + "\n\n" + stripped + "\n"
        except (FileNotFoundError, OSError):
            pass

    _cache[role] = content
    return content


def clear_prompt_cache() -> None:
    """Force a re-read on next access (useful for hot-edits during dev)."""
    _cache.clear()


def list_available_prompts() -> dict[Role, bool]:
    """For each role, True if a real prompt file exists, False if using fallback."""
    out: dict[Role, bool] = {}
    pdir = _prompt_dir()
    for role, fname in _ROLE_TO_FILE.items():
        out[role] = (pdir / fname).is_file()
    return out
