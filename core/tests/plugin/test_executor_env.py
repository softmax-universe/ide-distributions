"""The plugin reads its environment through the routed profile's secret scope."""

import subprocess
import sys
import types

from research_core.plugin import executor


def test_profile_env_merges_the_secret_scope_over_the_process_env(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/host")
    monkeypatch.setenv("SOFTMAX_PLAYER_ID", "ply_host")
    monkeypatch.setenv("ONLY_IN_PROCESS", "1")
    fake = types.ModuleType("agent.secret_scope")
    fake.current_secret_scope = lambda: {"SOFTMAX_PLAYER_ID": "ply_profile", "FROM_DOTENV": "x"}
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: "/host/profiles/orchestrator"
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)

    env = executor.profile_env(IDE_SESSION_ID="s1")
    assert env["SOFTMAX_PLAYER_ID"] == "ply_profile"  # the profile's .env wins
    assert env["FROM_DOTENV"] == "x" and env["ONLY_IN_PROCESS"] == "1"
    assert env["IDE_SESSION_ID"] == "s1"
    assert env["HERMES_HOME"] == "/host/profiles/orchestrator"
    assert executor.tool_env("orchestrator", "s1")["SOFTMAX_PLAYER_ID"] == "ply_profile"


def test_profile_env_without_hermes_or_without_a_scope(monkeypatch):
    monkeypatch.setenv("SOFTMAX_PLAYER_ID", "ply_host")
    monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
    monkeypatch.setitem(sys.modules, "agent", None)  # import fails cleanly
    assert executor.profile_env()["SOFTMAX_PLAYER_ID"] == "ply_host"
    fake = types.ModuleType("agent.secret_scope")
    fake.current_secret_scope = lambda: None
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: "/host/profiles/orchestrator"
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    assert executor.profile_env()["SOFTMAX_PLAYER_ID"] == "ply_host"


def test_workspace_process_env_drops_hermes_interpreter_variables(tmp_path):
    from research_core import layout

    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path / "ws")})
    env = {
        "PYTHONPATH": "/opt/hermes/.venv/lib/python3.14/site-packages",
        "VIRTUAL_ENV": "/opt/hermes/.venv",
        "PATH": "/opt/hermes/.venv/bin:/usr/bin",
        "IDE_WORKSPACE": str(tmp_path / "ws"),
        "OPENROUTER_API_KEY": "k",
    }
    clean = executor.workspace_process_env(env, ws)
    assert "PYTHONPATH" not in clean and "VIRTUAL_ENV" not in clean
    assert clean["PATH"].split(":")[0] == str(ws.venv / "bin")
    assert clean["PATH"].endswith("/opt/hermes/.venv/bin:/usr/bin")
    assert clean["OPENROUTER_API_KEY"] == "k" and clean["IDE_WORKSPACE"] == env["IDE_WORKSPACE"]


def test_workspace_tools_inherit_logs_and_propagate_standard_context(tmp_path, monkeypatch):
    from research_core import layout, telemetry
    from research_core.plugin import tracing

    env = {"IDE_WORKSPACE": str(tmp_path / "ws")}
    monkeypatch.setenv("IDE_WORKSPACE", env["IDE_WORKSPACE"])
    ws = layout.from_env(env)
    ws.venv_python.parent.mkdir(parents=True)
    ws.venv_python.touch()
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout='{"ok":false}')

    monkeypatch.setattr(executor.subprocess, "run", run)
    # Match Hermes's isolated handler worker: the parent exists but is not current.
    with telemetry.tracer().start_span("native.tool") as parent:
        tool = types.SimpleNamespace(name="demo", executor="workspace", timeout_s=1)
        turn = tracing.Turn("s1", "turn-1", {}, 0, span=parent)
        traces = types.SimpleNamespace(session_turn=lambda _: turn, tool_call=lambda *_: None)
        handler = executor.make_handler(tool, "researcher", traces)
        assert handler({}, session_id="s1") == '{"ok":false}'
    assert "stderr" not in calls[0] and "capture_output" not in calls[0]
    assert calls[0]["env"]["IDE_TURN_ID"] == "turn-1"
    context = telemetry.process_context(calls[0]["env"])
    from opentelemetry import trace

    remote = trace.get_current_span(context).get_span_context()
    assert (remote.trace_id, remote.span_id) == (
        parent.get_span_context().trace_id,
        parent.get_span_context().span_id,
    )
    assert remote.is_remote
