"""Install a Coworld's seed skills and how-to into its folder. Idempotent. Stdlib only.

    python -m research_core.brain seed <coworld-id> [--skills DIR] [--howto FILE]

--skills DIR holds ``<skill-name>/SKILL.md`` folders (the coworld's seed skills).
--howto FILE is the game's how-to text.

Writes only under ``<coworld folder>/skills/seed/`` and ``<coworld folder>/documents/howto.md``,
where the Coworld folder is ``<workspace>/<id with - as _>``. It never writes the agents' own
``skills/<name>/``. Each seeded skill gets a ``.seed`` marker holding the content
hash; unchanged content is skipped, a changed seed is refreshed, and a seed folder with no marker
(edited by hand) is left alone.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from research_core import layout

SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check(name: str, what: str) -> str:
    if not SAFE.match(name) or ".." in name:
        raise ValueError(f"unsafe {what} name: {name!r}")
    return name


def _root(workspace, coworld: str) -> Path:
    _check(coworld, "coworld")
    return Path(workspace) / layout.folder_name_for(coworld)


def install_seed_skill(workspace, coworld: str, name: str, content: bytes) -> str:
    """Returns 'created', 'updated', 'unchanged' or 'kept-edited'."""
    _check(name, "skill")
    folder = _root(workspace, coworld) / "skills" / "seed" / name
    skill, marker = folder / "SKILL.md", folder / ".seed"
    new = _hash(content)
    if skill.exists():
        if not marker.exists():
            return "kept-edited"
        if marker.read_text().strip() == new and _hash(skill.read_bytes()) == new:
            return "unchanged"
        if _hash(skill.read_bytes()) != marker.read_text().strip():
            return "kept-edited"
        status = "updated"
    else:
        status = "created"
    folder.mkdir(parents=True, exist_ok=True)
    skill.write_bytes(content)
    marker.write_text(new + "\n")
    return status


def install_howto(workspace, coworld: str, content: bytes) -> str:
    path = _root(workspace, coworld) / "documents" / "howto.md"
    if path.exists() and path.read_bytes() == content:
        return "unchanged"
    status = "updated" if path.exists() else "created"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return status


def install_all(workspace, coworld: str, skills_dir=None, howto=None) -> dict[str, str]:
    result: dict[str, str] = {}
    if skills_dir:
        for sk in sorted(Path(skills_dir).glob("*/SKILL.md")):
            result[f"skill:{sk.parent.name}"] = install_seed_skill(
                workspace, coworld, sk.parent.name, sk.read_bytes()
            )
    if howto:
        result["howto"] = install_howto(workspace, coworld, Path(howto).read_bytes())
    return result
