"""The prepare hook (IDE_BRAIN=1): write the learning section into the profile's SOUL.md.

Off: restore the plain SOUL when the brain one is installed (the flag was turned
off); otherwise do nothing at all."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from research_core import brain
from research_core.prompts import build


def _write(path: Path, text: str) -> None:
    """Atomic: a temp file beside it, then ``os.replace``."""
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def apply(profile_home: Path, role: str, env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    soul = Path(profile_home) / "SOUL.md"
    current = soul.read_text(encoding="utf-8") if soul.is_file() else None
    if not brain.enabled(env):
        if current is not None and current == build.render(role, brain=True):
            _write(soul, build.render(role))
            return ["brain: flag off, plain SOUL.md restored"]
        return []
    wanted = build.render(role, brain=True)
    if current == wanted:
        return []
    _write(soul, wanted)
    return ["brain: SOUL.md carries the learning section"]
