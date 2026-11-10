"""The hooks of ``image/hermes-patches/``, on a real gateway with a stub model and no model spend.

One gateway serves two profiles. ``recording`` registers every new hook (and a second callback for
each that raises); ``plain`` registers none of them except the exit hook. The stub model drops
streams on purpose so that the failed attempts exist to be observed. These cover what a whole
Session in ``test_rebuild_real.py`` does not: a dropped stream, a rebuilt prompt, overlapping runs
and the exit hook. The reference for the hooks is ``image/hermes-patches/README.md``. These tests
fail on an unpatched Hermes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from research_core.watcher.hermes_api import mint_session_id
from tests.hermes.capture_rig import (
    API_KEY,
    chunk,
    stream_reply,
    stub_server,
    tool_call,
    whole_reply,
)
from tests.hermes.test_spike_s1 import free_port, hermes_python

RECORDER_PLUGIN = Path(__file__).resolve().parent / "recorder_plugin"
RUN_TIMEOUT_S = 180
REASONING = "thinking before the drop"
PARTIAL_ARGUMENTS = '{"partial": "cut he'
VISIBLE_TEXT = "text the user saw before the drop"
SOUL_TEXT = "SOUL TEXT FOR THE TEST"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18, "cost": 0.25}
CHUNK_PAUSE_S = 0.05


class StubModel:
    """An OpenAI-compatible endpoint. The latest user message picks the script of a turn."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.dropped: set[str] = set()
        self.server = stub_server(self.handle)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()

    def handle(self, handler, raw: bytes, _arrived: float) -> None:
        body = json.loads(raw)
        messages = body.get("messages")
        if messages is None:
            # A /responses probe or similar; the chat path below is the one under test.
            handler.send_error(404)
            return
        if body.get("tools"):
            self._turn(handler, messages)
        elif body.get("stream"):
            self._stream(handler, [chunk({"content": "a title"}), chunk({}, "stop", USAGE)])
        else:
            whole_reply(handler, "a title", USAGE)

    def _turn(self, handler, messages: list[dict]) -> None:
        users = [m for m in messages if m["role"] == "user"]
        script = str(users[-1]["content"])
        after_tools = messages[-1]["role"] == "tool"
        with self.lock:
            first_time = script not in self.dropped
            self.dropped.add(script)
        if after_tools or "plain answer" in script:
            self._stream(handler, [chunk({"content": "done"}), chunk({}, "stop", USAGE)])
        elif first_time and "drop mid tool call" in script:
            self._stream(
                handler,
                [
                    chunk({"reasoning": REASONING}),
                    chunk(
                        {"tool_calls": [tool_call(0, "call_cut", "big_tool", PARTIAL_ARGUMENTS)]}
                    ),
                ],
                drop=True,
            )
        elif first_time and "drop after text" in script:
            self._stream(handler, [chunk({"content": VISIBLE_TEXT})], drop=True)
        elif "drop after text" in script:
            self._stream(handler, [chunk({"content": "done"}), chunk({}, "stop", USAGE)])
        else:
            self._stream(
                handler,
                [
                    chunk({"tool_calls": [tool_call(0, "call_big", "big_tool", "{}")]}),
                    chunk({"tool_calls": [tool_call(1, "call_small", "small_tool", "{}")]}),
                    chunk({}, "tool_calls", USAGE),
                ],
            )

    @staticmethod
    def _stream(handler, chunks: list[bytes], drop: bool = False) -> None:
        stream_reply(handler, chunks, pause_s=CHUNK_PAUSE_S, drop=drop)


