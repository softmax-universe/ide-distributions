"""A release reaches open chats on a real Hermes gateway, and nothing else disturbs them.

Hermes resends a session's stored tools and system prompt on every turn. This test runs both
profiles on a real gateway with the real plugin, against a local fake model that records each
request, and boots them the way ``image/runtime.py`` does: replace the release checkout,
``hermes profile update``, ``prepare.py``, then the gateway. It needs no model key and no network.

The old release is the current tree with an extra field on every tool and a marker in ``SOUL.md``.
A chat left on it holds a schema and instructions the current release does not have, which is the
state a chat like api_1793743095_1cb893cb was stuck in.

CI runs it as root, where the test sets ``HERMES_ALLOW_ROOT_GATEWAY`` for its throwaway home. To run
it by hand, use the image the release uses, as the image's own user::

    uv pip install --python 3.14 --target <dir> pytest
    docker run --rm --user 10000:10000 -v <checkout>:/repo:ro -v <dir>:/pytest:ro \\
        -e PYTHONPATH=/pytest:/repo/core/src:/repo/core -e PYTHONDONTWRITEBYTECODE=1 \\
        -e HERMES_BIN=/opt/hermes/.venv/bin/hermes -w /repo \\
        --entrypoint /opt/hermes/.venv/bin/python <the image in image/Dockerfile> \\
        -m pytest -p no:cacheprovider -m hermes core/tests/hermes/test_frozen_sessions_real.py
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from research_core.watcher.hermes_api import HermesRuns, mint_session_id
from tests.hermes.test_spike_s1 import REPO, Host, free_port, hermes_python, run

ROLES = ("orchestrator", "researcher")
OLD_SOUL_MARKER = "OLD-RELEASE-INSTRUCTIONS"
OLD_RELEASE_FIELD = "old_release_field"
REFRESHED = "open sessions: refreshed tools and system prompts"
RUN_TIMEOUT_S = 120


class FakeModel:
    """An OpenAI-style endpoint that answers every request with one line and keeps the request."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send_json(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._send_json({"object": "list", "data": [{"id": "fake", "object": "model"}]})

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model.requests.append(request)
                usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
                base = {"id": "reply", "model": "fake"}
                message = model.reply(request)
                finish = "tool_calls" if message.get("tool_calls") else "stop"
                if not request.get("stream"):
                    choice = {"index": 0, "message": message, "finish_reason": finish}
                    self._send_json(
                        {**base, "object": "chat.completion", "choices": [choice], "usage": usage}
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [
                    {"choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        "usage": usage,
                    },
                ]
                for chunk in chunks:
                    payload = {**base, "object": "chat.completion.chunk", **chunk}
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, request: dict) -> dict:
        return {"role": "assistant", "content": "Done."}


def old_release(checkout: Path) -> None:
    tools_base = checkout / "core/src/research_core/tools/base.py"
    tools_base.write_text(
        tools_base.read_text()
        + f"""

_current_schema = ToolSpec.schema


def _old_release_schema(self, role):
    schema = _current_schema(self, role)
    field = {{"type": "string", "description": "Old."}}
    schema["parameters"]["properties"]["{OLD_RELEASE_FIELD}"] = field
    return schema


ToolSpec.schema = _old_release_schema
"""
    )
    for role in ROLES:
        soul = checkout / role / "SOUL.md"
        soul.write_text(soul.read_text() + f"\n{OLD_SOUL_MARKER}\n")


def unrelated_commit(checkout: Path) -> None:
    watcher = checkout / "core/src/research_core/watcher/cli.py"
    watcher.write_text(watcher.read_text() + "\n# An unrelated commit.\n")


