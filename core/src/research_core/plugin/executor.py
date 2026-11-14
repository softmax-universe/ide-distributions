"""Run a tool call either in the Hermes process or in the workspace environment.

Store-only tools run in process (``executor="inproc"``). Tools that need the Softmax client,
PyYAML or numpy run as ``<workspace venv>/bin/python -m research_core.cli tool <name>`` and
their stdout is the JSON result (``executor="workspace"``). Either way the handler Hermes sees
takes the model's ``args`` and the injected ``session_id`` and returns a JSON string, never
raising.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from research_core import layout
from research_core.tools.catalog import Role

if TYPE_CHECKING:
    from research_core.plugin.tracing import Traces

TOOL_TIMEOUT_S = 400  # under Hermes's 420 s tool deadline


def profile_env(**overrides: str) -> dict[str, str]:
    """The environment the profile's code runs with: the process environment plus the routed
    profile's secret scope, plus ``overrides``.

    Under a multiplexing gateway Hermes keeps each profile's ``.env`` out of ``os.environ`` (so
    one profile's keys never leak into another's turns) and exposes the routed profile's values
    per task through ``agent.secret_scope``. Everything the plugin reads from the environment,
    in process or for a workspace subprocess, goes through here so a profile's ``.env`` reaches
    the tools the way the design assumes. The routed home comes from Hermes's context-local
    accessor; it is not the gateway's process-wide ``HERMES_HOME``.
    """
    merged = dict(os.environ)
    merged.update(_profile_scope())
    merged.update(overrides)
    return merged


def _profile_scope() -> dict[str, str]:
    try:
        from agent.secret_scope import current_secret_scope
        from hermes_constants import get_hermes_home
    except ImportError:  # outside Hermes: tests and the CLI
        return {}
    try:
        scope = current_secret_scope()
    except Exception:  # a scope API change must never take the tools down
        return {}
    return {
        **{str(k): str(v) for k, v in (scope or {}).items() if v is not None},
        "HERMES_HOME": str(get_hermes_home()),
    }


def tool_env(
    role: Role,
    session_id: str,
    env: dict[str, str] | None = None,
    turn_id: str | None = None,
    tool_call_id: str | None = None,
) -> dict[str, str]:
    merged = profile_env() if env is None else dict(env)
    merged["IDE_ROLE"] = role
    if session_id:
        merged["IDE_SESSION_ID"] = session_id
    if turn_id:
        merged["IDE_TURN_ID"] = turn_id
    if tool_call_id:
        merged["IDE_TOOL_CALL_ID"] = tool_call_id
    return merged


def error_result(kind: str, message: str, **context: Any) -> str:
    """A failure Hermes's tool never reported, with ``kind`` its short class for telemetry."""
    return json.dumps(
        {"ok": False, "kind": kind, "error": message, "problems": [message], "context": context}
    )


# Variables that bind a process to one interpreter. Hermes sets them for its own venv; the
# workspace interpreter would otherwise import Hermes's compiled packages (pydantic_core built
# for another Python) and fail with "No module named pydantic_core._pydantic_core".
INTERPRETER_VARS = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSAFEPATH",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "PYTHONNOUSERSITE",
    "VIRTUAL_ENV",
    "__PYVENV_LAUNCHER__",
)


def workspace_process_env(env: dict[str, str], ws: layout.Workspace) -> dict[str, str]:
    """``env`` for a process of the workspace environment: Hermes's interpreter variables
    dropped and the workspace's ``bin`` first on ``PATH``."""
    clean = {key: value for key, value in env.items() if key not in INTERPRETER_VARS}
    bin_dir = str(ws.venv / "bin")
    rest = [part for part in clean.get("PATH", "").split(os.pathsep) if part and part != bin_dir]
    clean["PATH"] = os.pathsep.join([bin_dir, *rest])
    return clean


def run_in_workspace(
    name: str, args: dict, env: dict[str, str], timeout_s: int | None = None
) -> str:
    ws = layout.from_env(env)
    python = ws.venv_python
    if not python.exists():
        return error_result(
            "workspace_missing",
            "workspace environment missing; run the profile's scripts/prepare.py",
            expected=str(python),
        )
    command = [str(python), "-m", "research_core.cli", "tool", name, "--args", json.dumps(args)]
    from research_core.telemetry import process_env

    timeout = timeout_s or TOOL_TIMEOUT_S
    try:
        completed = subprocess.run(
            command,
            env=process_env(workspace_process_env(env, ws)),
            stdout=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return error_result("timeout", f"tool {name} timed out after {timeout} s", command=command)
    # Inherit stderr: the SDK exports directly to normal process logs, once.
    stdout = completed.stdout.strip()
    if completed.returncode != 0 or not stdout:
        return error_result(
            "process_failed",
            f"tool {name} process failed",
            exit_code=completed.returncode,
            stdout_tail=stdout[-2000:],
            command=command,
        )
    return stdout.splitlines()[-1]


def make_handler(spec: Any, role: Role, traces: Traces) -> Callable[..., str]:
    def handler(args: dict | None = None, session_id: str = "", **_: Any) -> str:
        from research_core.plugin.authority import TOOL_DENIED, owner_tools

        if not owner_tools(profile_env(), session_id):
            return TOOL_DENIED
        args = dict(args or {})
        turn = traces.session_turn(session_id)
        # The native call this serves, when the turn has only one open call of this tool.
        call = traces.tool_call(session_id, spec.name)
        env = tool_env(
            role, session_id, turn_id=turn and turn.turn_id, tool_call_id=call and call[1]
        )
        from opentelemetry import trace

        with trace.use_span(
            call[0] if call else trace.INVALID_SPAN if turn is None else turn.span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                if spec.executor == "inproc":
                    from research_core.tools.base import ToolContext, run_tool

                    return run_tool(spec.name, args, ToolContext.from_env(role, env))
                return run_in_workspace(spec.name, args, env, spec.timeout_s)
            except Exception as error:  # never raise into Hermes
                return error_result(
                    type(error).__name__, f"{type(error).__name__}: {error}", tool=spec.name
                )

    handler.__name__ = f"tool_{spec.name}"
    return handler
