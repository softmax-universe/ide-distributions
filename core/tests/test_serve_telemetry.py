"""``research serve`` and its telemetry writer: what it records, and that a stop waits for none."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from research_core import layout, serve
from research_core.agent_telemetry.stream import STREAM_VARIABLE
from research_core.client import SeamError
from research_core.plugin.executor import workspace_process_env
from research_core.serve import SERVE_WRITER
from research_core.store.db import Store
from tests.telemetry.conftest import stored_records
from tests.telemetry.test_writer import StuckStream
from tests.test_serve import start_seam


def test_research_serve_names_itself_and_redacts_no_text(tmp_path, stream_file):
    seam = start_seam(tmp_path, coworld=False)
    seam.server.telemetry.capture_operation("admission", "accepted", session_id="ses_1")
    seam.server.shutdown()
    assert seam.server.telemetry.flush(5.0)
    records = [r for r in stored_records(stream_file) if r["action"] == "admission"]
    assert [record["action"] for record in records] == ["admission"]
    assert records[0]["producer"] == SERVE_WRITER
    assert records[0]["redaction_version"] == "none"
    seam.server.shutdown()  # Safe to call more than once.


def test_a_reply_that_cannot_be_written_records_one_outcome(tmp_path, stream_file, monkeypatch):
    """The client went away after the work was done. That is not a refusal."""
    seam = start_seam(tmp_path, coworld=False)
    write_reply = serve._Handler._json
    failures = [BrokenPipeError()]

    def fails_once(self, status, payload, headers=None):
        if failures:
            raise failures.pop()
        write_reply(self, status, payload, headers)

    monkeypatch.setattr(serve._Handler, "_json", fails_once)
    with pytest.raises(SeamError):  # The stand-in fails once, so the error reply does get through.
        seam.client.open()
    seam.server.shutdown()
    assert seam.server.telemetry.flush(5.0)
    outcomes = [
        (record["stage"], record["details"]["status"])
        for record in stored_records(stream_file)
        if record.get("action") == "admission"
    ]
    assert outcomes == [("accepted", 200)]


def _start_serve(tmp_path: Path) -> subprocess.Popen:
    home = tmp_path / "profiles" / "orchestrator"
    home.mkdir(parents=True)
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "IDE_WORKSPACE": str(tmp_path / "workspace"),
        "IDE_PLATFORM": "fake",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "research_core.cli", "serve", "--host", "127.0.0.1", "--port", "0"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_a_stuck_stream_holds_up_no_tool_and_no_stop(tmp_path, monkeypatch):
    """A tool that commits still answers, and ``research serve`` still dies on SIGTERM."""
    stuck = StuckStream(tmp_path)
    monkeypatch.setenv(STREAM_VARIABLE, str(stuck.path))
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    env = {
        **os.environ,
        "IDE_WORKSPACE": str(tmp_path / "tool-workspace"),
        "IDE_WORKSPACE_ENV": sys.prefix,
        "IDE_PLATFORM": "fake",
        "IDE_ROLE": "orchestrator",
        "IDE_SESSION_ID": "ses_tool",
    }
    ws = layout.from_env(env)
    Store.open(ws.store_path, kind="workspace").close()
    args = {"action": "update", "key": "max_researchers", "value": 2, "cites": "a test"}
    started = time.monotonic()
    done = subprocess.run(
        [str(ws.venv_python), "-m", "research_core.cli", "tool", "settings"]
        + ["--args", json.dumps(args)],
        env=workspace_process_env(env, ws),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["ok"] is True
    assert time.monotonic() - started < 30

    process = _start_serve(tmp_path)
    try:
        while "research serve:" not in process.stdout.readline():
            assert process.poll() is None
        time.sleep(1.0)  # Long enough for its writer to reach the stuck stream.
        process.send_signal(signal.SIGTERM)
        started = time.monotonic()
        assert process.wait(timeout=30) == -signal.SIGTERM
        assert time.monotonic() - started < 2
    finally:
        process.kill()
        process.stdout.close()


def test_sigterm_ends_research_serve_at_once_and_every_line_written_parses(tmp_path, stream_file):
    process = _start_serve(tmp_path)
    try:
        # A first line shows that the writer is alive in that process and can reach the stream.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not stream_file.exists():
            assert process.poll() is None, process.stdout.read()
            time.sleep(0.05)
        sent_before = len(stored_records(stream_file))
        assert sent_before, "research serve wrote no line"
        process.send_signal(signal.SIGTERM)
        # Dying by the signal, as it did before this writer existed, rather than a clean exit.
        assert process.wait(timeout=30) == -signal.SIGTERM
    finally:
        process.kill()
        process.stdout.close()
    for line in filter(None, stream_file.read_bytes().splitlines()):
        assert json.loads(line)["service"] == "ide-agent-telemetry"
