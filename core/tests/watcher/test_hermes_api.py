"""HermesRuns against a scripted local HTTP server, and FakeHermesRuns' behaviour.

The ``gateway`` fixture is a threaded ``http.server`` that records every request (method, path,
headers, parsed JSON body) and answers from a queue of scripted ``(status, payload)`` responses;
an unscripted request answers 500 so a test cannot pass on a response it did not expect.
"""

from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from research_core.platform.errors import PlatformError
from research_core.watcher.hermes_api import (
    TERMINAL,
    FakeHermesRuns,
    FakeRun,
    HermesRuns,
    RunsAuthError,
    RunsBusy,
    RunsConflict,
    RunsError,
    RunStatus,
    RunsUnavailable,
    mint_session_id,
    read_dotenv,
)

ORCH_KEY = "orchestrator-key-0123456789abcdef"
RESEARCHER_KEY = "researcher-key-0123456789abcdef"
KEYS = {"orchestrator": ORCH_KEY, "researcher": RESEARCHER_KEY}


class ScriptedGateway:
    """Records requests; answers each from ``responses`` in order."""

    def __init__(self):
        self.requests: list[dict] = []
        self.responses: list[tuple[int, object]] = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                gateway.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(raw) if raw else None,
                    }
                )
                status, payload = (
                    gateway.responses.pop(0)
                    if gateway.responses
                    else (500, {"error": "unscripted request"})
                )
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

            def log_message(self, *_):  # keep pytest output clean
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def answer(self, status: int, payload: object) -> None:
        self.responses.append((status, payload))

    @property
    def last(self) -> dict:
        return self.requests[-1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    stub = ScriptedGateway()
    yield stub
    stub.close()


@pytest.fixture
def client(gateway) -> HermesRuns:
    return HermesRuns(base_url=gateway.url, keys=KEYS, timeout_s=5)


def write_env(profile_home: Path, **values: str) -> None:
    profile_home.mkdir(parents=True, exist_ok=True)
    (profile_home / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))


# --- dotenv and from_profile -----------------------------------------------------------------


def test_read_dotenv_tolerates_quotes_comments_and_export(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# the gateway\n"
        "API_SERVER_KEY='quoted key'\n"
        'API_SERVER_HOST="10.0.0.5"\n'
        "export API_SERVER_PORT=9000  # inline comment\n"
        "\n"
        "EMPTY=\n"
        "not a pair\n"
        "WITH_EQUALS=a=b\n"
    )
    assert read_dotenv(env_file) == {
        "API_SERVER_KEY": "quoted key",
        "API_SERVER_HOST": "10.0.0.5",
        "API_SERVER_PORT": "9000",
        "EMPTY": "",
        "WITH_EQUALS": "a=b",
    }
    assert read_dotenv(tmp_path / "missing.env") == {}


def test_from_profile_reads_every_profile_key_and_falls_back(tmp_path):
    profiles = tmp_path / "profiles"
    write_env(
        profiles / "orchestrator",
        API_SERVER_KEY=ORCH_KEY,
        API_SERVER_HOST="10.1.2.3",
        API_SERVER_PORT="9999",
    )
    write_env(profiles / "researcher", API_SERVER_KEY=RESEARCHER_KEY)
    (profiles / "researcher-2").mkdir()  # installed, but no .env yet

    client = HermesRuns.from_profile(
        profiles / "orchestrator", researcher_profiles=["researcher", "researcher-2"]
    )

    assert client.base_url == "http://10.1.2.3:9999"
    assert client.default_profile == "orchestrator"
    assert client.keys == KEYS
    assert client.key_for("researcher") == RESEARCHER_KEY
    assert client.key_for("researcher-2") == ORCH_KEY  # falls back to the Orchestrator's


def test_from_profile_defaults_host_and_port(tmp_path):
    write_env(tmp_path / "orchestrator", API_SERVER_KEY=ORCH_KEY)
    client = HermesRuns.from_profile(tmp_path / "orchestrator")
    assert client.base_url == "http://127.0.0.1:8642"
    assert client.keys == {"orchestrator": ORCH_KEY}


# --- start -----------------------------------------------------------------------------------


