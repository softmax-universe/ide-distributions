"""Store.open, token-marked transactions, events, detection, the schema check and repair."""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from research_core.store import db, migrate
from research_core.store.actors import Actor
from research_core.store.errors import SchemaMismatch, SchemaNewer
from research_core.store.events import Event

GOAL_COLUMNS = "title, end_state, reason, status, created, created_by, updated"


def insert_goal(tx, title: str = "Beat the champion") -> int:
    cur = tx.execute(
        f"INSERT INTO goals({GOAL_COLUMNS}) VALUES (?, ?, ?, 'active', ?, ?, ?)",
        (title, "Win rate above 60%", "the user asked", tx.at, str(tx.actor), tx.at),
    )
    return cur.lastrowid


def raw(path) -> sqlite3.Connection:
    """A plain connection: what an agent's direct ``sqlite3`` edit looks like."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# --- open ---------------------------------------------------------------------------------


def test_open_creates_and_migrates(tmp_path):
    path = tmp_path / "deep" / ".state" / "store.db"
    store = db.Store.open(path, kind="coworld")
    try:
        assert path.is_file()
        assert store.one("PRAGMA user_version")[0] == 4
        assert store.one("PRAGMA journal_mode")[0] == "wal"
        assert store.one("PRAGMA foreign_keys")[0] == 1
        assert store.one("PRAGMA busy_timeout")[0] == 5000
        assert store.get_meta("schema_kind") == "coworld"
        assert store.get_meta("created")
        readme = path.parent / "README.md"
        assert readme.read_text() == db.STATE_README
        assert "store.db" in db.STATE_README and "Do not edit files here" in db.STATE_README
    finally:
        store.close()


def test_writable_connections_use_wal_and_synchronous_full(tmp_path):
    """Every writable connection fsyncs each commit (FULL) and journals through the WAL."""
    path = tmp_path / "store.db"
    with db.Store.open(path, kind="coworld") as store:
        assert store.one("PRAGMA journal_mode")[0] == "wal"
        assert store.one("PRAGMA synchronous")[0] == 2  # FULL
    conn = migrate.connect_writable(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == migrate.BUSY_TIMEOUT_MS
    finally:
        conn.close()


def _mid_conversion(path):
    """A connection in the state a first opener is in: rollback journal, write lock held."""
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("CREATE TABLE placeholder(x)")
    other.execute("BEGIN IMMEDIATE")
    return other


def test_a_first_open_waits_for_another_connection_converting_the_file(tmp_path):
    """SQLite fails this lock upgrade at once whatever the busy timeout, so a first open that
    meets another one (a new Coworld's store: the coordinator and a tool) must wait itself."""
    path = tmp_path / "store.db"
    other = _mid_conversion(path)
    release = threading.Timer(0.3, other.execute, ["ROLLBACK"])
    release.start()
    started = time.monotonic()
    conn = migrate.connect_writable(path)
    waited = time.monotonic() - started
    release.join()
    other.close()
    try:
        assert waited >= 0.25
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_a_first_open_still_fails_when_the_lock_outlasts_the_busy_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "BUSY_TIMEOUT_MS", 50)
    path = tmp_path / "store.db"
    other = _mid_conversion(path)
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            migrate.connect_writable(path)
    finally:
        other.close()
    assert time.monotonic() - started < 2  # one wait: no attempt gets a fresh busy timeout


