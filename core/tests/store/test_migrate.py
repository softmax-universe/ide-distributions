"""Migrations: numbering, idempotent application, the expected schema and its normalisation."""

from __future__ import annotations

import sqlite3

import pytest

from research_core.store import migrate

COWORLD_TABLES = {
    "task_xp_waits",
    "transition_commands",
    "task_views",
    "meta",
    "events",
    "change_log",
    "handled_events",
    "goals",
    "tasks",
    "task_deps",
    "task_sessions",
    "task_messages",
    "versions",
    "version_parents",
    "champion",
    "champion_history",
    "claims",
    "claim_evidence",
    "claims_fts",
    "pins",
    "experiments",
    "experiment_runs",
    "experiments_fts",
    "directives",
    "capabilities",
    "settings",
    "ui_state",
    "intents",
}

WORKSPACE_TABLES = {
    "transition_effects",
    "task_assignments",
    "client_admissions",
    "request_events",
    "xp_followups",
    "chat_updates",
    "meta",
    "events",
    "change_log",
    "handled_events",
    "coworlds",
    "directives",
    "settings",
    "notices",
    "sessions",
    "ui_state",
}


def memory_store(kind: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate.apply_migrations(conn, kind)
    return conn


def tables_of(schema: dict[str, str]) -> set[str]:
    return {key.split(" ", 1)[1] for key in schema if key.startswith("table ")}


def test_expected_schema_has_every_coworld_table():
    schema = migrate.expected_schema("coworld")
    assert tables_of(schema) == COWORLD_TABLES
    # No sessions or notices in the Coworld store: they live in the workspace store.
    assert "table sessions" not in schema and "table notices" not in schema


def test_expected_schema_has_every_workspace_table():
    schema = migrate.expected_schema("workspace")
    assert tables_of(schema) == WORKSPACE_TABLES
    assert "index one_active" in schema


def test_expected_schema_excludes_sqlite_internals_and_fts_shadow_tables():
    schema = migrate.expected_schema("coworld")
    names = {key.split(" ", 1)[1] for key in schema}
    assert not any(name.startswith("sqlite_") for name in names)
    for fts in ("claims_fts", "experiments_fts"):
        for suffix in ("data", "idx", "docsize", "config", "content"):
            assert f"{fts}_{suffix}" not in names
    assert all(key.split(" ", 1)[0] in {"table", "index", "trigger", "view"} for key in schema)


def test_expected_schema_rejects_unknown_kind():
    with pytest.raises(ValueError):
        migrate.expected_schema("nope")


def test_change_log_triggers_cover_every_record_table():
    for kind in ("coworld", "workspace"):
        schema = migrate.expected_schema(kind)
        tables = tables_of(schema) - {"meta", "change_log", "claims_fts", "experiments_fts"}
        for table in tables:
            assert f"trigger {table}_log_insert" in schema, (kind, table)
            if table in {"events", "request_events"}:
                assert f"trigger {table}_log_update" not in schema
                assert f"trigger {table}_append_only_update" in schema
                assert f"trigger {table}_append_only_delete" in schema
                continue
            assert f"trigger {table}_log_update" in schema, (kind, table)
            assert f"trigger {table}_log_delete" in schema, (kind, table)
        assert "trigger events_append_only_update" in schema
        assert "trigger events_append_only_delete" in schema
        assert "trigger meta_log_insert" not in schema
        assert "trigger change_log_log_insert" not in schema


def test_fts_sync_triggers_exist():
    schema = migrate.expected_schema("coworld")
    for fts, table in (("claims_fts", "claims"), ("experiments_fts", "experiments")):
        for op in ("insert", "update", "delete"):
            assert f"trigger {fts}_{op}" in schema
            assert f"on {table}" in schema[f"trigger {fts}_{op}"]


def test_fts_tables_index_the_right_columns():
    schema = migrate.expected_schema("coworld")
    assert "content='claims'" in schema["table claims_fts"]
    assert "content_rowid='id'" in schema["table claims_fts"]
    assert "content='experiments'" in schema["table experiments_fts"]
    for column in ("summary", "hypothesis", "methods", "results", "tags"):
        assert column in schema["table experiments_fts"]


def test_migration_files_are_numbered_and_increasing():
    for kind in ("coworld", "workspace"):
        files = migrate.migration_files(kind)
        numbers = [n for n, _ in files]
        assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)
        assert numbers[0] == 1
        assert all(path.name.startswith(f"{n:04d}_") for n, path in files)