def test_start_posts_body_key_and_idempotency_key(gateway, client):
    gateway.answer(202, {"run_id": "run_abc", "status": "started", "replayed": False})

    run_id = client.start(
        profile="researcher",
        session_id="api_1_deadbeef",
        text="work on T-1",
        instructions="be brief",
        idempotency_key="task:1:3:0",
    )

    assert run_id == "run_abc"
    request = gateway.last
    assert (request["method"], request["path"]) == ("POST", "/p/researcher/v1/runs")
    assert request["headers"]["authorization"] == f"Bearer {RESEARCHER_KEY}"
    assert request["headers"]["idempotency-key"] == "task:1:3:0"
    assert request["headers"]["content-type"] == "application/json"
    assert request["body"] == {
        "input": "work on T-1",
        "session_id": "api_1_deadbeef",
        "instructions": "be brief",
    }


def test_start_omits_absent_optionals_and_uses_the_default_key(gateway, client):
    gateway.answer(200, {"run_id": "run_def", "status": "started"})

    run_id = client.start(profile="researcher-2", session_id="api_1_0badf00d", text="hi")

    assert run_id == "run_def"
    request = gateway.last
    assert request["path"] == "/p/researcher-2/v1/runs"
    assert request["headers"]["authorization"] == f"Bearer {ORCH_KEY}"
    assert "idempotency-key" not in request["headers"]
    assert request["body"] == {"input": "hi", "session_id": "api_1_0badf00d"}


def test_start_429_raises_busy(gateway, client):
    gateway.answer(
        429,
        {
            "error": {
                "message": "too many",
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
            }
        },
    )
    with pytest.raises(RunsBusy) as info:
        client.start(profile="researcher", session_id="s", text="t")
    error = info.value
    assert (error.status, error.code, error.retryable) == (429, "rate_limit_exceeded", True)
    assert isinstance(error, PlatformError)
    assert error.to_dict()["code"] == "rate_limit_exceeded"


def test_start_503_draining_raises_unavailable_which_is_busy(gateway, client):
    gateway.answer(503, {"error": {"message": "draining", "code": "gateway_draining"}})
    with pytest.raises(RunsUnavailable) as info:
        client.start(profile="researcher", session_id="s", text="t")
    assert isinstance(info.value, RunsBusy)
    assert info.value.retryable is True
    assert info.value.status == 503


@pytest.mark.parametrize("http_status", [401, 403])
def test_start_auth_failure_raises_auth_error(gateway, client, http_status):
    gateway.answer(
        http_status,
        {"error": {"message": "Invalid gateway API key", "code": "gateway_auth_failed"}},
    )
    with pytest.raises(RunsAuthError) as info:
        client.start(profile="researcher", session_id="s", text="t")
    assert info.value.status == http_status
    assert info.value.retryable is False
    assert "Invalid gateway API key" in str(info.value)


def test_start_409_raises_conflict(gateway, client):
    gateway.answer(
        409, {"error": {"message": "payload differs", "code": "idempotency_key_conflict"}}
    )
    with pytest.raises(RunsConflict) as info:
        client.start(profile="researcher", session_id="s", text="t", idempotency_key="k")
    assert info.value.code == "idempotency_key_conflict"
    assert info.value.retryable is False


def test_start_without_any_key_raises_auth_error_before_sending(gateway):
    client = HermesRuns(base_url=gateway.url, keys={})
    with pytest.raises(RunsAuthError, match="no API_SERVER_KEY"):
        client.start(profile="researcher", session_id="s", text="t")
    assert gateway.requests == []


def test_start_with_no_run_id_in_the_answer_is_an_error(gateway, client):
    gateway.answer(202, {"status": "started"})
    with pytest.raises(RunsError) as info:
        client.start(profile="researcher", session_id="s", text="t")
    assert info.value.retryable is True


def test_unexpected_server_error_is_retryable_runs_error(gateway, client):
    gateway.answer(500, {"error": "boom"})
    with pytest.raises(RunsError) as info:
        client.status("run_x", profile="orchestrator")
    assert (info.value.status, info.value.retryable) == (500, True)
    assert type(info.value) is RunsError


def test_connection_refused_raises_unavailable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    client = HermesRuns(base_url=f"http://127.0.0.1:{closed_port}", keys=KEYS, timeout_s=2)
    with pytest.raises(RunsUnavailable, match="gateway unreachable"):
        client.start(profile="orchestrator", session_id="s", text="t")


