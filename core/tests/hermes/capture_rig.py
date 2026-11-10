"""A real patched gateway running the real plugin against a stub model, for capture tests.

One gateway serves the Orchestrator and Researcher profiles with their shipped plugin folders. The
stub is an OpenAI-compatible endpoint that keeps each request body exactly as it arrived, so a test
can compare it with what reaches the stream.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from research_core.agent_telemetry.stream import STREAM_VARIABLE

REPO = Path(__file__).resolve().parents[3]
OPENROUTER_GATEWAY = Path(__file__).with_name("openrouter_gateway.py")
API_KEY = "0123456789abcdef0123456789abcdef"
PROOF_WORKSPACE = "wrk-proof"
STUB_MODEL = "moonshotai/kimi-k3.1-pro"
STUB_USAGE = {
    "prompt_tokens": 110,
    "completion_tokens": 7,
    "total_tokens": 117,
    "cost": 0.25,
    "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 5},
    "completion_tokens_details": {"reasoning_tokens": 3},
}
STUB_REASONING = "the stub is thinking"
STUB_ANSWER = "all done"


def chunk(delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> bytes:
    body = {
        "id": "gen-stub",
        "object": "chat.completion.chunk",
        "model": "stub-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        **({"usage": usage} if usage else {}),
    }
    return f"data: {json.dumps(body)}\n\n".encode()


def tool_call(index: int, call_id: str, name: str, arguments: str) -> dict:
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def stream_reply(handler, chunks: list[bytes], *, pause_s: float = 0.0, drop: bool = False) -> None:
    """A streamed answer. With ``drop`` it ends in a broken transfer, not a clean end of stream."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

    def write(data: bytes) -> None:
        handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        handler.wfile.flush()

    for data in chunks:
        write(data)
        time.sleep(pause_s)
    if drop:
        handler.close_connection = True
        handler.connection.close()
        return
    write(b"data: [DONE]\n\n")
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()


