"""``research store`` apply, export, snapshot and events (plan Task 2.10, design A.10, A.11).

Every test goes through ``cli.main`` with ``IDE_WORKSPACE`` pointing at a fresh workspace that
has one registered, active Coworld, so the commands pick their stores the way the bridge's calls
will.
"""

from __future__ import annotations

import hashlib
import json
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research_core import cli, layout
from research_core.store import cli as store_cli
from research_core.store import coworlds, directives, goals, migrate, settings, tasks
from research_core.store._common import now_iso
from research_core.store.actors import Actor
from research_core.store.db import Store, quiesce_busy_timeout_ms
from research_core.store.migrate import _copyable_tables
from research_core.watcher import sleep

COWORLD_ID = "cow-x"
HARNESS = Actor.harness()
USER = "user:james"


@dataclass
class Workspace:
    ws: layout.Workspace
    wstore: Store
    store: Store


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Workspace:
    """A workspace with an explicitly selected CLI Coworld; both stores open."""
    monkeypatch.setenv("IDE_COWORLD_ID", COWORLD_ID)
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    ws = layout.from_env()
    wstore = Store.open(ws.store_path, kind="workspace")
    coworlds.register(wstore, HARNESS, COWORLD_ID, folder="cow_x")
    coworlds.activate(wstore, HARNESS, COWORLD_ID)
    store = Store.open(ws.coworld(COWORLD_ID).store_path, kind="coworld")
    yield Workspace(ws, wstore, store)
    store.close()
    wstore.close()


def make_goal(store: Store) -> dict:
    return goals.create(
        store, HARNESS, title="Beat the starter", end_state="margin > 0", reason="r"
    )


def run(capsys, *argv: str) -> tuple[int, dict]:
    """Run ``research store ...``; the exit code and the last stdout line parsed as JSON."""
    code = cli.main(["store", *argv])
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1]) if out else {}


def apply(capsys, op: str, args: dict, *extra: str, actor: str = USER) -> tuple[int, dict]:
    if not op.startswith("tasks."):
        return run(capsys, "apply", "--actor", actor, *extra, op, json.dumps(args))
    from research_core.serve import SeamServer
    from research_core.watcher.hermes_api import FakeHermesRuns

    server = SeamServer(ws=layout.from_env(), hermes=FakeHermesRuns(), env={})
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        return run(capsys, "apply", "--actor", actor, *extra, op, json.dumps(args))
    finally:
        server.shutdown()
        thread.join(timeout=5)


# --- apply ----------------------------------------------------------------------------------


def test_apply_goal_set_status_round_trip_with_a_user_actor(workspace, capsys):
    goal = make_goal(workspace.store)
    before = workspace.store.last_seq()
    code, out = apply(
        capsys,
        "goals.set_status",
        {"goal_id": goal["id"], "status": "parked", "status_reason": "holiday"},
    )
    assert code == 0, out
    assert out["ok"] is True and out["op"] == "goals.set_status" and out["store"] == "coworld"
    assert out["record"]["id"] == "G-1" and out["record"]["status"] == "parked"
    assert out["seq"] == workspace.store.last_seq() == before + 1
    event = workspace.store.events_after(before)[0]
    assert (event.kind, event.actor) == ("goal.status", USER)
    assert goals.read(workspace.store, 1)["status"] == "parked"
    assert workspace.store.unmarked_changes() == []


def test_apply_takes_integer_ids_too_and_goals_update(workspace, capsys):
    make_goal(workspace.store)
    code, out = apply(capsys, "goals.update", {"goal_id": 1, "title": "Beat the champion"})
    assert code == 0, out
    assert out["record"]["title"] == "Beat the champion"


def test_apply_refuses_an_op_outside_the_allow_list(workspace, capsys):
    code, out = apply(capsys, "goals.create", {"title": "x"})
    assert code == 1
    assert out["ok"] is False
    assert "goals.create" in out["error"] and "not an op store apply runs" in out["error"]
    assert "goals.set_status" in out["error"]
    assert workspace.store.q("SELECT * FROM goals") == []


def test_apply_refuses_a_session_actor(workspace, capsys):
    make_goal(workspace.store)
    code, out = apply(
        capsys,
        "goals.set_status",
        {"goal_id": 1, "status": "parked", "status_reason": "r"},
        actor="session:api_1761690245_0badcafe",
    )
    assert code == 1
    assert "sessions use the tools" in out["error"]
    assert goals.read(workspace.store, 1)["status"] == "active"


def test_apply_harness_actor_is_allowed(workspace, capsys):
    make_goal(workspace.store)
    code, out = apply(
        capsys,
        "goals.set_status",
        {"goal_id": 1, "status": "parked", "status_reason": "r"},
        actor="harness",
    )
    assert code == 0, out
    assert out["record"]["status"] == "parked"


def test_apply_bad_actor_and_bad_json_are_refusals_not_tracebacks(workspace, capsys):
    code, out = run(capsys, "apply", "--actor", "nobody", "goals.update", "{}")
    assert code == 1 and "not an actor" in out["error"]
    code, out = run(capsys, "apply", "--actor", USER, "goals.update", "{not json")
    assert code == 1 and "not JSON" in out["error"]
    code, out = run(capsys, "apply", "--actor", USER, "goals.update", "[1, 2]")
    assert code == 1 and "JSON object" in out["error"]