# --- status, steer, stop ---------------------------------------------------------------------


def test_status_parses_a_running_run(gateway, client):
    gateway.answer(
        200,
        {"object": "hermes.run", "run_id": "run_1", "status": "running", "session_id": "api_1_a"},
    )
    status = client.status("run_1", profile="researcher")
    assert (gateway.last["method"], gateway.last["path"]) == ("GET", "/p/researcher/v1/runs/run_1")
    assert status == RunStatus("run_1", "running", "api_1_a", None, None, False, status.raw)
    assert status.raw["object"] == "hermes.run"


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL))
def test_status_parses_terminal_states(gateway, client, terminal_status):
    gateway.answer(
        200,
        {
            "run_id": "run_2",
            "status": terminal_status,
            "session_id": "api_1_b",
            "output": "done",
            "error": {"message": "why"} if terminal_status == "failed" else None,
        },
    )
    status = client.status("run_2", profile="orchestrator")
    assert status.terminal is True
    assert status.status == terminal_status
    assert status.output == "done"
    assert status.error == ("why" if terminal_status == "failed" else None)


def test_status_404_is_unknown_and_terminal(gateway, client):
    gateway.answer(404, {"error": {"message": "Run not found: run_9", "code": "run_not_found"}})
    status = client.status("run_9", profile="orchestrator")
    assert (status.run_id, status.status, status.terminal) == ("run_9", "unknown", True)
    assert status.error == "Run not found: run_9"


def test_steer_true_when_accepted_and_false_on_409(gateway, client):
    gateway.answer(200, {"object": "hermes.run.steer", "run_id": "run_1", "accepted": True})
    assert client.steer("run_1", "also check the logs", profile="researcher") is True
    request = gateway.last
    assert (request["method"], request["path"]) == ("POST", "/p/researcher/v1/runs/run_1/steer")
    assert request["body"] == {"input": "also check the logs"}

    gateway.answer(409, {"error": {"message": "not running", "code": "run_not_accepting_steer"}})
    assert client.steer("run_1", "again", profile="researcher") is False


def test_stop_returns_the_stopping_status(gateway, client):
    gateway.answer(200, {"run_id": "run_1", "status": "stopping"})
    status = client.stop("run_1", profile="researcher")
    assert (gateway.last["method"], gateway.last["path"]) == (
        "POST",
        "/p/researcher/v1/runs/run_1/stop",
    )
    assert gateway.last["body"] is None
    assert (status.status, status.terminal) == ("stopping", False)


def test_stop_of_an_inactive_run_falls_back_to_its_status(gateway, client):
    gateway.answer(409, {"error": {"message": "no agent", "code": "run_not_active"}})
    gateway.answer(200, {"run_id": "run_1", "status": "interrupted", "error": "gateway restarted"})
    status = client.stop("run_1", profile="researcher")
    assert [r["path"] for r in gateway.requests] == [
        "/p/researcher/v1/runs/run_1/stop",
        "/p/researcher/v1/runs/run_1",
    ]
    assert (status.status, status.terminal, status.error) == (
        "interrupted",
        True,
        "gateway restarted",
    )


def test_a_run_lost_in_a_restart_reads_unknown_until_resolved(gateway, client):
    lost = {"run_id": "run_1", "status": "unknown", "admission_id": "a1", "execution_generation": 2}
    gateway.answer(200, lost)
    status = client.status("run_1", profile="orchestrator")
    assert (status.status, status.terminal, status.lost) == ("unknown", False, True)

    gateway.answer(200, {"run_id": "run_1", "admission_id": "a1", "status": "terminal"})
    client.resolve_unknown(status, profile="orchestrator")
    assert (gateway.last["method"], gateway.last["path"]) == (
        "POST",
        "/p/orchestrator/v1/runs/run_1/resolve-unknown",
    )
    assert gateway.last["body"] == {"admission_id": "a1", "execution_generation": 2}


# --- sessions --------------------------------------------------------------------------------