def test_apply_migrations_sets_user_version_and_meta():
    conn = memory_store("coworld")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    assert rows["schema_kind"] == "coworld"
    assert rows["created"].endswith("Z")
    conn = memory_store("workspace")
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema_kind'").fetchone()[0] == (
        "workspace"
    )


def test_second_apply_is_a_noop():
    conn = memory_store("coworld")
    created = conn.execute("SELECT value FROM meta WHERE key = 'created'").fetchone()[0]
    conn.execute("INSERT INTO settings(key, value) VALUES ('paused', 'false')")
    assert migrate.apply_migrations(conn, "coworld") == 4
    assert conn.execute("SELECT value FROM meta WHERE key = 'created'").fetchone()[0] == created
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1


def test_apply_migrations_returns_the_version_it_reached():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    assert migrate.apply_migrations(conn, "workspace") == 14
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 14


def test_admission_waiting_migration_keeps_rows_and_adds_no_hold():
    """0008 adds the hold columns; existing rows keep every earlier column (the native message
    id, the JSON payload and the 0005 execution columns) and get no class, no hold and no
    resumes. A waiting admission is stored as 'queued', so the status CHECK is unchanged."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for number, path in migrate.migration_files("workspace")[:7]:
        migrate._run_migration(conn, number, path)
    payload = '{"input":[{"role":"user","content":"hi"}],"instructions":"be brief","context":null}'
    conn.execute(
        "INSERT INTO client_admissions (rowid,op_id,kind,request,session_id,message,payload,"
        "status,run_id,dispatch_started,error,created_at,native_message_id,origin)"
        " VALUES (7,'op','message','{}','s','hi',?,'failed','run_1',1,'boom','t',42,'client')",
        (payload,),
    )
    assert migrate.apply_migrations(conn, "workspace") == 14
    row = conn.execute("SELECT rowid, * FROM client_admissions").fetchone()
    assert (row["rowid"], row["op_id"], row["status"], row["error"]) == (7, "op", "failed", "boom")
    assert (row["payload"], row["native_message_id"], row["origin"]) == (payload, 42, "client")
    assert (row["error_class"], row["resume_after"], row["resumes"]) == (None, None, 0)
    assert (
        migrate.schema_diff(migrate.expected_schema("workspace"), migrate.actual_schema(conn)) == []
    )


def test_terminal_admissions_flagged_for_resolution_are_settled():
    """0011 clears the resolution flag 0006 left on admissions that had already ended; one
    that is still queued keeps it for the coordinator to close."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for number, path in migrate.migration_files("workspace")[:10]:
        migrate._run_migration(conn, number, path)
    for op_id, status in (("ended", "failed"), ("open", "queued")):
        conn.execute(
            "INSERT INTO client_admissions (op_id,kind,request,session_id,message,payload,status,"
            "dispatch_started,resolution_required,created_at)"
            " VALUES (?,'message','{}','s','hi','{}',?,1,1,'t')",
            (op_id, status),
        )
    assert migrate.apply_migrations(conn, "workspace") == 14
    flags = dict(conn.execute("SELECT op_id, resolution_required FROM client_admissions"))
    assert flags == {"ended": 0, "open": 1}


def test_scoped_notices_upgrade_keeps_existing_workspace_notices():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for number, path in migrate.migration_files("workspace")[:8]:
        migrate._run_migration(conn, number, path)
    conn.execute(
        "INSERT INTO notices(text,refs,urgency,created,created_by)"
        " VALUES ('Existing news','[]','normal','t','harness')"
    )
    assert migrate.apply_migrations(conn, "workspace") == 14
    row = conn.execute("SELECT * FROM notices").fetchone()
    assert row["text"] == "Existing news" and row["coworld_id"] is None