class Workspace:
    """Boots the release the way the image does, and runs turns against a fake model."""

    def __init__(
        self,
        root: Path,
        hermes_bin: Path,
        monkeypatch: pytest.MonkeyPatch,
        model: FakeModel | None = None,
    ) -> None:
        self.hermes_bin = hermes_bin
        self.checkout = root / "release"
        self.prebuilt_env = root / "workspace-env"
        self.prebuilt_env.mkdir()
        # The workspace environment is not under test; hosted workspaces also use a prebuilt one.
        monkeypatch.setenv("IDE_WORKSPACE_ENV", str(self.prebuilt_env))
        monkeypatch.setenv("IDE_RESEARCHER_PROFILES", "researcher")
        if os.geteuid() == 0:  # CI's container; the Hermes home is a temporary folder
            monkeypatch.setenv("HERMES_ALLOW_ROOT_GATEWAY", "1")
        monkeypatch.setenv("IDE_PLATFORM", "fake")
        monkeypatch.setenv("HOME", str(root / "host"))
        self.host = Host(
            hermes_bin=hermes_bin,
            home=root / "host",
            workspace=root / "workspace",
            port=free_port(),
            key=secrets.token_hex(16),
            openrouter_key="not-used",
            log=root / "gateway.log",
            dotenv_extra={"IDE_WORKSPACE_ENV": str(self.prebuilt_env)},
        )
        self.host.home.mkdir()
        self.host.write_host_dotenv()
        self.model = model or FakeModel()

    def publish(self, change=None) -> None:
        """Put a release at the checkout path, as a new image does at /opt/distribution."""
        if self.checkout.exists():
            shutil.rmtree(self.checkout)
        ignore = shutil.ignore_patterns("__pycache__", ".venv", "tests")
        for folder in ("core", *ROLES):
            shutil.copytree(REPO / folder, self.checkout / folder, ignore=ignore)
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copy(REPO / name, self.checkout / name)
        if change:
            change(self.checkout)
        lock = hashlib.sha256((self.checkout / "uv.lock").read_bytes()).hexdigest()
        (self.prebuilt_env / ".lock-sha").write_text(lock + "\n")

    def boot(self) -> dict[str, list[str]]:
        """Update and prepare each profile, start the gateway; returns what prepare did."""
        actions = {}
        for role in ROLES:
            if self.host.profile(role).exists():
                argv = [self.hermes_bin, "profile", "update", role, "--yes"]
            else:
                source = self.checkout / role
                argv = [self.hermes_bin, "profile", "install", source, "--name", role, "--yes"]
            result = run(argv, self.host.base_env())
            assert result.returncode == 0, result.stdout + result.stderr
            self.host.write_dotenv(role)
            script = self.host.profile(role) / "scripts" / "prepare.py"
            source = self.checkout / role
            argv = [hermes_python(self.hermes_bin), script, "--source", source, "--hermes"]
            result = run([*argv, self.hermes_bin], self.host.profile_env(role), timeout=300)
            assert result.returncode == 0, result.stdout + result.stderr
            actions[role] = json.loads(result.stdout.strip().splitlines()[-1])["actions"]
            self.use_fake_model(role)
        self.host.start_gateway()
        return actions

    def use_fake_model(self, role: str) -> None:
        settings = {
            "model.provider": "custom",
            "model.default": "fake",
            "model.base_url": f"http://127.0.0.1:{self.model.port}/v1",
            "model.api_key": "not-used",
            "model.context_length": "200000",
        }
        script = """
import json
import sys
from hermes_cli.config import set_config_value

for key, value in json.loads(sys.argv[1]).items():
    set_config_value(key, value)
"""
        result = run(
            [hermes_python(self.hermes_bin), "-c", script, json.dumps(settings)],
            self.host.profile_env(role),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def turn(self, role: str, session_id: str) -> dict:
        """One run on the session; returns the agent's request (a title request has no tools)."""
        runs = HermesRuns(
            base_url=self.host.base_url,
            keys=dict.fromkeys(ROLES, self.host.key),
            default_profile="orchestrator",
        )
        seen = len(self.model.requests)
        run_id = runs.start(
            profile=role,
            session_id=session_id,
            text="Reply and stop.",
            idempotency_key=secrets.token_hex(8),
        )
        deadline = time.monotonic() + RUN_TIMEOUT_S
        status = runs.status(run_id, profile=role)
        while not status.terminal and time.monotonic() < deadline:
            time.sleep(0.2)
            status = runs.status(run_id, profile=role)
        assert status.status == "completed", status
        return next(r for r in reversed(self.model.requests[seen:]) if r.get("tools"))


def task_properties(request: dict) -> dict:
    tool = next(t for t in request["tools"] if t["function"]["name"] == "task")
    return tool["function"]["parameters"]["properties"]


def system_prompt(request: dict) -> str:
    content = request["messages"][0]["content"]
    return content if isinstance(content, str) else "".join(part["text"] for part in content)


def assert_cache_prefix_kept(earlier: dict, later: dict) -> None:
    """A prefix cache hits when the tools and everything already sent are unchanged."""
    assert later["tools"] == earlier["tools"]
    assert later["messages"][: len(earlier["messages"])] == earlier["messages"]


def test_a_release_reaches_open_chats_once_and_other_boots_keep_their_cache(
    tmp_path, hermes_bin, monkeypatch
):
    workspace = Workspace(tmp_path, hermes_bin, monkeypatch)
    try:
        _scenario(workspace)
    finally:
        workspace.host.stop_gateway()


def _scenario(workspace: Workspace) -> None:
    host = workspace.host
    sessions = {role: mint_session_id() for role in ROLES}

    workspace.publish(old_release)
    actions = workspace.boot()
    # A new profile has no state.db yet, so its first preparation has nothing to refresh.
    assert not any("open sessions" in action for role in ROLES for action in actions[role]), actions
    latest = {}
    for role, session_id in sessions.items():
        workspace.turn(role, session_id)
        latest[role] = workspace.turn(role, session_id)
        assert OLD_RELEASE_FIELD in task_properties(latest[role])
        assert OLD_SOUL_MARKER in system_prompt(latest[role])
    host.stop_gateway()

    # The boot after this feature first sees sessions records the release. They are refreshed
    # once, here, which is how chats opened before this feature get out of an old pin.
    actions = workspace.boot()
    assert all(REFRESHED in actions[role] for role in ROLES), actions
    for role, session_id in sessions.items():
        latest[role] = workspace.turn(role, session_id)
    host.stop_gateway()

    # Same release: no refresh, and each session's next request extends the previous one.
    actions = workspace.boot()
    assert not any(REFRESHED in actions[role] for role in ROLES), actions
    for role, session_id in sessions.items():
        request = workspace.turn(role, session_id)
        assert_cache_prefix_kept(latest[role], request)
        latest[role] = request
    host.stop_gateway()

    # A commit that touches nothing frozen: still no refresh.
    workspace.publish(lambda checkout: (old_release(checkout), unrelated_commit(checkout)))
    actions = workspace.boot()
    assert not any(REFRESHED in actions[role] for role in ROLES), actions
    for role, session_id in sessions.items():
        request = workspace.turn(role, session_id)
        assert_cache_prefix_kept(latest[role], request)
        latest[role] = request
    host.stop_gateway()

    # The release that changes the schema: the first turn already sends it.
    workspace.publish()
    actions = workspace.boot()
    assert all(REFRESHED in actions[role] for role in ROLES), actions
    for role, session_id in sessions.items():
        first_turn = workspace.turn(role, session_id)
        assert OLD_RELEASE_FIELD not in task_properties(first_turn)
        assert OLD_SOUL_MARKER not in system_prompt(first_turn)
        assert "[session context]" in system_prompt(first_turn)
        assert "[briefing]" in system_prompt(first_turn)
        # The conversation itself survives the refresh.
        earlier_messages = latest[role]["messages"][1:]
        assert first_turn["messages"][1 : len(earlier_messages) + 1] == earlier_messages
        second_turn = workspace.turn(role, session_id)
        assert_cache_prefix_kept(first_turn, second_turn)
        latest[role] = second_turn
    host.stop_gateway()

    # And it stays: a restart restores the new pin and the new prompt verbatim.
    actions = workspace.boot()
    assert not any(REFRESHED in actions[role] for role in ROLES), actions
    for role, session_id in sessions.items():
        request = workspace.turn(role, session_id)
        assert OLD_RELEASE_FIELD not in task_properties(request)
        assert_cache_prefix_kept(latest[role], request)
