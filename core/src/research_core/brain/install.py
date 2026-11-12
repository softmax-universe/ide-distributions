"""Install the brain template into the workspace and compose the SOUL. Opt-in: IDE_BRAIN=1.

Idempotent and non-destructive: a file that already exists is never touched. Stdlib only.
Off (IDE_BRAIN unset or not "1") every function here is a no-op and the SOUL is unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from research_core import layout
from research_core.prompts import build

TEMPLATE = Path(__file__).resolve().parent / "template"
FLAG = "IDE_BRAIN"


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return (os.environ if env is None else env).get(FLAG, "") == "1"


def install(workspace: Path, template: Path = TEMPLATE) -> list[str]:
    """Copy the template files that do not exist yet. Returns the relative paths created."""
    created: list[str] = []
    for src in sorted(template.rglob("*")):
        rel = src.relative_to(template)
        dst = Path(workspace) / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            created.append(rel.as_posix())
    return created


def apply(profile_home: Path, role: str, env: Mapping[str, str] | None = None) -> list[str]:
    """The prepare hook. With IDE_BRAIN=1: install the template into the workspace and write the
    profile's installed SOUL.md with the learning section. Otherwise do nothing at all."""
    env = os.environ if env is None else env
    if not enabled(env):
        return []
    actions = [f"brain: created {name}" for name in install(layout.workspace_root(env))]
    soul = Path(profile_home) / "SOUL.md"
    wanted = build.render(role, brain=True)
    if not soul.exists() or soul.read_text(encoding="utf-8") != wanted:
        soul.write_text(wanted, encoding="utf-8")
        actions.append("brain: SOUL.md carries the learning section")
    return actions