def test_apply_arguments_that_do_not_fit_are_a_refusal(workspace, capsys):
    make_goal(workspace.store)
    code, out = apply(capsys, "goals.set_status", {"goal_id": 1, "status": "parked"})
    assert code == 1
    assert "arguments do not fit goals.set_status" in out["error"]
    assert "status_reason" in out["error"]


def test_apply_reports_every_library_problem(workspace, capsys):
    make_goal(workspace.store)
    code, out = apply(
        capsys, "goals.set_status", {"goal_id": 1, "status": "done", "status_reason": " "}
    )
    assert code == 1
    assert out["error"] == "goals.set_status refused"
    assert len(out["problems"]) == 2, out["problems"]


def test_apply_wrong_kind_of_display_id_is_refused(workspace, capsys):
    make_goal(workspace.store)
    code, out = apply(
        capsys, "goals.set_status", {"goal_id": "T-1", "status": "parked", "status_reason": "r"}
    )
    assert code == 1
    assert out["problems"] == ["goal_id is 'T-1', a task id; expected a goal id"]


def test_apply_settings_update_routes_by_the_keys_scope(workspace, capsys):
    code, out = apply(capsys, "settings.update", {"key": "paused", "value": True, "cites": "m1"})
    assert code == 0, out
    assert out["store"] == "workspace" and out["record"]["scope"] == "workspace"
    assert settings.get([workspace.wstore, workspace.store], "paused") is True
    assert out["seq"] == workspace.wstore.last_seq()

    code, out = apply(
        capsys, "settings.update", {"key": "submissions.mode", "value": "ask", "cites": "m2"}
    )
    assert code == 0, out
    assert out["store"] == "coworld"
    assert settings.get(workspace.store, "submissions.mode") == "ask"
    assert out["seq"] == workspace.store.last_seq()

    code, out = apply(capsys, "settings.update", {"key": "no.such", "value": 1, "cites": "m3"})
    assert code == 1 and "unknown setting" in out["problems"][0]
    code, out = apply(capsys, "settings.update", {"value": 1, "cites": "m3"})
    assert code == 1 and out["problems"] == ["key is required: the setting to change"]


def test_apply_pause_and_resume_set_paused_citing_the_ui_action(workspace, capsys):
    code, out = apply(capsys, "workspace.pause", {"cites": "ui:action:7"})
    assert code == 0, out
    assert out["store"] == "workspace"
    assert out["record"]["key"] == "paused" and out["record"]["value"] is True
    assert settings.get(workspace.wstore, "paused") is True
    event = workspace.wstore.events_after(out["seq"] - 1)[0]
    assert event.kind == "settings.updated" and event.payload["cites"] == "ui:action:7"
    assert event.actor == USER

    code, out = apply(capsys, "workspace.resume", {"cites": "ui:action:8"})
    assert code == 0, out
    assert out["record"]["value"] is False and out["record"]["previous"] is True
    assert settings.get(workspace.wstore, "paused") is False

    code, out = apply(capsys, "workspace.pause", {})
    assert code == 1 and "cites is required" in out["problems"][0]
    code, out = apply(capsys, "workspace.pause", {"cites": "x", "value": True})
    assert code == 1 and "arguments do not fit workspace.pause" in out["error"]


def test_apply_tasks_reject_and_approve(workspace, capsys):
    make_goal(workspace.store)
    researcher = Actor.session("api_1761690300_researcher1")
    first = tasks.propose(
        workspace.store, researcher, goal_id=1, type="research", title="t", description="d"
    )
    second = tasks.propose(
        workspace.store, researcher, goal_id=1, type="research", title="u", description="d"
    )
    code, out = apply(capsys, "tasks.reject", {"task_id": first["id"], "next_step": "ask again"})
    assert code == 0, out
    assert out["record"]["status"] == "abandoned"
    assert out["record"]["failure"] == {
        "what_failed": "rejected by the user",
        "next_step": "ask again",
    }
    code, out = apply(capsys, "tasks.approve", {"task_id": second["id"]})
    assert code == 0, out
    assert out["record"]["status"] == "unclaimed" and out["record"]["approved_by"] == USER


def test_apply_tasks_update_and_abandon(workspace, capsys):
    make_goal(workspace.store)
    task = tasks.create(
        workspace.store, HARNESS, goal_id=1, type="research", title="t", description="d"
    )
    code, out = apply(capsys, "tasks.update", {"task_id": task["id"], "title": "better"})
    assert code == 0, out
    assert out["record"]["title"] == "better"
    failure = {"what_failed": "no longer wanted", "next_step": "nothing"}
    code, out = apply(capsys, "tasks.abandon", {"task_id": task["id"], "failure": failure})
    assert code == 0, out
    assert out["record"]["status"] == "abandoned"