@dataclass
class Gateway:
    home: Path
    port: int
    events_path: Path
    process: subprocess.Popen
    log: Path
    env: dict[str, str]

    def request(self, method: str, profile: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/p/{profile}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    def open_session(self, profile: str) -> str:
        session_id = mint_session_id()
        self.request("POST", profile, "/api/sessions", {"id": session_id})
        return session_id

    def start_run(self, profile: str, session_id: str, text: str) -> str:
        return self.request("POST", profile, "/v1/runs", {"input": text, "session_id": session_id})[
            "run_id"
        ]

    def wait_run(self, profile: str, run_id: str) -> dict:
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while time.monotonic() < deadline:
            status = self.request("GET", profile, f"/v1/runs/{run_id}")
            if status["status"] in ("completed", "failed", "cancelled", "interrupted"):
                assert status["status"] == "completed", (status, self.log.read_text()[-4000:])
                return status
            time.sleep(0.2)
        raise AssertionError(f"run {run_id} did not finish: {self.log.read_text()[-4000:]}")

    def events(self, hook: str | None = None, **match) -> list[dict]:
        if not self.events_path.exists():
            return []
        found = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if hook is not None and event["hook"] != hook:
                continue
            if all(event.get(key) == value for key, value in match.items()):
                found.append(event)
        return found

    def wait_events(self, hook: str, **match) -> list[dict]:
        """Bounded hooks run on a worker, so a record can land just after the run ends."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            found = self.events(hook, **match)
            if found:
                return found
            time.sleep(0.1)
        raise AssertionError(f"no {hook} matching {match}: {self.log.read_text()[-4000:]}")


def _profile_files(profile: Path, stub_url: str, workspace: Path) -> None:
    (profile / "plugins").mkdir(parents=True)
    (profile / "plugins" / "recorder").symlink_to(RECORDER_PLUGIN)
    (profile / "SOUL.md").write_text(f"{SOUL_TEXT}\n")
    (profile / "config.yaml").write_text(
        json.dumps(
            {
                "model": {"default": "stub-model", "provider": "custom", "base_url": stub_url},
                "plugins": {"enabled": ["recorder"]},
                "platform_toolsets": {"api_server": ["recorder"]},
                "known_plugin_toolsets": {"api_server": ["recorder"]},
                "tools": {"tool_search": {"enabled": "off"}},
                "terminal": {"cwd": str(workspace)},
                "memory": {"memory_enabled": False},
                "auxiliary": {"title_generation": {"provider": "main", "model": "stub-model"}},
            }
        )
    )
    (profile / ".env").write_text(
        f"API_SERVER_ENABLED=true\nAPI_SERVER_KEY={API_KEY}\nOPENAI_API_KEY=stub\n"
        f"TERMINAL_CWD={workspace}\n"
    )


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=60)


@pytest.fixture(scope="module")
def stub_model():
    stub = StubModel()
    yield stub
    stub.close()


@pytest.fixture(scope="module")
def gateway(hermes_bin, tmp_path_factory, stub_model):
    root = tmp_path_factory.mktemp("patched")
    home = root / "home"
    workspace = root / "workspace"
    workspace.mkdir()
    home.mkdir()
    port = free_port()
    (home / ".env").write_text(
        f"API_SERVER_ENABLED=true\nAPI_SERVER_HOST=127.0.0.1\nAPI_SERVER_PORT={port}\n"
        f"API_SERVER_KEY={API_KEY}\nOPENAI_API_KEY=stub\nGATEWAY_MULTIPLEX_PROFILES=true\n"
    )
    for name in ("recording", "plain"):
        _profile_files(home / "profiles" / name, stub_model.url, workspace)
    events_path = root / "events.jsonl"
    log = root / "gateway.log"
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "HERMES_REAL_HOME": str(home),
        "RECORDER_OUT": str(events_path),
        "GATEWAY_MULTIPLEX_PROFILES": "true",
    }
    with log.open("ab") as output:
        process = subprocess.Popen(
            [hermes_python(hermes_bin), "-m", "gateway.run"],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    running = Gateway(home, port, events_path, process, log, env)
    deadline = time.monotonic() + 120
    while True:
        try:
            running.request("GET", "recording", "/v1/capabilities")
            break
        except OSError:
            assert process.poll() is None, log.read_text()[-4000:]
            assert time.monotonic() < deadline, log.read_text()[-4000:]
            time.sleep(0.5)
    yield running
    _stop(process)


@pytest.fixture(scope="module")
def tool_turn(gateway):
    """One run with two tools, and a concurrent run in another session."""
    session_id = gateway.open_session("recording")
    other_session_id = gateway.open_session("recording")
    run_id = gateway.start_run("recording", session_id, "use the tools")
    other_run_id = gateway.start_run("recording", other_session_id, "plain answer please")
    gateway.wait_run("recording", run_id)
    gateway.wait_run("recording", other_run_id)
    return session_id, run_id, other_session_id, other_run_id


def test_prompt_rebuild_fires_again_with_the_new_text(gateway, hermes_bin):
    session_id = gateway.open_session("recording")
    gateway.wait_run("recording", gateway.start_run("recording", session_id, "plain answer one"))
    assert len(gateway.wait_events("on_system_prompt_built", session_id=session_id)) == 1
    profile = gateway.home / "profiles" / "recording"
    original_soul = (profile / "SOUL.md").read_text()
    (profile / "SOUL.md").write_text("REBUILT SOUL TEXT\n")
    try:
        refreshed = subprocess.run(
            [str(hermes_bin), "sessions", "refresh-prompts"],
            env={**gateway.env, "HERMES_HOME": str(profile)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
        run_id = gateway.start_run("recording", session_id, "plain answer two")
        gateway.wait_run("recording", run_id)
    finally:
        (profile / "SOUL.md").write_text(original_soul)
    builds = gateway.events("on_system_prompt_built", session_id=session_id)
    assert len(builds) == 2
    assert "REBUILT SOUL TEXT" in builds[-1]["system_prompt"]
    soul = next(s for s in builds[-1]["sections"] if s["source"] == "soul")
    assert builds[-1]["system_prompt"][soul["start"] : soul["end"]] == "REBUILT SOUL TEXT"


def test_failed_stream_attempt_keeps_what_the_stub_drops(gateway):
    session_id = gateway.open_session("recording")
    run_id = gateway.start_run("recording", session_id, "drop mid tool call now")
    gateway.wait_run("recording", run_id)
    attempts = gateway.wait_events("on_model_attempt", session_id=session_id)
    failed = next(a for a in attempts if a["outcome"] == "failed")
    assert failed["kind"] == "stream"
    assert failed["attempt"] == 1
    assert failed["run_id"] == run_id
    assert failed["error_type"]
    assert failed["stream_parts"]["reasoning"] == REASONING
    assert failed["stream_parts"]["tool_calls"] == [
        {"id": "call_cut", "name": "big_tool", "arguments": PARTIAL_ARGUMENTS}
    ]
    assert failed["response"] is None
    retried = next(a for a in attempts if a["attempt"] == 2)
    assert retried["outcome"] == "completed"
    assert retried["api_request_id"] == failed["api_request_id"]
    assert retried["request"]["stream"] is True
    assert retried["request"]["stream_options"] == {"include_usage": True}
    assert retried["request"]["messages"]
    assert "timeout" not in retried["request"]
    assert retried["response"]["usage"]["cost"] == USAGE["cost"]
    assert retried["first_chunk_at"] >= retried["started_at"]


def test_stream_dropped_after_visible_text_reports_the_error_and_partial_text(gateway):
    session_id = gateway.open_session("recording")
    gateway.wait_run("recording", gateway.start_run("recording", session_id, "drop after text now"))
    attempts = gateway.wait_events("on_model_attempt", session_id=session_id)
    failed = next(a for a in attempts if a["outcome"] == "failed")
    assert failed["stream_parts"]["content"] == VISIBLE_TEXT
    assert failed["error"]


def test_every_hook_names_its_own_run_while_runs_overlap(gateway, tool_turn):
    session_id, run_id, other_session_id, other_run_id = tool_turn
    assert run_id != other_run_id
    for hook in ("pre_llm_call", "pre_api_request", "post_api_request", "on_model_attempt"):
        events = gateway.wait_events(hook, session_id=session_id)
        assert {event["run_id"] for event in events} == {run_id}, hook
        others = gateway.wait_events(hook, session_id=other_session_id)
        assert {event["run_id"] for event in others} == {other_run_id}, hook
    # Tool callbacks run on tool worker threads.
    tool_events = gateway.wait_events("post_tool_call", session_id=session_id)
    assert {event["tool_name"] for event in tool_events} == {"big_tool", "small_tool"}
    assert {event["run_id"] for event in tool_events} == {run_id}


def test_gateway_exit_reaches_every_profile_before_the_process_ends(gateway):
    """Last in this file because it stops the module's gateway."""
    assert gateway.process.poll() is None
    gateway.process.send_signal(signal.SIGTERM)
    gateway.process.wait(timeout=60)
    exits = gateway.events("on_gateway_exit")
    assert {event["profile"] for event in exits} >= {"recording", "plain"}
    # An unplanned SIGTERM ends this gateway with 1 on purpose, patched or not.
    assert {event["exit_code"] for event in exits} == {gateway.process.returncode}


def test_a_tool_handler_gets_the_native_call_ids(hermes_bin, tmp_path):
    """Hermes has no public reader of the tool-call ID it binds, so hold ours to this image."""
    script = """
import json
from hermes_cli.middleware import current_run_id
from research_core.plugin.executor import tool_env
from tools.approval_context import set_current_observability_context

current_run_id.set("run_1")
set_current_observability_context(tool_call_id="call_1", session_id="ses_1")
env = tool_env("researcher", "ses_1", {})
print(json.dumps({"run": env["IDE_RUN_ID"], "call": env["IDE_TOOL_CALL_ID"]}))
"""
    source = Path(__file__).resolve().parents[2] / "src"
    done = subprocess.run(
        [str(hermes_python(hermes_bin)), "-c", script],
        env={**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(done.stdout.splitlines()[-1]) == {"run": "run_1", "call": "call_1"}
