"""Steps 1 and 2: the pause check, unmarked-write detection, schema repairs and the refusal of a
store a newer release wrote (design B.10)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from research_core.coordinator import Coordinator
from research_core.store import notices, settings
from research_core.store.db import Store
from research_core.store.errors import SchemaMismatch, SchemaNewer
from research_core.watcher import context, store_checks
from tests.watcher._support import USER, events_of, make_ctx, set_setting


def test_pause_state_reads_the_workspace_setting(tmp_path):
    ctx = make_ctx(tmp_path)
    assert store_checks.pause_state(ctx) is False
    set_setting(ctx, "paused", True)
    assert store_checks.pause_state(ctx) is True


def test_labelled_stores_names_the_workspace_and_the_coworld(tmp_path):
    ctx = make_ctx(tmp_path)
    assert [label for label, _ in store_checks.labelled_stores(ctx)] == ["workspace", "cow_fake"]
    ctx.store = None
    assert [label for label, _ in store_checks.labelled_stores(ctx)] == ["workspace"]


def test_nothing_to_report_on_a_clean_store(tmp_path):
    ctx = make_ctx(tmp_path)
    settings.update(ctx.store, USER, "submissions.mode", "ask", cites="msg-1")  # a marked write
    assert store_checks.run(ctx) == {"workspace": {}, "cow_fake": {}}
    assert events_of(ctx.store, "store.unmarked_write") == []


def test_unmarked_writes_in_the_coworld_store_are_reported_once(tmp_path):
    ctx = make_ctx(tmp_path)
    raw = sqlite3.connect(ctx.store.path)
    raw.execute(
        "INSERT INTO directives(text, scope, source, status)"
        " VALUES ('be bold', 'coworld', 'user', 'active')"
    )
    raw.execute("UPDATE directives SET text = 'be bolder' WHERE text = 'be bold'")
    raw.commit()
    raw.close()
    report = store_checks.run(ctx)
    assert report == {"workspace": {}, "cow_fake": {"unmarked_writes": 2}}
    [event] = events_of(ctx.store, "store.unmarked_write")
    assert event["subject"] == "cow_fake" and event["payload"]["store"] == "cow_fake"
    assert event["payload"]["count"] == 2 and event["payload"]["tables"] == ["directives"]
    assert [row["op"] for row in event["payload"]["rows"]] == ["insert", "update"]
    [notice] = notices.unread(ctx.wstore, coworld_id="cow_fake")
    assert "Coworld cow_fake" in notice["text"] and "directives" in notice["text"]
    assert ctx.store.unmarked_changes() == []
    assert store_checks.run(ctx) == {"workspace": {}, "cow_fake": {}}
    assert len(events_of(ctx.store, "store.unmarked_write")) == 1


def test_schema_mismatch_rebuilds_the_workspace_store_keeping_rows(tmp_path):
    ctx = make_ctx(tmp_path)
    set_setting(ctx, "max_researchers", 3)
    raw = sqlite3.connect(ctx.wstore.path)
    raw.execute("CREATE TABLE scratch (id INTEGER PRIMARY KEY, note TEXT)")
    raw.execute("INSERT INTO scratch(note) VALUES ('left by a shell command')")
    raw.commit()
    raw.close()
    old = ctx.wstore
    report = store_checks.run(ctx)
    assert report["workspace"]["schema_rebuilt"] >= 1
    assert ctx.wstore is not old and ctx.wstore.schema_status().ok
    assert settings.get(ctx.wstore, "max_researchers") == 3
    [event] = events_of(ctx.wstore, "store.schema_rebuilt")
    assert event["subject"] == "workspace"
    assert "unexpected table scratch" in event["payload"]["differences"]
    assert event["payload"]["rebuild"]["dropped_tables"] == ["scratch"]
    backup = Path(event["payload"]["rebuild"]["backup"])
    assert backup.parent == ctx.wstore.path.parent and backup.is_file()
    [notice] = notices.unread(ctx.wstore)
    assert "rebuilt" in notice["text"] and "scratch" in notice["text"]
    assert f"A copy of the store as it was is at {backup}" in notice["text"]
    assert ctx.wstore.one("SELECT 1 FROM sqlite_master WHERE name = 'scratch'") is None


def test_repair_unopenable_rebuilds_both_stores_before_a_tick(tmp_path):
    ctx = make_ctx(tmp_path)
    set_setting(ctx, "submissions.mode", "ask")
    wstore_path, store_path = ctx.wstore.path, ctx.store.path
    ctx.wstore.close()
    ctx.store.close()
    for path in (wstore_path, store_path):
        raw = sqlite3.connect(path)
        raw.execute("ALTER TABLE directives ADD COLUMN junk TEXT")
        raw.commit()
        raw.close()
    try:
        Store.open(wstore_path, kind="workspace")
    except SchemaMismatch:
        pass
    else:
        raise AssertionError("the altered store should not open")

    repaired = store_checks.repair_unopenable({"IDE_WORKSPACE": str(ctx.ws.root)})
    assert [entry["store"] for entry in repaired] == ["workspace", "cow_fake"]
    assert all(entry["dropped_columns"] == {"directives": ["junk"]} for entry in repaired)
    assert all(Path(entry["backup"]).is_file() for entry in repaired)
    with Store.open(wstore_path, kind="workspace") as wstore:
        assert len(events_of(wstore, "store.schema_rebuilt")) == 1
        texts = [n["text"] for n in notices.unread(wstore)]
        assert len(texts) == 2 and any("Coworld cow_fake" in t for t in texts)
    with Store.open(store_path, kind="coworld") as store:
        assert len(events_of(store, "store.schema_rebuilt")) == 1
        assert settings.get(store, "submissions.mode") == "ask"
    assert store_checks.repair_unopenable({"IDE_WORKSPACE": str(ctx.ws.root)}) == []


# --- a store a newer release wrote -----------------------------------------------------------


def make_newer(path: Path) -> None:
    """A store a newer release used: a higher version and a populated table this one lacks."""
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE future (id INTEGER PRIMARY KEY, note TEXT)")
    raw.execute("INSERT INTO future(note) VALUES ('kept by the newer release')")
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()


def assert_newer_store_untouched(path: Path) -> None:
    raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 99
        assert raw.execute("SELECT COUNT(*) FROM future").fetchone()[0] == 1
        assert raw.execute("SELECT value FROM settings WHERE key = 'submissions.mode'").fetchone()[
            0
        ] == json.dumps("ask")
    finally:
        raw.close()
    assert [p.name for p in path.parent.iterdir() if "before-repair" in p.name] == []


def tick_env(ctx, tmp_path) -> dict[str, str]:
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    return {
        "HERMES_HOME": str(profile),
        "IDE_WORKSPACE": str(ctx.ws.root),
        "IDE_PLATFORM": "fake",
    }


def test_a_newer_coworld_store_is_left_alone_reported_once_and_pauses_the_workspace(tmp_path):
    ctx = make_ctx(tmp_path)
    set_setting(ctx, "submissions.mode", "ask")
    wstore_path, store_path = ctx.wstore.path, ctx.store.path
    ctx.wstore.close()
    ctx.store.close()
    make_newer(store_path)
    with pytest.raises(SchemaNewer):
        Store.open(store_path, kind="coworld")

    env = {"IDE_WORKSPACE": str(ctx.ws.root)}
    assert store_checks.repair_unopenable(env) == []  # nothing was rebuilt
    assert_newer_store_untouched(store_path)
    with Store.open(wstore_path, kind="workspace") as wstore:
        [event] = events_of(wstore, "store.schema_newer")
        assert event["subject"] == "cow_fake"
        assert event["payload"] == {
            "store": "cow_fake",
            "user_version": 99,
            "expected_version": 4,
        }
        [notice] = notices.unread(wstore)
        assert "Coworld cow_fake" in notice["text"] and "newer release" in notice["text"]
        assert "schema version 99" in notice["text"] and "paused" in notice["text"]
        assert settings.get(wstore, "paused") is True
        [paused] = events_of(wstore, "settings.updated")
        assert paused["payload"]["cites"] == notice["id"] and paused["actor"] == "harness"
    # A second look reports nothing new.
    assert store_checks.repair_unopenable(env) == []
    with Store.open(wstore_path, kind="workspace") as wstore:
        assert len(events_of(wstore, "store.schema_newer")) == 1
        assert len(notices.unread(wstore)) == 1
    assert_newer_store_untouched(store_path)


def test_the_tick_keeps_running_its_paused_steps_without_the_newer_store(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    set_setting(ctx, "submissions.mode", "ask")
    wstore_path, store_path = ctx.wstore.path, ctx.store.path
    ctx.wstore.close()
    ctx.store.close()
    make_newer(store_path)
    env = tick_env(ctx, tmp_path)

    from research_core.watcher import context

    store_checks.repair_unopenable(env)
    observed = context.build(env, coworld_id="cow_fake")
    first = Coordinator(observed.ws, observed.hermes).advance(observed)
    second = Coordinator(observed.ws, observed.hermes).advance(observed)
    assert settings.get(observed.wstore, "paused") is True
    assert first["store_checks"] == {"workspace": {}, "cow_fake": {"schema_newer": 99}}
    assert set(second) == {"store_checks", "heartbeat", "context_panel", "drain"}
    assert second["drain"]["errors"] == [
        {"coworld": "cow_fake", "error": "store schema is newer than this runtime"}
    ]
    assert observed.wstore.get_meta("paused_drained") != "1"
    observed.wstore.close()
    assert_newer_store_untouched(store_path)
    with Store.open(wstore_path, kind="workspace") as wstore:
        assert len(events_of(wstore, "store.schema_newer")) == 1
        assert wstore.get_meta("heartbeat")


def test_a_store_turning_newer_while_open_is_set_aside_in_the_tick(tmp_path):
    ctx = make_ctx(tmp_path)
    set_setting(ctx, "submissions.mode", "ask")
    store_path = ctx.store.path
    make_newer(store_path)
    report = store_checks.run(ctx)
    assert report == {"workspace": {}, "cow_fake": {"schema_newer": 99}}
    assert ctx.store is None and ctx.coworld is not None
    assert settings.get(ctx.wstore, "paused") is True
    assert len(events_of(ctx.wstore, "store.schema_newer")) == 1
    assert store_checks.run(ctx) == {"workspace": {}, "cow_fake": {"schema_newer": 99}}
    assert len(events_of(ctx.wstore, "store.schema_newer")) == 1
    assert_newer_store_untouched(store_path)


def test_a_newer_workspace_store_stops_coordinator_context_with_the_reason(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    set_setting(ctx, "paused", False)
    wstore_path = ctx.wstore.path
    ctx.wstore.close()
    ctx.store.close()
    raw = sqlite3.connect(wstore_path)
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    env = tick_env(ctx, tmp_path)
    from research_core.store.errors import SchemaNewer

    with pytest.raises(SchemaNewer, match="newer release"):
        context.build(env)
    raw = sqlite3.connect(f"file:{wstore_path}?mode=ro", uri=True)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 99
    finally:
        raw.close()
    assert [p.name for p in wstore_path.parent.iterdir() if "before-repair" in p.name] == []


def test_the_newer_report_is_forgotten_once_the_store_opens_again(tmp_path):
    ctx = make_ctx(tmp_path)
    store_path = ctx.store.path
    make_newer(store_path)
    store_checks.run(ctx)
    assert ctx.wstore.get_meta("schema_newer:cow_fake") == "99"
    # The matching runtime arrived (here: the store is put back to this release's version).
    raw = sqlite3.connect(store_path)
    raw.execute("DROP TABLE future")
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()
    ctx.store = Store.open(store_path, kind="coworld", clock=ctx.clock)
    assert store_checks.run(ctx) == {"workspace": {}, "cow_fake": {}}
    assert ctx.wstore.get_meta("schema_newer:cow_fake") is None