def test_session_messages_reads_the_list_and_empties_on_404(gateway, client):
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    gateway.answer(200, {"messages": messages})
    assert client.session_messages("api_1_a", profile="orchestrator") == messages
    assert gateway.last["path"] == "/p/orchestrator/api/sessions/api_1_a/messages"

    gateway.answer(200, messages)  # a bare list is accepted too
    assert client.session_messages("api_1_a", profile="orchestrator") == messages

    gateway.answer(404, {"error": "Session not found"})
    assert client.session_messages("api_1_zzz", profile="orchestrator") == []


def test_create_session_posts_the_id(gateway, client):
    gateway.answer(200, {"id": "api_1_c", "source": "api_server"})
    assert client.create_session("api_1_c", profile="researcher") is None
    request = gateway.last
    assert (request["method"], request["path"]) == ("POST", "/p/researcher/api/sessions")
    assert request["body"] == {"id": "api_1_c"}


def test_create_session_replay_accepts_only_an_existing_session(gateway, client):
    gateway.answer(409, {"error": {"code": "session_exists", "message": "Already exists"}})
    assert client.create_session("api_1_c", profile="orchestrator") is None
    gateway.answer(409, {"error": {"code": "other_conflict", "message": "Not a replay"}})
    with pytest.raises(RunsConflict, match="other_conflict"):
        client.create_session("api_1_c", profile="orchestrator")


def test_mint_session_id_has_the_hermes_api_shape():
    assert re.fullmatch(r"api_\d{9,}_[0-9a-f]{8}", mint_session_id())
    assert mint_session_id() != mint_session_id()
    assert mint_session_id("wake").startswith("wake_")


# --- FakeHermesRuns --------------------------------------------------------------------------


def test_fake_start_records_the_run_the_call_and_the_session():
    fake = FakeHermesRuns()
    assert isinstance(fake, HermesRuns)

    run_id = fake.start(
        profile="researcher", session_id="api_1_a", text="do T-1", instructions="brief"
    )

    assert run_id.startswith("run_")
    run = fake.runs[run_id]
    assert (run.profile, run.session_id, run.input, run.instructions) == (
        "researcher",
        "api_1_a",
        "do T-1",
        "brief",
    )
    assert fake.status(run_id, profile="researcher") == RunStatus(
        run_id,
        "running",
        "api_1_a",
        None,
        None,
        False,
        fake.status(run_id, profile="researcher").raw,
    )
    assert fake.sessions["api_1_a"] == [{"role": "user", "content": "do T-1"}]
    assert fake.calls[0] == (
        "start",
        {
            "profile": "researcher",
            "session_id": "api_1_a",
            "text": "do T-1",
            "instructions": "brief",
            "idempotency_key": None,
        },
    )


def test_fake_reused_key_replays_the_same_body_and_conflicts_on_a_different_one():
    fake = FakeHermesRuns()
    first = fake.start(profile="researcher", session_id="s", text="t", idempotency_key="task:1:3:0")

    assert (
        fake.start(profile="researcher", session_id="s", text="t", idempotency_key="task:1:3:0")
        == first
    )
    assert len(fake.runs) == 1

    with pytest.raises(RunsConflict) as info:
        fake.start(profile="researcher", session_id="s", text="other", idempotency_key="task:1:3:0")
    assert (info.value.status, info.value.code) == (409, "idempotency_key_conflict")

    # the next attempt carries a new key (A5.1) and starts a fresh run
    second = fake.start(
        profile="researcher", session_id="s", text="t", idempotency_key="task:1:3:1"
    )
    assert second != first


def test_fake_script_hook_simulates_an_agent():
    def agent(run: FakeRun) -> None:
        assert run.status == "running"
        fake.complete(run.run_id, output=f"finished {run.input}")

    fake = FakeHermesRuns(script=agent)
    run_id = fake.start(profile="researcher", session_id="api_1_a", text="T-1")

    status = fake.status(run_id, profile="researcher")
    assert (status.status, status.terminal, status.output) == ("completed", True, "finished T-1")
    assert fake.session_messages("api_1_a", profile="researcher") == [
        {"role": "user", "content": "T-1"},
        {"role": "assistant", "content": "finished T-1"},
    ]


