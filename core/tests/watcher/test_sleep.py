"""``research workspace sleep`` and ``wake``: the host's sleep cycle (``watcher/sleep.py``)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from research_core.store import events, sessions, settings
from research_core.watcher import sleep, wakes
from tests.watcher._support import (
    HARNESS,
    advance_executions,
    events_of,
    make_ctx,
    set_setting,
    start_run,
    working_session,
)

CHAT_SESSION = "api_1761690000_chat0001"


class StepClock:
    """A monotonic clock that each fake sleep moves forward; ``on_sleep`` runs at each step."""

    def __init__(self, on_sleep=None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


def chat_run(ctx, session_id: str = CHAT_SESSION) -> str:
    sessions.register(
        ctx.wstore, HARNESS, session_id=session_id, profile="orchestrator", kind="chat"
    )
    return start_run(ctx, session_id)


def go_to_sleep(ctx, clock: StepClock | None = None, wait_s: float = 120.0) -> dict:
    clock = clock or StepClock()
    return sleep.go_to_sleep(ctx, wait_s=wait_s, sleep=clock.sleep, clock=clock)


def test_sleep_pauses_drains_and_lets_a_chat_turn_finish(tmp_path):
    ctx = make_ctx(tmp_path)
    _task, researcher, researcher_run = working_session(ctx)
    chat = chat_run(ctx)
    clock = StepClock(on_sleep=lambda n: n == 3 and ctx.hermes.complete(chat, "answered"))

    report = go_to_sleep(ctx, clock)

    assert settings.get(ctx.wstore, "paused") is True and report["paused_by_sleep"] is True
    assert sleep.paused_by_sleep(ctx.wstore)
    assert report["drained"]["stopped"] == [researcher["id"]]
    assert sessions.read(ctx.wstore, researcher["id"])["drained"] is True
    assert ctx.hermes.status(researcher_run, profile="researcher").terminal
    orchestrator = report["orchestrator_runs"]
    assert orchestrator["ended"] == [CHAT_SESSION] and orchestrator["stopped"] == []
    assert ("stop", chat) not in [(n, kw.get("run_id")) for n, kw in ctx.hermes.calls]
    assert report["still_running"] == []
    assert ctx.wstore.get_meta(sleep.SLEEP_STARTED_KEY) == report["sleep_started"]
    assert ctx.wstore.get_meta("paused_drained") == "1"  # the paused tick will not drain again
    [event] = events_of(ctx.wstore, "workspace.sleep")
    assert event["payload"]["drained"] == [researcher["id"]]


def test_sleep_stops_an_orchestrator_run_that_outlasts_the_wait(tmp_path):
    ctx = make_ctx(tmp_path)
    chat = chat_run(ctx)

    report = go_to_sleep(ctx, wait_s=5)

    orchestrator = report["orchestrator_runs"]
    assert orchestrator["stopped"] == [CHAT_SESSION] and orchestrator["quiet"] is True
    assert orchestrator["waited_s"] >= 5
    assert sessions.read(ctx.wstore, CHAT_SESSION)["stopped_by"] == sessions.SLEEP_STOP
    assert ctx.hermes.status(chat, profile="orchestrator").status == "cancelled"
    assert report["still_running"] == []


def test_a_failed_stop_is_reported_as_still_running(tmp_path):
    ctx = make_ctx(tmp_path)
    chat_run(ctx)

    def refuse(run_id, *, profile):
        from research_core.watcher.hermes_api import RunsError

        raise RunsError("gateway said no")

    ctx.hermes.stop = refuse
    report = go_to_sleep(ctx, wait_s=0)

    assert report["orchestrator_runs"]["errors"][0]["session"] == CHAT_SESSION
    assert sessions.read(ctx.wstore, CHAT_SESSION)["stopped_by"] is None
    assert report["still_running"] == [f"session:{CHAT_SESSION}:running"]
    assert sleep.volatile(report["still_running"], {"databases": {}}) == [
        f"session:{CHAT_SESSION}:running"
    ]


def test_a_wake_stopped_for_sleep_retries_without_using_an_attempt(tmp_path):
    ctx = make_ctx(tmp_path)  # recovery.max_attempts defaults to 2
    with ctx.store.tx(HARNESS) as tx:
        seq = tx.event("task.asked", "T-1", {"question": "which seed?"})
    advance_executions(ctx)
    first = [kw for name, kw in ctx.hermes.calls if name == "start"][-1]

    go_to_sleep(ctx, wait_s=0)
    assert sessions.read(ctx.wstore, first["session_id"])["stopped_by"] == sessions.SLEEP_STOP

    sleep.wake_up(ctx)
    advance_executions(ctx)  # after the wake: released, then started again at once
    assert ctx.wstore.get_meta(f"wake_planned_stops:cow_fake:{seq}") == "1"
    second = [kw for name, kw in ctx.hermes.calls if name == "start"][-1]
    assert second["session_id"] != first["session_id"]
    assert second["idempotency_key"] == f"event:cow_fake:{seq}:2"  # a key of its own
    row = ctx.store.one("SELECT attempts FROM handled_events WHERE seq = ?", (seq,))
    assert wakes.counted_attempts(ctx, "cow_fake", seq, row["attempts"]) == 1

    # Two real failures are still allowed: the stop for sleep did not count.
    ctx.hermes.fail(sessions.read(ctx.wstore, second["session_id"])["run_id"], "model error")
    advance_executions(ctx)
    third = [kw for name, kw in ctx.hermes.calls if name == "start"][-1]
    assert third["idempotency_key"] == f"event:cow_fake:{seq}:3"
    assert events_of(ctx.wstore, "wake.failed") == []


def test_sleep_twice_is_safe(tmp_path):
    ctx = make_ctx(tmp_path)
    working_session(ctx)
    go_to_sleep(ctx)
    again = go_to_sleep(ctx)

    assert again["paused_by_sleep"] is True and again["still_running"] == []
    paused = [e for e in events_of(ctx.wstore, "settings.updated") if e["subject"] == "paused"]
    assert len(paused) == 1
    assert len(events_of(ctx.wstore, "workspace.sleep")) == 2


def test_wake_clears_sleeps_pause_and_resumes_the_drained_session(tmp_path):
    ctx = make_ctx(tmp_path)
    _task, researcher, _run = working_session(ctx)
    go_to_sleep(ctx)

    result = sleep.wake_up(ctx)

    assert result == {
        "paused": False,
        "unpaused": True,
        "slept_since": result["slept_since"],
        "drained_sessions_to_resume": [researcher["id"]],
    }
    assert result["slept_since"] is not None
    assert ctx.wstore.get_meta(sleep.SLEEP_STARTED_KEY) is None
    [resumed] = events_of(ctx.wstore, "workspace.resumed")
    assert resumed["payload"]["drained_sessions"] == [researcher["id"]]

    advance_executions(ctx)  # the next tick's handler resumes it with its own session id
    assert events.unhandled(ctx.wstore, {"workspace.resumed"}) == []
    assert sessions.read(ctx.wstore, researcher["id"])["drained"] is False
    restarted = [kw for name, kw in ctx.hermes.calls if name == "start"][-1]
    assert restarted["session_id"] != researcher["id"]
    assert sessions.read(ctx.wstore, restarted["session_id"])["parent"] == researcher["id"]


def test_sleep_and_wake_keep_the_users_own_pause(tmp_path):
    ctx = make_ctx(tmp_path)
    set_setting(ctx, "paused", True)  # the user paused, citing their message

    assert go_to_sleep(ctx)["paused_by_sleep"] is False
    result = sleep.wake_up(ctx)

    assert result["paused"] is True and result["unpaused"] is False
    assert events_of(ctx.wstore, "workspace.resumed") == []


# --- flush ------------------------------------------------------------------------------------


def wal_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")  # keep every write in the WAL until asked
    conn.execute("CREATE TABLE IF NOT EXISTS notes(text TEXT)")
    return conn


def read_main_file_only(path: Path, query: str) -> list:
    """Read the database file as it is on disk, ignoring any WAL (``immutable=1``)."""
    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    try:
        return conn.execute(query).fetchall()
    finally:
        conn.close()


def test_flush_moves_every_write_into_the_main_files(tmp_path):
    ctx = make_ctx(tmp_path)
    home = tmp_path / "profiles" / "orchestrator"
    orchestrator_state = wal_database(home / "state.db")
    researcher_state = wal_database(tmp_path / "profiles" / "researcher" / "state.db")
    # Writes right before the flush, through connections other than the flush's own.
    ctx.wstore.set_meta("written_before_sleep", "yes")
    orchestrator_state.execute("INSERT INTO notes VALUES ('orchestrator turn')")
    researcher_state.execute("INSERT INTO notes VALUES ('researcher turn')")
    env = {"HERMES_HOME": str(home)}
    assert (home / "state.db-wal").stat().st_size > 0

    flushed = sleep.flush_everything(ctx.ws, env, ("researcher",), sleep=lambda _s: None)

    assert set(flushed["databases"]) == {
        str(ctx.ws.store_path),
        str(ctx.coworld.store_path),
        str(home / "state.db"),
        str(tmp_path / "profiles" / "researcher" / "state.db"),
    }
    for path, report in flushed["databases"].items():
        assert report == {"busy": False, "wal_bytes": 0, "fsynced": True}, path
    assert flushed["synced"] is True
    assert sleep.volatile([], flushed) == []
    meta = read_main_file_only(
        ctx.ws.store_path, "SELECT value FROM meta WHERE key = 'written_before_sleep'"
    )
    assert meta == [("yes",)]
    assert read_main_file_only(home / "state.db", "SELECT text FROM notes") == [
        ("orchestrator turn",)
    ]
    orchestrator_state.close()
    researcher_state.close()


def test_a_database_a_reader_keeps_busy_is_volatile():
    flushed = {"databases": {"/w/workspace.db": {"busy": True, "wal_bytes": 4096, "fsynced": True}}}
    assert sleep.volatile([], flushed) == ["database:/w/workspace.db:not-checkpointed"]
    assert sleep.volatile([], {"error": "OSError: gone"}) == ["flush:failed:OSError: gone"]


# --- the commands -----------------------------------------------------------------------------


def research(args: list[str], env: dict) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, "-m", "research_core.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, completed.stderr
    return completed.returncode, json.loads(lines[-1])


def test_sleep_and_wake_commands_on_an_idle_workspace(tmp_path):
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("IDE_", "HERMES_"))},
        "IDE_WORKSPACE": str(tmp_path / "ws"),
        "HERMES_HOME": str(tmp_path / "profiles" / "orchestrator"),
        "IDE_PLATFORM": "fake",
    }

    code, slept = research(["workspace", "sleep", "--wait", "0"], env)
    assert code == 0, slept
    assert slept["ok"] is True and slept["paused"] is True
    assert slept["volatile"] == [] and slept["flushed"]["synced"]
    code, status = research(["watcher", "status"], env)
    assert status["paused"] is True and status["sleep_started"]

    code, woke = research(["workspace", "wake"], env)
    assert code == 0 and woke["ok"] is True and woke["unpaused"] is True
    assert woke["paused"] is False

    code, status = research(["watcher", "status"], env)
    assert status["paused"] is False and status["sleep_started"] is None


def test_a_sleep_that_cannot_stop_hands_the_workspace_back_awake(tmp_path):
    home = tmp_path / "profiles" / "orchestrator"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("IDE_", "HERMES_"))},
        "IDE_WORKSPACE": str(tmp_path / "ws"),
        "HERMES_HOME": str(home),
        "IDE_PLATFORM": "fake",
    }
    # A reader holding the Orchestrator's state.db keeps its checkpoint from finishing.
    state = wal_database(home / "state.db")
    state.execute("INSERT INTO notes VALUES ('turn')")
    reader = sqlite3.connect(home / "state.db", isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM notes").fetchall()

    code, slept = research(["workspace", "sleep", "--wait", "0"], env)
    reader.close()
    state.close()

    assert code == 1 and slept["ok"] is False, slept
    assert slept["volatile"] == [f"database:{home / 'state.db'}:not-checkpointed"]
    assert slept["paused"] is False and slept["woke"]["unpaused"] is True
    code, status = research(["watcher", "status"], env)
    assert status["paused"] is False and status["sleep_started"] is None


def test_sleep_refuses_without_the_profile_home(tmp_path):
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("IDE_", "HERMES_"))},
        "IDE_WORKSPACE": str(tmp_path / "ws"),
    }
    code, result = research(["workspace", "sleep"], env)
    assert code == 1 and "HERMES_HOME" in result["error"]
