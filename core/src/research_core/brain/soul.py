"""The prepare hook (IDE_BRAIN=1): write the learning section into the profile's SOUL.md.

Off: restore the plain SOUL when the brain one is installed (the flag was turned
off); otherwise do nothing at all."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from research_core import brain
from research_core.brain import _fs
from research_core.prompts import build


def apply(profile_home: Path, role: str, env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    home = Path(profile_home)
    soul = home / "SOUL.md"
    current = soul.read_text(encoding="utf-8") if soul.is_file() else None
    if not brain.enabled(env):
        if current is not None and current == build.render(role, brain=True):
            _fs.write(home, soul, build.render(role))
            return ["brain: flag off, plain SOUL.md restored"]
        return []
    actions: list[str] = []
    try:
        wanted = build.render(role, brain=True)
        if current != wanted:
            _fs.write(home, soul, wanted)
            actions.append("brain: SOUL.md carries the learning section")
    except _fs.UnsafePath as error:
        actions.append(f"brain: refused, {error}")
    return actions