def test_apply_directives_add_routes_by_scope_and_update_retire_by_home(workspace, capsys):
    make_goal(workspace.store)
    code, out = apply(
        capsys, "directives.add", {"text": "No Fridays", "scope": "workspace", "source": "m1"}
    )
    assert code == 0, out
    assert out["store"] == "workspace" and out["record"]["id"] == "W-1"
    code, out = apply(
        capsys, "directives.add", {"text": "Prefer small diffs", "scope": "goal:1", "source": "m2"}
    )
    assert code == 0, out
    assert out["store"] == "coworld" and out["record"]["id"] == "D-1"

    # The id alone says which directive is meant, so no --store is needed.
    code, out = apply(capsys, "directives.update", {"directive_id": "W-1", "text": "Never Fridays"})
    assert code == 0, out
    assert out["store"] == "workspace"
    assert directives.read(workspace.wstore, 1)["text"] == "Never Fridays"
    assert directives.read(workspace.store, 1)["text"] == "Prefer small diffs"

    code, out = apply(capsys, "directives.retire", {"directive_id": "D-1"})
    assert code == 0, out
    assert out["store"] == "coworld" and out["record"]["status"] == "retired"
    assert directives.read(workspace.wstore, 1)["status"] == "active"

    code, out = apply(capsys, "directives.retire", {"directive_id": "D-9"})
    assert code == 1 and out["problems"] == ["D-9 does not exist"]
    code, out = apply(capsys, "directives.retire", {"directive_id": "W-9"})
    assert code == 1 and out["problems"] == ["W-9 does not exist"]
    for bad in ({}, {"directive_id": 1}, {"directive_id": "G-1"}):
        code, out = apply(capsys, "directives.retire", bad)
        assert code == 1 and "W-<n>" in out["problems"][0], out


def test_apply_refuses_a_store_that_contradicts_the_directive_id(workspace, capsys):
    make_goal(workspace.store)
    apply(capsys, "directives.add", {"text": "No Fridays", "scope": "workspace", "source": "m1"})
    apply(capsys, "directives.add", {"text": "Small diffs", "scope": "goal:1", "source": "m2"})

    code, out = apply(capsys, "directives.retire", {"directive_id": "W-1"}, "--store", "coworld")

    assert code == 1 and "W-1" in out["error"] and "workspace" in out["error"]
    assert directives.read(workspace.store, 1)["status"] == "active"
    assert directives.read(workspace.wstore, 1)["status"] == "active"


def test_apply_coworld_switch_delegates_to_the_coworld_command(workspace, capsys, monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, stdout='log line\n{"coworld_id": "cow-y", "active": true}\n', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    code, out = apply(capsys, "workspace.coworld_switch", {"coworld": "cow-y", "cites": "m9"})
    assert code == 0, out
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        sys.executable,
        "-m",
        "research_core.cli",
        "coworld",
        "switch",
        "cow-y",
        "--cites",
        "m9",
    ]
    assert kwargs["env"]["IDE_ACTOR"] == USER
    assert kwargs["env"]["IDE_WORKSPACE"] == str(workspace.ws.root)
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert out["store"] == "workspace"
    assert out["record"] == {"coworld_id": "cow-y", "active": True}
    assert out["seq"] == workspace.wstore.last_seq()

    code, out = apply(capsys, "workspace.coworld_switch", {"coworld": "cow-z"})
    assert code == 0, out
    assert calls[1][0][-1] == "cow-z"  # no --cites when none was given


def test_apply_coworld_switch_failure_carries_the_output(workspace, capsys, monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, stdout="partial\n", stderr="boom: no such")

    monkeypatch.setattr(subprocess, "run", fake_run)
    code, out = apply(capsys, "workspace.coworld_switch", {"coworld": "cow-y"})
    assert code == 1
    assert out["error"] == "workspace.coworld_switch refused"
    assert out["problems"][0] == "research coworld switch cow-y exited 3"
    assert "boom: no such" in out["problems"] and "partial" in out["problems"]

    code, out = apply(capsys, "workspace.coworld_switch", {"cites": "m"})
    assert code == 1 and out["problems"] == ["coworld is required: the Coworld id to switch to"]
    code, out = apply(capsys, "workspace.coworld_switch", {"coworld": "cow-y", "source": "x"})
    assert code == 1 and "unknown arguments: source" in out["problems"]


