"""Resolve tool authority from the native execution's registered conversation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from research_core.authority import OwnerAuthority, bound_authority

TOOL_DENIED = json.dumps(
    {"ok": False, "kind": "authority_denied", "error": "This conversation has no tool access"}
)


def owner_tools(env: Mapping[str, str], session_id: str) -> bool:
    try:
        return isinstance(bound_authority(env, session_id), OwnerAuthority)
    except Exception:
        # Hermes skips a middleware that raises. A failed authority read must return a refusal.
        return False


def guard_tools(
    *, args: dict[str, Any], next_call: Any, session_id: str = "", task_id: str = "", **_: Any
) -> Any:
    from research_core.plugin.executor import profile_env

    execution_id = task_id or session_id
    try:
        permitted = owner_tools(profile_env(), execution_id)
    except Exception:
        return TOOL_DENIED
    if not permitted:
        return TOOL_DENIED
    return next_call(args)
