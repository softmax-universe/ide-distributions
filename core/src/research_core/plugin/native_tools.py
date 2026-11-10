"""Middleware that fixes two of Hermes's own tools where the model's first call usually misses.

- ``skill_by_category`` (``tool_request``): Hermes's skill index lists ``experiment`` under
  ``method:``, so models call ``skill_view('method.experiment')``, which Hermes does not find (27
  failures, 2026-11-07 to 11-09). When ``<category>.<name>`` reads as exactly one skill path that
  exists, the call asks for that path.
- ``read_file_nearest_directory`` (``tool_execution``): a ``read_file`` whose directory is missing
  gets Hermes's "File not found" with no hint, so the model guesses again (73 misses in the same
  window, mostly a wrong folder). The result then names the deepest directory that exists and what
  it holds. Hosted profiles use Hermes's local backend, so this process sees the same files.

Hermes isolates middleware: a callback that raises is logged and skipped. Stdlib and Hermes only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

NOT_FOUND = "File not found: "
LISTED = 30


def _dot_readings(name: str, roots: list[Path]) -> list[str]:
    """The ``<category>/<name>`` paths ``name`` can mean, where the category may nest (``a.b.c``
    reads as ``a/b.c`` or ``a/b/c``), that have a ``SKILL.md`` in some root."""
    parts = name.split(".")
    if "" in parts:
        return []
    readings = ("/".join(parts[:i]) + "/" + ".".join(parts[i:]) for i in range(1, len(parts)))
    return sorted(
        {path for path in readings if any((r / path / "SKILL.md").is_file() for r in roots)}
    )


def skill_by_category(*, tool_name: str, args: dict[str, Any], **_: Any) -> dict[str, Any] | None:
    """``tool_request`` middleware: ``skill_view('method.experiment')`` reads
    ``method/experiment``."""
    name = args.get("name")
    if tool_name != "skill_view" or not isinstance(name, str) or "." not in name or "/" in name:
        return None
    from tools.skills_tool import skill_search_dirs

    roots = [root for _tier, root in skill_search_dirs()[0]]
    if any(next(root.rglob(f"{name}/SKILL.md"), None) for root in roots):
        return None  # a skill really has this name
    readings = _dot_readings(name, roots)
    if len(readings) != 1:
        return None  # nothing to read, or more than one: Hermes's own not-found answers
    return {"args": {**args, "name": readings[0]}, "reason": "category.name skill reference"}


def read_file_nearest_directory(
    *, tool_name: str, args: dict[str, Any], next_call: Any, **_: Any
) -> Any:
    """``tool_execution`` middleware: a ``read_file`` miss in a missing directory names the deepest
    directory that exists and lists it."""
    result = next_call(args)
    if tool_name != "read_file" or not isinstance(result, str) or NOT_FOUND not in result:
        return result
    payload = json.loads(result)
    error = payload.get("error")
    if not isinstance(error, str) or not error.startswith(NOT_FOUND):
        return result
    missing = Path(error.removeprefix(NOT_FOUND)).parent
    if not missing.is_absolute() or missing.is_dir():
        return result
    nearest = next(parent for parent in missing.parents if parent.is_dir())
    entries = sorted(entry.name + ("/" if entry.is_dir() else "") for entry in os.scandir(nearest))[
        :LISTED
    ]
    payload["hint"] = (
        f"No directory {missing}. The nearest existing one is {nearest}, holding: "
        f"{', '.join(entries) or 'nothing'}"
    )
    return json.dumps(payload, ensure_ascii=False)