def test_only_a_hold_may_clear_an_execution_receipt():
    """0008 narrows the immutable receipt: a hold clears it, nothing else changes or clears it."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    migrate.apply_migrations(conn, "workspace")
    conn.execute(
        "INSERT INTO client_admissions (op_id,kind,request,session_id,message,payload,status,"
        "run_id,dispatch_started,created_at)"
        " VALUES ('op','message','{}','s','hi','{}','running','run_1',1,'t')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="execution receipt cannot change"):
        conn.execute("UPDATE client_admissions SET run_id='run_2'")
    with pytest.raises(sqlite3.IntegrityError, match="execution receipt cannot change"):
        conn.execute("UPDATE client_admissions SET run_id=NULL")
    with pytest.raises(sqlite3.IntegrityError, match="execution receipt cannot change"):
        conn.execute("UPDATE client_admissions SET run_id=NULL,status='queued'")
    conn.execute("UPDATE client_admissions SET run_id=NULL,status='queued',resume_after='t2'")
    conn.execute("UPDATE client_admissions SET run_id='run_2',status='running',resume_after=NULL")
    assert conn.execute("SELECT run_id FROM client_admissions").fetchone()[0] == "run_2"


def test_migrations_apply_in_numeric_order_and_only_pending_ones(tmp_path, monkeypatch):
    root = tmp_path / "migrations"
    folder = root / "coworld"
    folder.mkdir(parents=True)
    # Each step depends on the previous one, so any other order fails.
    (folder / "0003_add_column.sql").write_text("ALTER TABLE t ADD COLUMN b TEXT;\n")
    (folder / "0001_create.sql").write_text(
        "CREATE TABLE t(a INTEGER PRIMARY KEY);\n"
        "CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN\n"
        "  INSERT INTO t2(a) VALUES (NEW.a);\n"
        "END;\n"
        "CREATE TABLE t2(a INTEGER);\n"
    )
    (folder / "0002_seed.sql").write_text("INSERT INTO t(a) VALUES (1);\n")
    (folder / "notes.txt").write_text("ignored\n")
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", root)
    migrate.expected_schema.cache_clear()

    assert [n for n, _ in migrate.migration_files("coworld")] == [1, 2, 3]
    conn = sqlite3.connect(":memory:", isolation_level=None)
    assert migrate.apply_migrations(conn, "coworld") == 3
    assert conn.execute("SELECT a, b FROM t").fetchall() == [(1, None)]
    assert conn.execute("SELECT a FROM t2").fetchall() == [(1,)]

    # A store already at version 2 gets only 0003 (0001 would fail: the table exists).
    partial = sqlite3.connect(":memory:", isolation_level=None)
    partial.execute("CREATE TABLE t(a INTEGER PRIMARY KEY)")
    partial.execute("CREATE TABLE t2(a INTEGER)")
    partial.execute("PRAGMA user_version = 2")
    assert migrate.apply_migrations(partial, "coworld") == 3
    assert [r[1] for r in partial.execute("PRAGMA table_info(t)")] == ["a", "b"]
    migrate.expected_schema.cache_clear()


def test_a_failing_migration_leaves_no_half_applied_state(tmp_path, monkeypatch):
    root = tmp_path / "migrations"
    folder = root / "workspace"
    folder.mkdir(parents=True)
    (folder / "0001_ok.sql").write_text("CREATE TABLE t(a);\n")
    (folder / "0002_bad.sql").write_text("CREATE TABLE u(a);\nCREATE TABLE t(a);\n")
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", root)
    migrate.expected_schema.cache_clear()
    conn = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(sqlite3.OperationalError):
        migrate.apply_migrations(conn, "workspace")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == {"t"}
    assert conn.in_transaction is False
    migrate.expected_schema.cache_clear()


def test_badly_named_migration_files_are_rejected(tmp_path, monkeypatch):
    root = tmp_path / "migrations"
    folder = root / "coworld"
    folder.mkdir(parents=True)
    (folder / "0001_ok.sql").write_text("CREATE TABLE t(a);\n")
    (folder / "0001_dup.sql").write_text("CREATE TABLE u(a);\n")
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", root)
    with pytest.raises(ValueError, match="0001"):
        migrate.migration_files("coworld")
    (folder / "0001_dup.sql").unlink()
    (folder / "2_bad.sql").write_text("CREATE TABLE u(a);\n")
    with pytest.raises(ValueError, match="2_bad.sql"):
        migrate.migration_files("coworld")


def test_normalise_sql_collapses_whitespace_and_case():
    assert migrate.normalise_sql("CREATE  TABLE\n  x (\n\ta  INT\n)") == "create table x ( a int )"
    assert migrate.normalise_sql("  create table X(a)  ") == "create table x(a)"


def test_actual_schema_matches_expected_for_a_fresh_file(tmp_path):
    for kind in ("coworld", "workspace"):
        conn = sqlite3.connect(tmp_path / f"{kind}.db", isolation_level=None)
        migrate.apply_migrations(conn, kind)
        assert migrate.schema_diff(migrate.expected_schema(kind), migrate.actual_schema(conn)) == []
        conn.close()


def test_schema_diff_names_each_kind_of_difference():
    expected = {"table a": "create table a(x)", "index a_x": "create index a_x on a(x)"}
    actual = {"table a": "create table a(x, y)", "trigger t": "create trigger t ..."}
    diff = migrate.schema_diff(expected, actual)
    assert len(diff) == 3
    assert any(d.startswith("table a differs") for d in diff)
    assert any(d.startswith("missing index a_x") for d in diff)
    assert any(d.startswith("unexpected trigger t") for d in diff)


def test_check_constraints_hold():
    conn = memory_store("coworld")
    now = "2026-10-28T00:00:00.000000Z"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO goals(title, end_state, reason, status, created, created_by, updated)"
            " VALUES ('t', 'e', 'r', 'parked', ?, 'me', ?)",
            (now, now),
        )
    conn.execute(
        "INSERT INTO goals(title, end_state, reason, status, created, created_by, updated)"
        " VALUES ('t', 'e', 'r', 'active', ?, 'me', ?)",
        (now, now),
    )
    base = (
        "INSERT INTO tasks(goal_id, type, title, description, status, created, created_by, updated)"
    )
    # A maintenance Task may have no Goal; any other type needs one.
    conn.execute(
        f"{base} VALUES (NULL, 'maintenance', 't', 'd', 'unclaimed', ?, 'me', ?)", (now, now)
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"{base} VALUES (NULL, 'research', 't', 'd', 'unclaimed', ?, 'me', ?)", (now, now)
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"{base} VALUES (1, 'research', 't', 'd', 'abandoned', ?, 'me', ?)", (now, now)
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO task_deps(task_id, depends_on) VALUES (1, 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO versions(summary, source, created, created_by)"
            " VALUES ('s', 'built', ?, 'me')",
            (now,),
        )
    conn.execute(
        "INSERT INTO versions(summary, source, created, created_by)"
        " VALUES ('s', 'adopted', ?, 'me')",
        (now,),
    )
    conn.execute("INSERT INTO intents(kind, key, state) VALUES ('upload', 'k', 'pending')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO intents(kind, key, state) VALUES ('upload', 'k', 'pending')")
    for kind in ("upload", "submit", "xp", "forum", "wiki", "run", "champion"):
        conn.execute("INSERT INTO intents(kind, key, state) VALUES (?, ?, 'pending')", (kind, kind))
    assert [r[1] for r in conn.execute("PRAGMA table_info(task_sessions)")] == [
        "task_id",
        "session_id",
        "kind",
        "epoch",
        "started",
    ]


def test_workspace_one_active_coworld():
    conn = memory_store("workspace")
    conn.execute("INSERT INTO coworlds(coworld_id, folder, active) VALUES ('a', 'a', 1)")
    conn.execute("INSERT INTO coworlds(coworld_id, folder, active) VALUES ('b', 'b', 0)")
    conn.execute("INSERT INTO coworlds(coworld_id, folder, active) VALUES ('c', 'c', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE coworlds SET active = 1 WHERE coworld_id = 'b'")
    columns = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    assert "coworld_id" in columns and "task_id" in columns
    assert conn.execute("PRAGMA foreign_key_list(sessions)").fetchall() == []


def test_message_content_migration_preserves_queued_delivery_and_untracked_writes():
    import json

    conn = sqlite3.connect(":memory:", isolation_level=None)
    for number, path in migrate.migration_files("workspace"):
        if number < 4:
            migrate._run_migration(conn, number, path)
    conn.execute(
        "INSERT INTO client_admissions"
        "(op_id,kind,request,session_id,message,payload,status,created_at)"
        " VALUES ('op1','message',?,'chat1','hello','hello','queued','2026-01-01')",
        (json.dumps({"message": "hello", "context": {"contract_version": 1}}),),
    )
    untracked = conn.execute("SELECT id FROM change_log WHERE token IS NULL").fetchall()
    migrate.apply_migrations(conn, "workspace")
    assert json.loads(conn.execute("SELECT payload FROM client_admissions").fetchone()[0]) == {
        "input": "hello",
        "instructions": None,
        "context": {"contract_version": 1},
    }
    request = json.loads(conn.execute("SELECT request FROM client_admissions").fetchone()[0])
    assert request["message"] == {
        "role": "user",
        "content": "hello",
    }
    assert conn.execute("SELECT id FROM change_log WHERE token IS NULL").fetchall() == untracked


def test_admission_schema_does_not_activate_legacy_quarantine_before_its_owner_exists():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    for number, path in migrate.migration_files("workspace"):
        if number < 5:
            migrate._run_migration(conn, number, path)
    conn.execute(
        "INSERT INTO client_admissions"
        "(op_id,kind,request,session_id,message,payload,status,created_at,dispatch_started,error)"
        " VALUES ('uncertain','message','{}','chat','hello','{}','queued','2026-01-01',1,"
        "'lost native response')"
    )
    untracked = conn.execute("SELECT id FROM change_log WHERE token IS NULL").fetchall()
    number, path = next(item for item in migrate.migration_files("workspace") if item[0] == 5)
    migrate._run_migration(conn, number, path)
    assert conn.execute(
        "SELECT status,dispatch_started,resolution_required,error FROM client_admissions"
    ).fetchone() == ("queued", 1, 0, "lost native response")
    assert conn.execute("SELECT id FROM change_log WHERE token IS NULL").fetchall() == untracked


@pytest.mark.parametrize("lost_receipt", [False, True])
@pytest.mark.parametrize("state", ["running", "failed", "interrupted"])
def test_attempt_pointer_migration_preserves_live_authority_or_first_queued_successor(
    lost_receipt, state
):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for version, path in migrate.migration_files("workspace"):
        if version <= 6:
            conn.executescript(path.read_text())
    conn.execute(
        "INSERT INTO sessions(id,profile,kind,started,run_id)"
        " VALUES ('worker','researcher','task','2026-11-01T00:00:00Z','second-run')"
    )
    for op_id, status, mode, run_id in (
        ("first", "failed", "start", "first-run"),
        ("second", state, "start", "second-run"),
        ("guidance", "running", "steer", "second-run"),
        ("queued-retry", "queued", "start", None),
        ("queued-guidance", "queued", "start", None),
    ):
        conn.execute(
            "INSERT INTO client_admissions(op_id,kind,request,session_id,message,payload,status,"
            "created_at,delivery_mode,run_id,dispatch_started)"
            " VALUES (?,'message','{}','worker','','{}',?,"
            "'2026-11-01T00:00:00Z',?,?,?)",
            (op_id, status, mode, run_id, int(status != "queued")),
        )
    if state == "interrupted":
        conn.execute("UPDATE client_admissions SET execution_uncertain=1 WHERE op_id='second'")
    if lost_receipt:
        conn.execute(
            "INSERT INTO client_admissions(op_id,kind,request,session_id,message,payload,status,"
            "created_at,delivery_mode,dispatch_started)"
            " VALUES ('lost-receipt','message','{}','worker','','{}','queued',"
            "'2026-11-01T00:00:00Z','start',1)"
        )
    conn.execute(
        "INSERT INTO task_assignments(coworld_id,task_id,op_id,session_id,profile,task_updated,"
        "rationale,context) VALUES ('world',1,'first','worker','researcher','','','')"
    )
    conn.execute("PRAGMA user_version=6")
    migrate.apply_migrations(conn, "workspace")
    expected = "lost-receipt" if lost_receipt else "queued-retry" if state == "failed" else "second"
    assert conn.execute("SELECT current_attempt_id FROM task_assignments").fetchone()[0] == expected