def test_a_contradicting_store_is_refused_before_it_is_opened(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    code, out = apply(capsys, "directives.retire", {"directive_id": "W-1"}, "--store", "coworld")
    assert code == 1
    assert "W-1 is in the workspace store" in out["error"]


def test_apply_without_an_active_coworld_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    code, out = apply(capsys, "goals.update", {"goal_id": 1, "title": "x"})
    assert code == 1
    assert "no session Coworld" in out["problems"][0]
    # Workspace-scoped ops still work without one.
    code, out = apply(capsys, "workspace.pause", {"cites": "ui:action:1"})
    assert code == 0, out


# --- export ---------------------------------------------------------------------------------


def test_export_writes_every_table_of_both_stores(workspace, capsys, tmp_path):
    make_goal(workspace.store)
    out_dir = tmp_path / "dump"
    code, out = run(capsys, "export", "--out", str(out_dir))
    assert code == 0, out
    for store, folder in ((workspace.wstore, "workspace"), (workspace.store, COWORLD_ID)):
        tables = _copyable_tables(store.conn, "main")
        assert {"events", "change_log", "handled_events", "meta", "settings"} <= set(tables)
        written = sorted(p.stem for p in (out_dir / folder).glob("*.jsonl"))
        assert written == sorted(tables)
        summary = out["stores"][folder]
        assert summary["seq"] == store.last_seq()
        assert summary["tables"] == {
            table: store.one(f"SELECT COUNT(*) FROM {table}")[0] for table in tables
        }
    goal_rows = (out_dir / COWORLD_ID / "goals.jsonl").read_text().splitlines()
    assert json.loads(goal_rows[0])["title"] == "Beat the starter"
    event_rows = (out_dir / "workspace" / "events.jsonl").read_text().splitlines()
    assert [json.loads(line)["kind"] for line in event_rows] == [
        "coworld.registered",
        "coworld.switched",
    ]
    assert out["out"] == str(out_dir)


def test_export_a_named_coworld_must_have_a_store(workspace, capsys, tmp_path):
    code, out = run(capsys, "export", "--coworld", "cow-nope", "--out", str(tmp_path / "d"))
    assert code == 1
    assert out["error"] == "Coworld cow-nope has no store in this workspace"
    assert not (workspace.ws.root / "cow_nope").exists()


def test_export_without_a_coworld_dumps_the_workspace_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    code, out = run(capsys, "export", "--out", str(tmp_path / "dump"))
    assert code == 0, out
    assert list(out["stores"]) == ["workspace"]


def test_export_tables_and_seq_come_from_one_read_transaction(
    workspace, capsys, tmp_path, monkeypatch
):
    """A writer committing between the events and goals tables moves neither in the export."""
    make_goal(workspace.store)
    seq_before = workspace.store.last_seq()
    real_tables = _copyable_tables
    written: list[int] = []

    def tables_with_a_writer_after_events(conn, schema):
        for table in real_tables(conn, schema):
            yield table
            if table == "events" and "goals" in real_tables(conn, schema) and not written:
                # The fixture's connection is a second connection to the Coworld store.
                written.append(goals.update(workspace.store, HARNESS, 1, title="changed")["id"])

    monkeypatch.setattr(store_cli, "_copyable_tables", tables_with_a_writer_after_events)
    out_dir = tmp_path / "dump"
    code, out = run(capsys, "export", "--out", str(out_dir))
    assert code == 0, out
    assert written and goals.read(workspace.store, 1)["title"] == "changed"
    folder = out_dir / COWORLD_ID
    goal_rows = [json.loads(line) for line in (folder / "goals.jsonl").read_text().splitlines()]
    event_rows = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    assert goal_rows[0]["title"] == "Beat the starter"
    assert "goal.updated" not in [row["kind"] for row in event_rows]
    assert out["stores"][COWORLD_ID]["seq"] == seq_before
    assert workspace.store.last_seq() == seq_before + 1


# --- backup and restore-check ---------------------------------------------------------------


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def test_backup_copies_every_database_and_writes_a_manifest(
    workspace, capsys, tmp_path, monkeypatch
):
    make_goal(workspace.store)
    profile = tmp_path / "profile"
    profile.mkdir()
    state = sqlite3.connect(profile / "state.db")
    state.execute("CREATE TABLE sessions (id TEXT)")
    state.execute("INSERT INTO sessions VALUES ('s1')")
    state.commit()
    state.close()
    monkeypatch.setenv("HERMES_HOME", str(profile))

    code, out = run(capsys, "backup", str(tmp_path / "backups"))
    assert code == 0, out
    folder = Path(out["dir"])
    assert folder.parent == tmp_path / "backups" and folder.name.endswith("Z")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest == {key: value for key, value in out.items() if key != "ok"}
    assert manifest["workspace"] == str(workspace.ws.root)
    assert manifest["last_seq"] == workspace.wstore.last_seq()
    databases = {entry["name"]: entry for entry in manifest["databases"]}
    assert list(databases) == ["workspace.db", f"coworld-{COWORLD_ID}.db", "hermes-state.db"]
    assert databases["workspace.db"]["source"] == str(workspace.ws.store_path)
    assert databases[f"coworld-{COWORLD_ID}.db"]["last_seq"] == workspace.store.last_seq()
    assert databases["hermes-state.db"]["source"] == str(profile / "state.db")
    assert "last_seq" not in databases["hermes-state.db"]
    for name, entry in databases.items():
        copy = folder / name
        assert copy.stat().st_size == entry["bytes"] and sha256_of(copy) == entry["sha256"]
        assert not copy.with_name(name + "-wal").exists()
        conn = read_only(copy)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        finally:
            conn.close()
    conn = read_only(folder / f"coworld-{COWORLD_ID}.db")
    try:
        assert conn.execute("SELECT title FROM goals").fetchone()[0] == "Beat the starter"
    finally:
        conn.close()
    assert sorted(p.name for p in folder.iterdir()) == sorted([*databases, "manifest.json"])

    code, out = run(capsys, "restore-check", str(folder))
    assert code == 0, out
    assert out["ok"] is True and out["last_seq"] == manifest["last_seq"]
    verdicts = {entry["name"]: entry for entry in out["databases"]}
    assert all(entry["ok"] and entry["integrity"] == "ok" for entry in verdicts.values())
    assert verdicts["workspace.db"]["schema"] == "current"
    assert verdicts[f"coworld-{COWORLD_ID}.db"]["schema"] == "current"
    assert verdicts[f"coworld-{COWORLD_ID}.db"]["sha256_matches"] is True
    assert "schema" not in verdicts["hermes-state.db"]


def test_backup_without_a_store_or_a_coworld_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path / "empty"))
    code, out = run(capsys, "backup", str(tmp_path / "backups"))
    assert code == 1 and "no store yet" in out["error"]
    Store.open(layout.from_env().store_path, kind="workspace").close()
    code, out = run(capsys, "backup", str(tmp_path / "backups"))
    assert code == 0, out
    assert [entry["name"] for entry in out["databases"]] == ["workspace.db"]


