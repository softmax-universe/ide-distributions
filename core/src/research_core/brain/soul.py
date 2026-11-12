"""The prepare hook (IDE_BRAIN=1): write the learning section into the profile's SOUL.md and
mirror the Coworld seed skills into the profile's skills dir. Off: restore the plain SOUL when
the brain one is installed (the flag was turned off); otherwise do nothing at all."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from research_core import brain, layout
from research_core.brain import _fs, seeds
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
        ws = layout.workspace_root(env)
        actions += [
            f"brain: seed skill visible to Hermes: {path}"
            for path in seeds.mirror_to_profile(ws, home)
        ]
        wanted = build.render(role, brain=True)
        if current != wanted:
            _fs.write(home, soul, wanted)
            actions.append("brain: SOUL.md carries the learning section")
    except _fs.UnsafePath as error:
        actions.append(f"brain: refused, {error}")
    return actions