def test_a_first_open_keeps_the_full_busy_timeout_for_later_locks(tmp_path):
    conn = migrate.connect_writable(tmp_path / "store.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == migrate.BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_simultaneous_first_opens_all_succeed(tmp_path):
    """Two bare opens collide often enough here to fail on most runs without the wait."""
    rounds = 300
    start = threading.Barrier(2)
    failures = []

    def open_every_round():
        for round_number in range(rounds):
            start.wait()
            try:
                migrate.connect_writable(tmp_path / f"{round_number}.db").close()
            except sqlite3.OperationalError as error:
                failures.append(str(error))

    threads = [threading.Thread(target=open_every_round) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []


def test_open_twice_is_idempotent(tmp_path):
    path = tmp_path / "store.db"
    db.Store.open(path, kind="coworld").close()
    (path.parent / "README.md").write_text("custom note")
    store = db.Store.open(path, kind="coworld")
    try:
        assert store.one("PRAGMA user_version")[0] == 4
        assert store.schema_status().ok
        assert (path.parent / "README.md").read_text() == "custom note"
    finally:
        store.close()


def test_open_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        db.Store.open(tmp_path / "store.db", kind="other")


def test_open_refuses_a_store_of_the_other_kind(tmp_path):
    path = tmp_path / "store.db"
    db.Store.open(path, kind="workspace").close()
    with pytest.raises(SchemaMismatch) as excinfo:
        db.Store.open(path, kind="coworld")
    assert any("schema_kind" in problem for problem in excinfo.value.problems)
    assert str(tmp_path) not in str(excinfo.value)


def test_store_is_a_context_manager(tmp_path):
    with db.Store.open(tmp_path / "store.db", kind="workspace") as store:
        assert store.kind == "workspace"
    with pytest.raises(sqlite3.ProgrammingError):
        store.q("SELECT 1")


# --- transactions, tokens and events ------------------------------------------------------


def test_tx_writes_event_and_stamps_change_log(coworld_store, harness):
    with coworld_store.tx(harness) as tx:
        goal_id = insert_goal(tx)
        seq = tx.event("goal.created", f"G-{goal_id}", {"title": "Beat the champion"})
        assert tx.token and len(tx.token) == 16
    assert seq == 1
    log = coworld_store.q("SELECT tbl, op, token FROM change_log ORDER BY id")
    assert [(r["tbl"], r["op"]) for r in log] == [("goals", "insert"), ("events", "insert")]
    assert {r["token"] for r in log} == {tx.token}
    events = coworld_store.events_after(0)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, Event)
    assert (event.seq, event.actor, event.kind, event.subject) == (
        1,
        "harness",
        "goal.created",
        "G-1",
    )
    assert event.payload == {"title": "Beat the champion"}
    assert event.at == tx.at == "2026-10-28T21:04:05.123456Z"
    assert coworld_store.unmarked_changes() == []


def test_tx_rolls_back_on_error(coworld_store, harness):
    with pytest.raises(RuntimeError, match="boom"):
        with coworld_store.tx(harness) as tx:
            insert_goal(tx)
            tx.event("goal.created", "G-1", {})
            raise RuntimeError("boom")
    assert coworld_store.one("SELECT COUNT(*) FROM goals")[0] == 0
    assert coworld_store.last_seq() == 0
    assert coworld_store.q("SELECT * FROM change_log") == []
    # The store is usable afterwards.
    with coworld_store.tx(harness) as tx:
        insert_goal(tx)
    assert coworld_store.one("SELECT COUNT(*) FROM goals")[0] == 1


def test_tx_cannot_nest_and_event_needs_an_open_tx(coworld_store, harness):
    with coworld_store.tx(harness) as tx:
        with pytest.raises(RuntimeError):
            with coworld_store.tx(harness):
                pass
    with pytest.raises(RuntimeError):
        tx.event("goal.created", "G-1", {})
    with pytest.raises(RuntimeError):
        tx.execute("SELECT 1")


def test_tx_records_the_actor_string(coworld_store, session_actor):
    user = Actor.parse("user:u_42")
    with coworld_store.tx(session_actor) as tx:
        tx.event("session.started", session_actor.id, {})
    with coworld_store.tx(user) as tx:
        tx.event("task.status", "T-1", {"status": "unclaimed"})
    actors = [e.actor for e in coworld_store.events_after(0)]
    assert actors == [f"session:{session_actor.id}", "user:u_42"]


def test_tx_takes_the_write_lock_at_begin(coworld_store, harness):
    """BEGIN IMMEDIATE: a second writer waits even before the first writes anything."""
    other = sqlite3.connect(coworld_store.path, isolation_level=None, timeout=0)
    try:
        with coworld_store.tx(harness):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
    finally:
        other.close()


def test_concurrent_writers_serialise(tmp_path):
    path = tmp_path / "store.db"
    db.Store.open(path, kind="coworld").close()
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            with db.Store.open(path, kind="coworld") as store:
                for _ in range(5):
                    with store.tx(Actor.harness()) as tx:
                        insert_goal(tx, f"goal {n}")
                        tx.event("goal.created", "G-?", {"n": n})
        except BaseException as exc:  # noqa: BLE001 - surfaced through the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with db.Store.open(path, kind="coworld") as store:
        assert store.one("SELECT COUNT(*) FROM goals")[0] == 20
        assert store.last_seq() == 20
        assert store.unmarked_changes() == []


def test_events_after_and_last_seq(coworld_store, harness):
    assert coworld_store.last_seq() == 0
    assert coworld_store.events_after(0) == []
    with coworld_store.tx(harness) as tx:
        for n in range(5):
            tx.event("notice", f"N-{n}", {"n": n})
    assert coworld_store.last_seq() == 5
    assert [e.seq for e in coworld_store.events_after(2)] == [3, 4, 5]
    assert [e.seq for e in coworld_store.events_after(0, limit=2)] == [1, 2]
    assert coworld_store.events_after(5) == []


def test_two_stores_independent_seq(coworld_store, workspace_store, harness):
    with coworld_store.tx(harness) as tx:
        assert tx.event("goal.created", "G-1", {}) == 1
    with workspace_store.tx(harness) as tx:
        assert tx.event("notice", "N-1", {}) == 1
    assert coworld_store.last_seq() == 1
    assert workspace_store.last_seq() == 1


def test_event_payload_must_be_a_dict(coworld_store, harness):
    with pytest.raises(TypeError):
        with coworld_store.tx(harness) as tx:
            tx.event("notice", "N-1", ["not", "a", "dict"])


def test_meta_helpers_do_not_touch_change_log(coworld_store):
    coworld_store.set_meta("heartbeat", "2026-10-28T21:05:00.000000Z")
    assert coworld_store.get_meta("heartbeat") == "2026-10-28T21:05:00.000000Z"
    assert coworld_store.get_meta("missing") is None
    assert coworld_store.q("SELECT * FROM change_log") == []


# --- detection ----------------------------------------------------------------------------


def test_unmarked_write_detected(coworld_store, harness):
    other = raw(coworld_store.path)
    try:
        other.execute(
            f"INSERT INTO goals({GOAL_COLUMNS}) VALUES ('x', 'y', 'z', 'active', 't', 'me', 't')"
        )
    finally:
        other.close()
    unmarked = coworld_store.unmarked_changes()
    assert len(unmarked) == 1
    assert unmarked[0]["tbl"] == "goals"
    assert unmarked[0]["op"] == "insert"
    assert unmarked[0]["row_id"] == 1
    # A later library transaction stamps only its own rows; the stray one stays visible.
    with coworld_store.tx(harness) as tx:
        insert_goal(tx)
    assert [row["row_id"] for row in coworld_store.unmarked_changes()] == [1]
    coworld_store.acknowledge_unmarked([unmarked[0]["id"]], "reported")
    assert coworld_store.unmarked_changes() == []
    token = coworld_store.one("SELECT token FROM change_log WHERE id = ?", (unmarked[0]["id"],))
    assert token[0] == "ack:reported"


def test_unmarked_update_and_delete_detected(coworld_store, harness):
    with coworld_store.tx(harness) as tx:
        insert_goal(tx)
    other = raw(coworld_store.path)
    try:
        other.execute("UPDATE goals SET title = 'edited' WHERE id = 1")
        other.execute("DELETE FROM goals WHERE id = 1")
    finally:
        other.close()
    ops = [(row["tbl"], row["op"]) for row in coworld_store.unmarked_changes()]
    assert ops == [("goals", "update"), ("goals", "delete")]


def test_unmarked_event_insert_detected(coworld_store):
    other = raw(coworld_store.path)
    try:
        other.execute(
            "INSERT INTO events(at, actor, kind, subject) VALUES ('t', 'me', 'notice', 'N-1')"
        )
    finally:
        other.close()
    assert [row["tbl"] for row in coworld_store.unmarked_changes()] == ["events"]


def test_events_append_only(coworld_store, harness):
    with coworld_store.tx(harness) as tx:
        tx.event("notice", "N-1", {})
    other = raw(coworld_store.path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            other.execute("UPDATE events SET kind = 'x' WHERE seq = 1")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            other.execute("DELETE FROM events WHERE seq = 1")
    finally:
        other.close()
    assert coworld_store.last_seq() == 1


# --- schema check and rebuild -------------------------------------------------------------


def test_schema_status_ok(coworld_store, workspace_store):
    for store in (coworld_store, workspace_store):
        status = store.schema_status()
        assert status.ok
        assert status.diff == []
        assert status.user_version == status.expected_version >= 1


def test_schema_mismatch_refused(tmp_path):
    path = tmp_path / "store.db"
    db.Store.open(path, kind="coworld").close()
    other = raw(path)
    try:
        other.execute("ALTER TABLE goals ADD COLUMN x TEXT")
    finally:
        other.close()
    with pytest.raises(SchemaMismatch) as excinfo:
        db.Store.open(path, kind="coworld")
    problems = excinfo.value.problems
    assert len(problems) == 1
    assert "goals" in problems[0]
    assert str(tmp_path) not in str(excinfo.value)
    assert "goals" in str(excinfo.value)


def test_schema_mismatch_lists_every_difference(tmp_path):
    path = tmp_path / "store.db"
    db.Store.open(path, kind="coworld").close()
    other = raw(path)
    try:
        other.execute("ALTER TABLE goals ADD COLUMN x TEXT")
        other.execute("DROP TRIGGER events_append_only_update")
        other.execute("CREATE TABLE scratch(a)")
        other.execute("CREATE INDEX scratch_a ON scratch(a)")
    finally:
        other.close()
    with pytest.raises(SchemaMismatch) as excinfo:
        db.Store.open(path, kind="coworld")
    text = "\n".join(excinfo.value.problems)
    assert len(excinfo.value.problems) == 4
    assert "table goals" in text
    assert "trigger events_append_only_update" in text
    assert "table scratch" in text
    assert "index scratch_a" in text


def test_rebuild_keeps_rows(tmp_path, harness):
    path = tmp_path / "cow" / ".state" / "store.db"
    with db.Store.open(path, kind="coworld") as store:
        with store.tx(harness) as tx:
            insert_goal(tx, "one")
            tx.event("goal.created", "G-1", {"n": 1})
        with store.tx(harness) as tx:
            insert_goal(tx, "two")
            tx.event("goal.created", "G-2", {"n": 2})
            tx.execute(
                "INSERT INTO claims(summary, body, tags, status, created, created_by, updated)"
                " VALUES ('a claim', 'body text', '[\"G-1\"]', 'active', ?, ?, ?)",
                (tx.at, str(tx.actor), tx.at),
            )
        store.set_meta("heartbeat", "beat")
    other = raw(path)
    try:
        other.execute("ALTER TABLE goals ADD COLUMN x TEXT")
        other.execute("UPDATE goals SET x = 'stray' WHERE id = 2")
        other.execute("INSERT INTO settings(key, value) VALUES ('paused', 'true')")
    finally:
        other.close()
    with pytest.raises(SchemaMismatch):
        db.Store.open(path, kind="coworld")

    report = migrate.rebuild_keeping_rows(path, "coworld")

    assert report.tables["goals"] == 2
    assert report.tables["events"] == 2
    assert report.tables["claims"] == 1
    assert report.tables["settings"] == 1
    assert report.dropped_columns == {"goals": ["x"]}
    assert report.user_version == 4
    as_dict = report.to_dict()
    assert as_dict["tables"]["goals"] == 2 and as_dict["dropped_columns"] == {"goals": ["x"]}
    # The original was copied next to the store before anything changed: old shape, old rows.
    backup = path.parent / as_dict["backup"].rsplit("/", 1)[1]
    assert backup.parent == path.parent and backup.name.startswith("store.before-repair-")
    copy = sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True)
    try:
        assert "x" in [r[1] for r in copy.execute("PRAGMA table_info(goals)")]
        assert copy.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 2
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()
    with db.Store.open(path, kind="coworld") as store:
        assert store.schema_status().ok
        assert store.one("PRAGMA user_version")[0] == 4
        assert store.q("SELECT name FROM sqlite_master WHERE name LIKE 'old\\_%' ESCAPE '\\'") == []
        assert [r["title"] for r in store.q("SELECT title FROM goals ORDER BY id")] == [
            "one",
            "two",
        ]
        assert [e.seq for e in store.events_after(0)] == [1, 2]
        assert store.events_after(0)[1].payload == {"n": 2}
        assert store.last_seq() == 2
        assert store.get_meta("heartbeat") == "beat"
        assert store.get_meta("schema_kind") == "coworld"
        assert store.one("SELECT value FROM settings WHERE key = 'paused'")[0] == "true"
        # The FTS index was rebuilt by the sync triggers.
        hit = store.one("SELECT rowid FROM claims_fts WHERE claims_fts MATCH 'claim'")
        assert hit[0] == 1
        # The stray settings insert stays visible as an unmarked write; the copy itself is marked.
        unmarked = store.unmarked_changes()
        assert [(row["tbl"], row["op"]) for row in unmarked] == [
            ("goals", "update"),
            ("settings", "insert"),
        ]
        tokens = {
            r["token"] for r in store.q("SELECT token FROM change_log WHERE token IS NOT NULL")
        }
        assert any(t.startswith("rebuild:") for t in tokens)
        # New events continue the sequence.
        with store.tx(harness) as tx:
            assert tx.event("notice", "N-1", {}) == 3


def test_rebuild_reports_dropped_tables(tmp_path):
    path = tmp_path / "store.db"
    db.Store.open(path, kind="workspace").close()
    other = raw(path)
    try:
        other.execute("CREATE TABLE extra(a, b)")
        other.execute("INSERT INTO extra VALUES (1, 2)")
    finally:
        other.close()
    report = migrate.rebuild_keeping_rows(path, "workspace")
    assert report.dropped_tables == ["extra"]
    assert "extra" not in report.tables
    with db.Store.open(path, kind="workspace") as store:
        assert store.schema_status().ok


def drifted_store(path, harness, title: str = "original") -> None:
    """A Coworld store with one Goal and an extra column, so it no longer opens."""
    with db.Store.open(path, kind="coworld") as store:
        with store.tx(harness) as tx:
            insert_goal(tx, title)
            tx.event("goal.created", "G-1", {})
    other = raw(path)
    try:
        other.execute("ALTER TABLE goals ADD COLUMN x TEXT")
    finally:
        other.close()


def goal_title(path) -> str:
    conn = raw(path)
    try:
        return conn.execute("SELECT title FROM goals WHERE id = 1").fetchone()[0]
    finally:
        conn.close()


def test_rebuild_keeps_the_file_and_other_connections_see_the_new_shape(tmp_path, harness):
    """The repair happens inside the same file: no new inode, no stale connections."""
    path = tmp_path / "store.db"
    drifted_store(path, harness)
    inode = os.stat(path).st_ino
    other = raw(path)
    try:
        assert "x" in [r[1] for r in other.execute("PRAGMA table_info(goals)")]
        migrate.rebuild_keeping_rows(path, "coworld")
        assert os.stat(path).st_ino == inode
        assert "x" not in [r[1] for r in other.execute("PRAGMA table_info(goals)")]
        assert other.execute("SELECT title FROM goals").fetchone()[0] == "original"
    finally:
        other.close()


def test_a_writer_waiting_during_the_repair_commits_after_it(tmp_path, harness, monkeypatch):
    """The repair holds the write lock; a writer waits on its busy timeout and then lands."""
    path = tmp_path / "store.db"
    with db.Store.open(path, kind="coworld") as store:
        with store.tx(harness) as tx:
            insert_goal(tx, "original")
    opened, go = threading.Event(), threading.Event()
    outcome: dict[str, object] = {}

    def writer() -> None:
        # Opened before the drift, as a tool process that keeps its connection would be.
        with db.Store.open(path, kind="coworld") as store:
            opened.set()
            go.wait()
            try:
                with store.tx(harness) as tx:
                    tx.execute("UPDATE goals SET title = 'acknowledged during rebuild'")
                    tx.event("goal.updated", "G-1", {"title": "acknowledged during rebuild"})
                outcome["committed"] = True
            except sqlite3.OperationalError as error:
                outcome["error"] = str(error)

    thread = threading.Thread(target=writer)
    thread.start()
    opened.wait()
    other = raw(path)
    other.execute("ALTER TABLE goals ADD COLUMN x TEXT")
    other.close()
    real_copy = migrate._copy_rows

    def copy_slowly(*args, **kwargs):
        go.set()
        time.sleep(0.5)  # the writer is now blocked on the repair's lock
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(migrate, "_copy_rows", copy_slowly)
    migrate.rebuild_keeping_rows(path, "coworld")
    thread.join(timeout=10)
    assert outcome == {"committed": True}
    with db.Store.open(path, kind="coworld") as store:
        assert store.schema_status().ok
        assert goal_title(path) == "acknowledged during rebuild"
        assert [e.kind for e in store.events_after(0)] == ["goal.updated"]
        assert store.unmarked_changes() == []
        assert store.one("PRAGMA integrity_check")[0] == "ok"


def test_a_writer_that_cannot_wait_during_the_repair_is_refused_not_lost(
    tmp_path, harness, monkeypatch
):
    path = tmp_path / "store.db"
    drifted_store(path, harness)
    real_copy = migrate._copy_rows
    seen: list[str] = []

    def copy_with_a_hurried_writer(*args, **kwargs):
        other = sqlite3.connect(path, isolation_level=None, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("UPDATE goals SET title = 'slipped in'")
            other.execute("COMMIT")
        except sqlite3.OperationalError as error:
            seen.append(str(error))
        finally:
            other.close()
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(migrate, "_copy_rows", copy_with_a_hurried_writer)
    migrate.rebuild_keeping_rows(path, "coworld")
    assert seen == ["database is locked"]
    assert goal_title(path) == "original"
    with db.Store.open(path, kind="coworld") as store:
        assert store.schema_status().ok


def test_an_interrupted_repair_leaves_the_original_intact(tmp_path, harness, monkeypatch):
    path = tmp_path / "store.db"
    drifted_store(path, harness)

    def copy_that_fails(*args, **kwargs):
        raise RuntimeError("no space left on device")

    monkeypatch.setattr(migrate, "_copy_rows", copy_that_fails)
    with pytest.raises(RuntimeError, match="no space"):
        migrate.rebuild_keeping_rows(path, "coworld")
    other = raw(path)
    try:
        assert "x" in [r[1] for r in other.execute("PRAGMA table_info(goals)")]
        assert other.execute("SELECT title FROM goals").fetchone()[0] == "original"
        assert other.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert other.execute("PRAGMA user_version").fetchone()[0] == 4
        assert other.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        names = [r[0] for r in other.execute("SELECT name FROM sqlite_master")]
        assert not any(name.startswith("old_") for name in names)
        assert "claims_fts" in names and "goals_log_insert" in names
    finally:
        other.close()
    # The rolled-back repair removed its copy: the file says nothing the original does not.
    assert [p.name for p in tmp_path.iterdir() if "before-repair" in p.name] == []
    with pytest.raises(SchemaMismatch):
        db.Store.open(path, kind="coworld")
    monkeypatch.undo()
    migrate.rebuild_keeping_rows(path, "coworld")
    with db.Store.open(path, kind="coworld") as store:
        assert store.schema_status().ok and goal_title(path) == "original"


def newer_store(path, harness) -> None:
    """What a store looks like after a newer release added a migration and used it."""
    with db.Store.open(path, kind="coworld") as store:
        with store.tx(harness) as tx:
            insert_goal(tx, "from the future")
    other = raw(path)
    try:
        other.execute("CREATE TABLE future (id INTEGER PRIMARY KEY, note TEXT)")
        other.execute("INSERT INTO future(note) VALUES ('a newer release wrote this')")
        other.execute("PRAGMA user_version = 99")
    finally:
        other.close()


def assert_untouched_newer_store(path) -> None:
    other = raw(path)
    try:
        assert other.execute("PRAGMA user_version").fetchone()[0] == 99
        assert other.execute("SELECT COUNT(*) FROM future").fetchone()[0] == 1
        assert other.execute("SELECT title FROM goals").fetchone()[0] == "from the future"
    finally:
        other.close()


def test_a_store_from_a_newer_release_is_refused_and_never_rebuilt(tmp_path, harness):
    path = tmp_path / "store.db"
    newer_store(path, harness)
    with pytest.raises(SchemaNewer) as excinfo:
        db.Store.open(path, kind="coworld")
    assert (excinfo.value.user_version, excinfo.value.expected_version) == (99, 4)
    assert "newer release" in str(excinfo.value) and str(tmp_path) not in str(excinfo.value)
    assert not isinstance(excinfo.value, SchemaMismatch)
    with pytest.raises(SchemaNewer):
        migrate.rebuild_keeping_rows(path, "coworld")
    assert_untouched_newer_store(path)
    assert [p.name for p in tmp_path.iterdir() if "before-repair" in p.name] == []
    conn = raw(path)
    try:
        status = db.schema_status(conn, "coworld")
    finally:
        conn.close()
    assert status.newer and not status.ok and status.user_version == 99


def test_a_store_of_the_other_kind_is_a_mismatch_even_at_a_higher_version(tmp_path):
    """The workspace store's version is above the Coworld's; the kind check still wins."""
    path = tmp_path / "store.db"
    db.Store.open(path, kind="workspace").close()
    conn = raw(path)
    try:
        status = db.schema_status(conn, "coworld")
    finally:
        conn.close()
    assert not status.ok and not status.newer
    assert any("schema_kind" in problem for problem in status.diff)


def test_read_tx_gives_several_reads_one_snapshot(tmp_path, harness):
    path = tmp_path / "store.db"
    with db.Store.open(path, kind="coworld") as store:
        with store.tx(harness) as tx:
            insert_goal(tx, "one")
            tx.event("goal.created", "G-1", {})
        with db.Store.open(path, kind="coworld") as writer:
            with store.read_tx():
                titles = [r["title"] for r in store.q("SELECT title FROM goals")]
                with writer.tx(harness) as tx:
                    tx.execute("UPDATE goals SET title = 'two'")
                    tx.event("goal.updated", "G-1", {})
                assert store.last_seq() == 1 and titles == ["one"]
            assert store.last_seq() == 2
            assert store.one("SELECT title FROM goals")[0] == "two"
            with store.read_tx():
                with pytest.raises(RuntimeError, match="already open"):
                    with store.read_tx():
                        pass


def test_error_messages_never_name_the_store_path(tmp_path):
    path = tmp_path / "secret-folder" / "store.db"
    db.Store.open(path, kind="coworld").close()
    other = raw(path)
    try:
        other.execute("ALTER TABLE goals ADD COLUMN x TEXT")
    finally:
        other.close()
    with pytest.raises(SchemaMismatch) as excinfo:
        db.Store.open(path, kind="coworld")
    assert "secret-folder" not in str(excinfo.value)
    assert "secret-folder" not in repr(excinfo.value)
