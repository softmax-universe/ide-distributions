"""The tool catalog: which tools each role gets (design C.2) and the tool budget.

Tool modules register their ``ToolSpec`` here at import time; :func:`tools_for` returns the
role's set. The budget keeps every tool loaded in Hermes rather than deferred behind
``tool_search``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover
    from research_core.tools.base import ToolSpec

Role = Literal["orchestrator", "researcher"]
ROLES: tuple[Role, ...] = ("orchestrator", "researcher")
TOOL_BUDGET = 26

TOOLS: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.name in TOOLS:
        raise ValueError(f"tool {spec.name!r} registered twice")
    TOOLS[spec.name] = spec
    return spec


def tools_for(role: Role) -> list[ToolSpec]:
    _load_tool_modules()
    return sorted((spec for spec in TOOLS.values() if role in spec.roles), key=lambda s: s.name)


# Tool modules, imported on first use so that importing the catalog stays cheap.
TOOL_MODULES: tuple[str, ...] = (
    "research_core.tools.goal",
    "research_core.tools.task",
    "research_core.tools.claim",
    "research_core.tools.experiment",
    "research_core.tools.version",
    "research_core.tools.directive",
    "research_core.tools.settings",
    "research_core.tools.chat_update",
    "research_core.tools.workspace",
    "research_core.tools.upload",
    "research_core.tools.submit",
    "research_core.tools.xp",
    "research_core.tools.platform",
    "research_core.tools.forum_post",
    "research_core.tools.wiki_edit",
    "research_core.tools.softmax_api",
    "research_core.tools.github",
)
_loaded = False


def _load_tool_modules() -> None:
    """Import every tool module, and let a failure propagate: a broken module must stop what asked
    for the tools, never quietly drop its tool from a manifest, a prompt or a session.

    Every module imports under Hermes's interpreter (only the standard library at import time;
    ``test_in_process_imports_are_stdlib_only`` checks it), so no failure here is expected. In the
    gateway the exception reaches Hermes's plugin loader, which logs "Failed to load plugin" and
    keeps the gateway up without this profile's plugin.
    """
    global _loaded
    if _loaded:
        return
    import importlib

    for module_name in TOOL_MODULES:
        importlib.import_module(module_name)
    _loaded = True