def whole_reply(handler, text: str, usage: dict) -> None:
    data = json.dumps(
        {
            "id": "gen-stub-aux",
            "object": "chat.completion",
            "model": "stub-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
    ).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def stub_server(answer: Callable[..., None]) -> ThreadingHTTPServer:
    """An HTTP server on a free port, passing each POST to ``answer(handler, body, arrived_at)``."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            arrived = time.perf_counter()
            answer(self, self.rfile.read(int(self.headers["Content-Length"])), arrived)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class StubModel:
    """Answers a turn with reasoning and ``tool_calls``, and the request after them with text.

    A request with no tools is an auxiliary call such as a title, answered whole. With
    ``usage_from_size``, the prompt tokens it reports follow the request's size, as a provider's do,
    so that Hermes's token-based pruning fires. With ``repeat_tool_calls`` every request gets the
    tool calls, so that a failing tool reaches Hermes's guard.
    """

    def __init__(self, tool_calls: list[dict], *, usage_from_size: bool = False) -> None:
        self.tool_calls = tool_calls
        self.usage_from_size = usage_from_size
        self.repeat_tool_calls = False
        # When set, a request that follows a tool result gets no answer until this is set.
        self.hang_tool_followups: threading.Event | None = None
        # When set, a request that follows a user's message gets no answer until this is set.
        self.hang_user_requests: threading.Event | None = None
        self.lock = threading.Lock()
        self.bodies: list[bytes] = []
        # When each request that follows a tool result arrived.
        self.tool_followups_at: list[float] = []
        self.server = stub_server(self.answer)
        self.url = f"http://openrouter.ai:{self.server.server_address[1]}/api/v1"

    def close(self) -> None:
        self.server.shutdown()

    def main_requests(self) -> list[dict]:
        """The requests of the agent's own turns, in order, without auxiliary calls."""
        with self.lock:
            parsed = [json.loads(body) for body in self.bodies]
        return [body for body in parsed if body.get("tools")]

    def answer(self, handler, raw: bytes, arrived: float) -> None:
        body = json.loads(raw)
        messages = body.get("messages")
        if messages is None:
            handler.send_error(404)
            return
        with self.lock:
            self.bodies.append(raw)
            if messages[-1]["role"] == "tool":
                self.tool_followups_at.append(arrived)
        if self.hang_tool_followups is not None and messages[-1]["role"] == "tool":
            self.hang_tool_followups.wait(120)
            return
        if self.hang_user_requests is not None and messages[-1]["role"] == "user":
            self.hang_user_requests.wait(120)
            return
        usage = STUB_USAGE
        if self.usage_from_size:
            usage = {**STUB_USAGE, "prompt_tokens": len(raw) // 4}
        if not body.get("tools"):
            whole_reply(handler, "a title", STUB_USAGE)
            return
        if messages[-1]["role"] == "tool" and not self.repeat_tool_calls:
            chunks = [chunk({"content": STUB_ANSWER}), chunk({}, "stop", usage)]
        else:
            chunks = [
                chunk({"reasoning": STUB_REASONING}),
                chunk({"tool_calls": self.tool_calls}),
                chunk({}, "tool_calls", usage),
            ]
        stream_reply(handler, chunks)


def _write_profile(home: Path, role: str, workspace: Path, prune_tokens: int) -> None:
    profile = home / "profiles" / role
    (profile / "plugins").mkdir(parents=True)
    (profile / "local").mkdir()
    (profile / "plugins" / role).symlink_to(REPO / role / "plugins" / role)
    (profile / "local" / "core-path.txt").write_text(str(REPO / "core" / "src"))
    shutil.copy(REPO / role / "SOUL.md", profile / "SOUL.md")
    # JSON is YAML, and only what the gateway needs for these tests differs from a prepared profile.
    (profile / "config.yaml").write_text(
        json.dumps(
            {
                "model": {"default": STUB_MODEL, "provider": "openrouter"},
                "agent": {"reasoning_effort": "low"},
                "provider_routing": {"models": {STUB_MODEL: {"only": ["moonshotai"]}}},
                "plugins": {"enabled": [role], "stream_reasoning_deltas": True},
                "platform_toolsets": {"api_server": [f"ide-{role}"]},
                "known_plugin_toolsets": {"api_server": [f"ide-{role}"]},
                "tools": {"tool_search": {"enabled": "off"}},
                "terminal": {"cwd": str(workspace)},
                "memory": {"memory_enabled": False},
                "auxiliary": {"title_generation": {"provider": "main", "model": STUB_MODEL}},
                "compression": {"proactive_prune_tokens": prune_tokens},
                # As ``prepare.py`` sets them, so that the guard stops a failing tool as it would.
                "tool_loop_guardrails": {
                    "hard_stop_enabled": True,
                    "hard_stop_after": {"same_tool_failure": 5},
                },
            }
        )
    )
    (profile / ".env").write_text(
        f"API_SERVER_ENABLED=true\nAPI_SERVER_KEY={API_KEY}\nOPENROUTER_API_KEY=stub\n"
        f"TERMINAL_CWD={workspace}\nIDE_WORKSPACE={workspace}\nIDE_PLATFORM=fake\n"
    )


@dataclass
class RealGateway:
    home: Path
    workspace: Path
    port: int
    log: Path
    # A file in place of the container's stream: every line the gateway and its commands wrote.
    stream: Path
    process: subprocess.Popen
    launch: Callable[[], subprocess.Popen]

    def request(self, method: str, profile: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/p/{profile}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    def run_turn(self, profile: str, session_id: str, text: str, timeout_s: float = 180) -> str:
        """Start a run, wait for it to complete, and return its ID."""
        run_id = self.request(
            "POST", profile, "/v1/runs", {"input": text, "session_id": session_id}
        )["run_id"]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.request("GET", profile, f"/v1/runs/{run_id}")
            if status["status"] in ("completed", "failed", "cancelled", "interrupted"):
                assert status["status"] == "completed", (status, self.log_tail())
                return run_id
            time.sleep(0.05)
        raise AssertionError(f"run {run_id} did not finish: {self.log_tail()}")

    def log_tail(self) -> str:
        return self.log.read_text(errors="replace")[-4000:]

    def stop(self) -> int:
        """SIGTERM, as ``image/processes.py`` sends it. Returns the exit code."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=60)

    def restart(self) -> None:
        """A new gateway process on the same home and workspace."""
        self.stop()
        self.process = self.launch()
        self.wait_until_serving()

    def wait_until_serving(self) -> None:
        deadline = time.monotonic() + 120
        while True:
            try:
                self.request("GET", "orchestrator", "/v1/capabilities")
                return
            except OSError:
                assert self.process.poll() is None, self.log_tail()
                assert time.monotonic() < deadline, self.log_tail()
                time.sleep(0.25)


def start_gateway(
    root: Path,
    python: str,
    stub_url: str,
    port: int,
    *,
    prepare_workspace: Callable[[Path], None] | None = None,
    prune_tokens: int = 0,
    result_chars: int = 0,
    stdout_on_stream: bool = False,
) -> RealGateway:
    """``prune_tokens`` is the profiles' ``compression.proactive_prune_tokens``. They ship with
    64,000. ``result_chars`` adds that much new text to each ``settings`` result. With
    ``stdout_on_stream`` the gateway's unbuffered stdout shares the stream, as in the image."""
    home = root / "home"
    workspace = root / "workspace"
    home.mkdir()
    workspace.mkdir()
    if prepare_workspace is not None:
        prepare_workspace(workspace)
    (home / ".env").write_text(
        f"API_SERVER_ENABLED=true\nAPI_SERVER_HOST=127.0.0.1\nAPI_SERVER_PORT={port}\n"
        f"API_SERVER_KEY={API_KEY}\nOPENROUTER_API_KEY=stub\nGATEWAY_MULTIPLEX_PROFILES=true\n"
        f"IDE_WORKSPACE={workspace}\nIDE_PLATFORM=fake\n"
    )
    for role in ("orchestrator", "researcher"):
        _write_profile(home, role, workspace, prune_tokens)
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "HERMES_REAL_HOME": str(home),
        "IDE_WORKSPACE": str(workspace),
        "IDE_RUNTIME_ID": "runtime-under-test",
        "IDE_IMAGE": "image-under-test",
        "IDE_PLATFORM": "fake",
        "GATEWAY_MULTIPLEX_PROFILES": "true",
        "STUB_OPENROUTER_URL": stub_url,
        "STUB_RESULT_CHARS": str(result_chars),
    }
    log = root / "gateway.log"
    stream = root / "telemetry-stream"
    env[STREAM_VARIABLE] = str(stream)
    env["IDE_WORKSPACE_ID"] = PROOF_WORKSPACE

    def launch() -> subprocess.Popen:
        with log.open("ab") as output, (stream if stdout_on_stream else log).open("ab") as out:
            return subprocess.Popen(
                [python, str(OPENROUTER_GATEWAY)],
                env={**env, "PYTHONUNBUFFERED": "1"} if stdout_on_stream else env,
                stdout=out,
                stderr=output,
            )

    gateway = RealGateway(home, workspace, port, log, stream, launch(), launch)
    gateway.wait_until_serving()
    return gateway