def test_a_backup_taken_while_a_writer_commits_is_one_committed_state(workspace, tmp_path):
    """The backup API restarts when another connection writes; the copy never mixes states."""
    make_goal(workspace.store)
    writes: list[str] = []

    def write_once(status: int, remaining: int, total: int) -> None:
        if not writes:
            writes.append(goals.update(workspace.store, HARNESS, 1, title="during copy")["title"])

    entry = store_cli.copy_database(
        workspace.store.path,
        tmp_path / "copy.db",
        name="copy.db",
        kind="coworld",
        pages=1,
        progress=write_once,
    )
    assert writes == ["during copy"]
    conn = read_only(tmp_path / "copy.db")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        title = conn.execute("SELECT title FROM goals").fetchone()[0]
        kinds = [row[0] for row in conn.execute("SELECT kind FROM events ORDER BY seq")]
        seq = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    finally:
        conn.close()
    # Either the state before the write or the state after it, never a mixture.
    assert (title == "during copy") == ("goal.updated" in kinds)
    assert entry["last_seq"] == seq


def test_restore_check_fails_on_a_truncated_copy(workspace, capsys, tmp_path):
    make_goal(workspace.store)
    manifest = store_cli.backup_workspace(workspace.ws, tmp_path / "backups", {})
    folder = Path(manifest["dir"])
    copy = folder / f"coworld-{COWORLD_ID}.db"
    data = copy.read_bytes()
    copy.write_bytes(data[: len(data) // 2])
    code, out = run(capsys, "restore-check", str(folder))
    assert code == 1
    assert out["ok"] is False
    verdicts = {entry["name"]: entry for entry in out["databases"]}
    assert verdicts["workspace.db"]["ok"] is True
    bad = verdicts[f"coworld-{COWORLD_ID}.db"]
    assert bad["ok"] is False and bad["sha256_matches"] is False and bad["integrity"] != "ok"
    copy.unlink()
    code, out = run(capsys, "restore-check", str(folder))
    assert code == 1 and out["databases"][1]["integrity"] == "missing"
    code, out = run(capsys, "restore-check", str(tmp_path / "nowhere"))
    assert code == 1 and "restore-check failed" in out["error"]


# --- quiesce --------------------------------------------------------------------------------

# Exits 3 when every database given is write-locked, 7 when any is free.
LOCK_PROBE = """
import sqlite3, sys
for path in sys.argv[1:]:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 0")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        continue
    sys.exit(7)
sys.exit(3)
"""


def run_lines(capsys, *argv: str) -> tuple[int, list[dict]]:
    """Run ``research store ...``; the exit code and every stdout line parsed as JSON."""
    code = cli.main(["store", *argv])
    out = capsys.readouterr().out.strip().splitlines()
    return code, [json.loads(line) for line in out]


def start_quiesce(*argv: str) -> subprocess.Popen:
    """``research store quiesce`` in its own process, its stdout a pipe of JSON lines."""
    command = [sys.executable, "-m", "research_core.cli", "store", "quiesce", *argv]
    return subprocess.Popen(command, stdout=subprocess.PIPE, text=True)


def assert_write_lock_free(path: Path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout = 0")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def assert_intact(store: Store) -> None:
    assert store.one("PRAGMA integrity_check")[0] == "ok"
    assert store.unmarked_changes() == []


def test_quiesce_holds_a_writer_until_release_and_empties_the_wal(workspace):
    make_goal(workspace.store)
    wal = workspace.store.path.with_name("store.db-wal")
    assert wal.stat().st_size > 0  # the open connection leaves the goal in the WAL
    marker = workspace.ws.state / "quiesce.json"
    proc = start_quiesce("--max-hold", "2")
    try:
        held = json.loads(proc.stdout.readline())
        assert held["quiesced"] == [str(workspace.store.path), str(workspace.ws.store_path)]
        for report in held["checkpoints"].values():
            assert report["busy"] is False and report["wal_bytes"] == 0
        assert held["checkpoints"][str(workspace.store.path)]["wal_bytes_before"] > 0
        assert wal.stat().st_size == 0
        written = json.loads(marker.read_text())
        assert written["pid"] == proc.pid and written["max_hold_s"] == 2
        assert written["held_at"] == held["held_at"]
        # Readers continue; a store opened during the hold waits for the release to write.
        assert goals.read(workspace.store, 1)["title"] == "Beat the starter"
        late = Store.open(workspace.store.path, kind="coworld")
        try:
            assert late.one("PRAGMA busy_timeout")[0] == 2 * 1000 + 5000
            started = time.monotonic()
            goals.update(late, HARNESS, 1, title="written during the hold")
            write_took = time.monotonic() - started
        finally:
            late.close()
        released = json.loads(proc.stdout.readline())
        assert proc.wait() == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert released["ok"] is True and released["then_exit"] is None
    assert released["held_s"] >= 2 and write_took >= 1
    assert released["released_at"] > held["held_at"]
    assert not marker.exists()
    assert goals.read(workspace.store, 1)["title"] == "written during the hold"
    assert [e.kind for e in workspace.store.events_after(0)][-1] == "goal.updated"
    assert_intact(workspace.store)
    assert_intact(workspace.wstore)
    assert_write_lock_free(workspace.store.path)


def test_a_writer_inside_a_coworld_transaction_can_still_reach_the_workspace_store(workspace):
    """A transition nests the workspace store's transaction in its Coworld store's. A quiesce that
    took the workspace store first would hold what this writer needs while waiting for what it has,
    and this connection's five seconds would end in ``database is locked``."""
    marker = workspace.ws.state / "quiesce.json"
    proc = start_quiesce("--max-hold", "0.2")
    try:
        with workspace.store.tx(HARNESS):
            while not marker.exists():
                assert proc.poll() is None
                time.sleep(0.01)
            time.sleep(0.5)  # long enough for a quiesce to have locked the store it takes first
            with workspace.wstore.tx(HARNESS) as tx:
                tx.event("test.nested", "writer", {})
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
    assert workspace.wstore.q("SELECT 1 FROM events WHERE kind='test.nested'")


def test_every_holder_of_several_locks_takes_coworld_stores_before_the_workspace_store(
    workspace, tmp_path
):
    hermes_home = tmp_path / "profiles" / "orchestrator"
    hermes_home.mkdir(parents=True)
    state = hermes_home / store_cli.HERMES_STATE_DB
    sqlite3.connect(state).close()
    env = {"HERMES_HOME": str(hermes_home)}
    expected = [workspace.store.path, workspace.ws.store_path, state]
    assert store_cli.lock_order(workspace.ws, env) == expected
    assert sleep.sleep_databases(workspace.ws, env, ()) == expected


def refill_the_wal_before_each_lock(monkeypatch) -> None:
    """A writer commits between every checkpoint and the lock taken after it."""
    connect = sqlite3.connect

    class Refilled(sqlite3.Connection):
        def __init__(self, database, **kwargs):
            super().__init__(database, **kwargs)
            self.database = database

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                writer = connect(self.database, isolation_level=None)
                writer.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('refill', hex(randomblob(8)))"
                )
                writer.close()
            return super().execute(sql, *args)

    monkeypatch.setattr(
        store_cli.sqlite3,
        "connect",
        lambda path, **kwargs: connect(path, factory=Refilled, **kwargs),
    )


def test_quiesce_holds_every_lock_when_writers_outrun_its_checkpoints(
    workspace, capsys, monkeypatch
):
    """Readiness means the locks are held; what the checkpoints left is in the fsynced WAL."""
    paths = [workspace.store.path, workspace.ws.store_path]
    refill_the_wal_before_each_lock(monkeypatch)
    probe = [sys.executable, "-c", LOCK_PROBE, *map(str, paths)]
    code, (held, released) = run_lines(capsys, "quiesce", "--max-hold", "10", "--then", *probe)
    assert code == 1  # the probe's non-zero exit is the quiesce's
    assert released["then_exit"] == 3  # every database was write-locked
    for report in held["checkpoints"].values():
        assert report["busy"] is False and report["wal_bytes"] > 0


def test_a_flush_holds_each_lock_for_its_fsync_and_reports_a_wal_it_could_not_empty(
    workspace, monkeypatch
):
    paths = [workspace.store.path, workspace.ws.store_path]
    refill_the_wal_before_each_lock(monkeypatch)
    locked = []

    def probe_the_locks(databases):
        for path in databases:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                assert_write_lock_free(path)
            locked.append(path)

    monkeypatch.setattr(store_cli, "flush_to_disk", probe_the_locks)
    reports = store_cli.flush_databases(paths)
    assert locked == paths
    for path in paths:
        assert not sleep.is_flushed(reports[str(path)])
        assert_write_lock_free(path)  # released


def test_lock_order_settles_coworlds_registered_at_the_same_moment(workspace):
    for coworld_id in ("cow-z", "cow-a"):
        coworlds.register(
            workspace.wstore, HARNESS, coworld_id, folder=coworld_id.replace("-", "_")
        )
        Store.open(workspace.ws.coworld(coworld_id).store_path, kind="coworld").close()
    with workspace.wstore.tx(HARNESS) as tx:
        tx.execute("UPDATE coworlds SET created = '2026-01-01T00:00:00+00:00'")
    assert store_cli.lock_order(workspace.ws, {}) == [
        workspace.ws.coworld(coworld_id).store_path for coworld_id in ("cow-a", "cow-x", "cow-z")
    ] + [workspace.ws.store_path]


def test_a_restored_copy_not_yet_in_wal_mode_waits_out_the_hold_to_switch(workspace):
    workspace.store.close()
    path = workspace.ws.coworld(COWORLD_ID).store_path
    restored = sqlite3.connect(path)
    assert restored.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    restored.close()
    proc = start_quiesce("--max-hold", "6.5")
    try:
        json.loads(proc.stdout.readline())
        started = time.monotonic()
        late = Store.open(path, kind="coworld")
        took = time.monotonic() - started
        assert proc.wait() == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    try:
        assert took > migrate.BUSY_TIMEOUT_MS / 1000
        assert late.one("PRAGMA journal_mode")[0] == "wal"
        assert late.one("PRAGMA busy_timeout")[0] == 6500 + migrate.BUSY_TIMEOUT_MS
    finally:
        late.close()


def test_quiesce_then_runs_with_every_lock_held_and_reports_its_exit_code(
    workspace, capsys, tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    state = sqlite3.connect(profile / "state.db")
    state.execute("CREATE TABLE sessions (id TEXT)")
    state.commit()
    state.close()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    paths = [workspace.store.path, workspace.ws.store_path, profile / "state.db"]

    probe = [sys.executable, "-c", LOCK_PROBE, *map(str, paths)]
    code, (held, released) = run_lines(capsys, "quiesce", "--max-hold", "10", "--then", *probe)
    assert code == 1  # the command's non-zero exit is the quiesce's
    assert held["quiesced"] == [str(path) for path in paths]
    hermes_report = held["checkpoints"][str(profile / "state.db")]  # rollback journal, no WAL
    assert hermes_report == {"busy": False, "wal_bytes_before": 0, "wal_bytes": 0}
    assert released["then_exit"] == 3 and released["ok"] is False
    assert "then_killed" not in released and released["held_s"] < 10
    assert not (workspace.ws.state / "quiesce.json").exists()
    for path in paths:
        assert_write_lock_free(path)

    code, (held, released) = run_lines(
        capsys, "quiesce", "--then", sys.executable, "-c", "raise SystemExit(0)"
    )
    assert code == 0 and released["ok"] is True and released["then_exit"] == 0
    assert_intact(workspace.store)


def test_quiesce_kills_a_then_command_that_runs_past_max_hold(workspace, capsys):
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    started = time.monotonic()
    code, (held, released) = run_lines(capsys, "quiesce", "--max-hold", "0.5", "--then", *sleeper)
    assert time.monotonic() - started < 5
    assert code == 1 and released["ok"] is False
    assert released["then_exit"] == -signal.SIGKILL
    assert released["then_killed"] == "ran past --max-hold"
    assert 0.5 <= released["held_s"] < 5
    assert not (workspace.ws.state / "quiesce.json").exists()
    assert_write_lock_free(workspace.ws.store_path)
    assert_write_lock_free(workspace.store.path)


def test_quiesce_releases_the_locks_on_sigterm(workspace):
    proc = start_quiesce("--max-hold", "30")
    try:
        held = json.loads(proc.stdout.readline())
        assert len(held["quiesced"]) == 2
        proc.send_signal(signal.SIGTERM)
        released = json.loads(proc.stdout.readline())
        assert proc.wait(timeout=10) == 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert released["interrupted"] is True and released["ok"] is False
    assert released["held_s"] < 30
    assert not (workspace.ws.state / "quiesce.json").exists()
    assert_write_lock_free(workspace.ws.store_path)
    assert_write_lock_free(workspace.store.path)


def test_quiesce_refusals(workspace, capsys):
    code, out = run(capsys, "quiesce", "--max-hold", "0")
    assert code == 1 and "--max-hold" in out["error"]
    code, out = run(capsys, "quiesce", "--then")
    assert code == 1 and "--then needs a command" in out["error"]
    assert not (workspace.ws.state / "quiesce.json").exists()


def test_a_fresh_quiesce_marker_raises_the_busy_timeout_of_stores_opened_during_the_hold(
    workspace,
):
    marker = workspace.ws.state / "quiesce.json"
    marker.write_text(json.dumps({"held_at": now_iso(), "max_hold_s": 30, "pid": 1}))
    stores = ((workspace.ws.store_path, "workspace"), (workspace.store.path, "coworld"))
    for path, kind in stores:
        with Store.open(path, kind=kind) as late:
            assert late.one("PRAGMA busy_timeout")[0] == 30 * 1000 + 5000
            assert late.one("PRAGMA synchronous")[0] == 2  # the rest of the setup is unchanged
    assert quiesce_busy_timeout_ms(workspace.store.path) == 35000
    marker.unlink()
    assert quiesce_busy_timeout_ms(workspace.store.path) is None
    with Store.open(workspace.store.path, kind="coworld") as late:
        assert late.one("PRAGMA busy_timeout")[0] == 5000


def test_a_stale_or_unreadable_quiesce_marker_is_ignored(workspace):
    marker = workspace.ws.state / "quiesce.json"
    stale_at = now_iso(lambda: datetime.now(UTC) - timedelta(seconds=31))
    marker.write_text(json.dumps({"held_at": stale_at, "max_hold_s": 30, "pid": 1}))
    assert quiesce_busy_timeout_ms(workspace.store.path) is None
    assert quiesce_busy_timeout_ms(workspace.ws.store_path) is None
    marker.write_text("not json")
    assert quiesce_busy_timeout_ms(workspace.store.path) is None
    marker.write_text(json.dumps({"held_at": "yesterday", "max_hold_s": 30}))
    assert quiesce_busy_timeout_ms(workspace.store.path) is None
    marker.write_text(json.dumps({"max_hold_s": 30}))
    assert quiesce_busy_timeout_ms(workspace.store.path) is None
    with Store.open(workspace.store.path, kind="coworld") as late:
        assert late.one("PRAGMA busy_timeout")[0] == 5000


# --- snapshot -------------------------------------------------------------------------------


def test_snapshot_shows_each_directive_with_the_id_of_its_store(workspace, capsys):
    make_goal(workspace.store)
    directives.add(workspace.wstore, HARNESS, text="No Fridays", scope="workspace", source="m1")
    directives.add(workspace.store, HARNESS, text="Small diffs", scope="goal:1", source="m2")

    code, coworld = run(capsys, "snapshot")
    assert code == 0, coworld
    assert [d["id"] for d in coworld["records"]["directives"]] == ["D-1"]

    code, workspace_snapshot = run(capsys, "snapshot", "--store", "workspace")
    assert code == 0, workspace_snapshot
    assert [d["id"] for d in workspace_snapshot["records"]["directives"]] == ["W-1"]


def test_snapshot_has_records_settings_and_seq(workspace, capsys):
    make_goal(workspace.store)
    settings.update(workspace.store, HARNESS, "submissions.mode", "ask", cites="m")

    code, out = run(capsys, "snapshot")
    assert code == 0, out
    assert out["store"] == "coworld" and out["coworld_id"] == COWORLD_ID
    assert out["seq"] == workspace.store.last_seq()
    records = out["records"]
    assert list(records) == [
        "goals",
        "tasks",
        "versions",
        "claims",
        "experiments",
        "directives",
        "champion",
        "capabilities",
        "intents",
        "settings",
    ]
    assert records["goals"][0]["id"] == "G-1" and records["goals"][0]["tags"] == []
    assert records["settings"]["submissions.mode"] == "ask"
    assert records["settings"]["submissions.quality_bar"] is True
    assert "paused" not in records["settings"]
    assert set(out) == {"store", "coworld_id", "seq", "records"}

    code, out = run(capsys, "snapshot", "--store", "workspace")
    assert code == 0, out
    assert out["store"] == "workspace" and out["seq"] == workspace.wstore.last_seq()
    assert list(out["records"]) == [
        "directives",
        "notices",
        "sessions",
        "coworlds",
        "settings",
        "admissions",
    ]
    assert out["records"]["coworlds"][0]["coworld_id"] == COWORLD_ID
    assert out["records"]["settings"]["paused"] is False


def test_snapshot_rows_and_seq_come_from_one_read_transaction(workspace, capsys, monkeypatch):
    """A writer committing after the goals were read moves neither the rows nor the cursor."""
    make_goal(workspace.store)
    seq_before = workspace.store.last_seq()
    real_all = settings.all
    written: list[str] = []

    def settings_then_a_writer(store_or_pair):
        result = real_all(store_or_pair)
        if not written:
            # The fixture's connection is a second connection to the Coworld store.
            written.append(goals.update(workspace.store, HARNESS, 1, title="changed")["title"])
        return result

    monkeypatch.setattr(settings, "all", settings_then_a_writer)
    code, out = run(capsys, "snapshot")
    assert code == 0, out
    assert written == ["changed"]
    assert out["records"]["goals"][0]["title"] == "Beat the starter"
    assert out["seq"] == seq_before
    # The committed change is exactly what the client reads after its cursor.
    [event] = workspace.store.events_after(out["seq"])
    assert event.kind == "goal.updated" and event.subject == "G-1"


# --- events ---------------------------------------------------------------------------------


def events_lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]


def test_events_after_returns_rows_greater_than_seq(workspace, capsys):
    goal = make_goal(workspace.store)
    goals.set_status(workspace.store, HARNESS, 1, "parked", "r")
    goals.set_status(workspace.store, HARNESS, 1, "active", "r")
    assert cli.main(["store", "events", "--after", "1"]) == 0
    lines = events_lines(capsys)
    assert [line["seq"] for line in lines] == [2, 3]
    assert lines[0] == {
        "store": "coworld",
        "seq": 2,
        "at": lines[0]["at"],
        "actor": "harness",
        "kind": "goal.status",
        "subject": goal["id"],
        "payload": {"from": "active", "to": "parked", "reason": "r"},
    }
    assert cli.main(["store", "events", "--after", "0", "--limit", "1"]) == 0
    assert [line["seq"] for line in events_lines(capsys)] == [1]
    assert cli.main(["store", "events", "--after", "0", "--store", "workspace"]) == 0
    assert [line["kind"] for line in events_lines(capsys)] == [
        "coworld.registered",
        "coworld.switched",
    ]
    assert cli.main(["store", "events", "--after", "3"]) == 0
    assert events_lines(capsys) == []


def test_events_follow_streams_a_row_written_by_another_thread(workspace, capsys):
    make_goal(workspace.store)
    path = workspace.store.path

    def write_later() -> None:
        store = Store.open(path, kind="coworld")
        try:
            goals.set_status(store, HARNESS, 1, "parked", "later")
        finally:
            store.close()

    writer = threading.Timer(0.15, write_later)
    writer.start()
    try:
        code = cli.main(
            ["store", "events", "--after", "1", "--follow", "--interval", "0.02", "--max-rows", "1"]
        )
    finally:
        writer.join()
    assert code == 0
    lines = events_lines(capsys)
    assert len(lines) == 1
    assert lines[0]["kind"] == "goal.status" and lines[0]["payload"]["reason"] == "later"


def test_events_without_an_active_coworld_names_the_fix(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    code, out = run(capsys, "events", "--after", "0")
    assert code == 1
    assert "no session Coworld" in out["error"] and "--coworld" in out["error"]


def test_pending_user_edit_is_not_reported_as_applied(workspace, capsys, monkeypatch):
    from types import SimpleNamespace

    from research_core import transitions

    record = tasks.create(
        workspace.store, HARNESS, type="maintenance", title="Before", description="Fix"
    )
    monkeypatch.setattr(transitions, "REQUEST_WAIT_SECONDS", 0)
    args = {"task_id": record["id"], "title": "After", "operation_id": "user-edit"}
    code, result = run(capsys, "apply", "--actor", USER, "tasks.update", json.dumps(args))
    assert code == 2 and result["status"] == "pending"
    assert "ok" not in result and "record" not in result
    assert tasks.read(workspace.store, record["id"])["title"] == "Before"
    tick = SimpleNamespace(
        store=workspace.store,
        wstore=workspace.wstore,
        ws=workspace.ws,
        env={},
        coworld=workspace.ws.coworld(COWORLD_ID),
    )
    transitions.apply(tick, "coordinator:replacement")
    code, result = run(capsys, "apply", "--actor", USER, "tasks.update", json.dumps(args))
    assert code == 0 and result["ok"] and result["record"]["title"] == "After"
    assert workspace.store.one("SELECT count(*) FROM transition_commands")[0] == 1