def test_fake_busy_raises_busy_until_cleared():
    fake = FakeHermesRuns()
    fake.set_busy(True)
    with pytest.raises(RunsBusy):
        fake.start(profile="researcher", session_id="s", text="t")
    fake.set_busy(False)
    assert fake.start(profile="researcher", session_id="s", text="t") in fake.runs


def test_fake_steer_only_while_running():
    fake = FakeHermesRuns()
    run_id = fake.start(profile="researcher", session_id="s", text="t")
    assert fake.steer(run_id, "more", profile="researcher") is True
    assert fake.steered[run_id] == ["more"]
    assert fake.sessions["s"][-1] == {"role": "user", "content": "more"}

    fake.complete(run_id, "done")
    assert fake.steer(run_id, "late", profile="researcher") is False
    assert fake.steer("run_missing", "x", profile="researcher") is False
    assert fake.steered[run_id] == ["more"]


def test_fake_stop_answers_stopping_then_reads_cancelled():
    fake = FakeHermesRuns()
    run_id = fake.start(profile="researcher", session_id="s", text="t")

    stopping = fake.stop(run_id, profile="researcher")
    assert (stopping.status, stopping.terminal) == ("stopping", False)

    cancelled = fake.status(run_id, profile="researcher")
    assert (cancelled.status, cancelled.terminal) == ("cancelled", True)
    # stopping a finished run returns its status unchanged
    assert fake.stop(run_id, profile="researcher").status == "cancelled"
    assert fake.stop("run_missing", profile="researcher").status == "unknown"


def test_fake_fail_crash_and_unknown_runs():
    fake = FakeHermesRuns()
    failed = fake.start(profile="researcher", session_id="s1", text="t")
    crashed = fake.start(profile="researcher", session_id="s2", text="t")
    fake.fail(failed, "tool exploded")
    fake.crash(crashed)

    assert fake.status(failed, profile="researcher").error == "tool exploded"
    assert fake.status(failed, profile="researcher").status == "failed"
    lost = fake.status(crashed, profile="researcher")
    assert (lost.status, lost.terminal, lost.lost) == ("unknown", False, True)

    unknown = fake.status("run_nope", profile="researcher")
    assert (unknown.status, unknown.terminal, unknown.lost) == ("unknown", True, False)
    assert [method for method, _ in fake.calls] == ["start", "start"] + ["status"] * 4


def test_fake_holds_a_lost_runs_session_until_it_is_resolved():
    fake = FakeHermesRuns()
    lost = fake.start(profile="orchestrator", session_id="s", text="t")
    fake.crash(lost)
    later = fake.start(profile="orchestrator", session_id="s", text="next")

    assert fake.stop(lost, profile="orchestrator").lost  # the gateway refuses to stop it
    assert fake.steer(lost, "x", profile="orchestrator") is False
    assert fake.status(later, profile="orchestrator").status == "queued"

    fake.resolve_unknown(fake.status(lost, profile="orchestrator"), profile="orchestrator")
    assert fake.status(lost, profile="orchestrator").status == "cancelled"
    assert fake.status(later, profile="orchestrator").status == "running"


def test_fake_create_session_then_messages():
    fake = FakeHermesRuns()
    fake.create_session("api_1_new", profile="orchestrator")
    assert fake.session_messages("api_1_new", profile="orchestrator") == []
    assert fake.session_messages("api_1_none", profile="orchestrator") == []
    assert fake.calls[-2:] == [
        ("session_messages", {"session_id": "api_1_new", "profile": "orchestrator"}),
        ("session_messages", {"session_id": "api_1_none", "profile": "orchestrator"}),
    ]


@pytest.mark.parametrize(
    "cause,unsent",
    [
        (ConnectionRefusedError(), True),
        (socket.gaierror(), True),
        (TimeoutError(), False),
        (ConnectionResetError(), False),
    ],
)
def test_transport_distinguishes_unsent_connection_setup_from_ambiguous_delivery(cause, unsent):
    from urllib.error import URLError

    from research_core.watcher.hermes_api import RunsNotSent

    def failed(request, *, timeout):
        raise URLError(cause)

    native = HermesRuns(base_url="http://fixture", keys=KEYS, opener=failed)
    with pytest.raises(RunsUnavailable) as error:
        native.steer("run", "input", profile="researcher")
    assert isinstance(error.value, RunsNotSent) == unsent
