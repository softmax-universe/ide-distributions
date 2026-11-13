"""The ``workspace`` tool: coworld_switch, worktree_open, doctor, capabilities and the guide."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import research_core.tools.workspace  # noqa: F401  (registers the tool)
from research_core import environment_guide, gitops
from research_core.store import capabilities, coworlds, sessions
from research_core.store.actors import Actor
from research_core.tools.base import ToolContext, run_tool
from tests.fixtures.fakegame_repo import make_fakegame_repo


def test_doctor_reports_both_stores_ok(call, ctx_factory):
    ctx = ctx_factory()
    ctx.wstore.set_meta("heartbeat", ctx.wstore.now())
    capabilities.mark(ctx.store, Actor.harness(), "build", status="broken", output="boom")

    result = call("workspace", {"action": "doctor"}, role="researcher")
    assert result["ok"] is True
    assert result["stores"]["workspace"]["ok"] is True and result["stores"]["coworld"]["ok"] is True
    assert result["stores"]["workspace"]["diff"] == []
    assert result["watcher"]["heartbeat"] and result["watcher"]["age_s"] < 60
    assert [c["name"] for c in result["capabilities"]] == ["build"]
    assert [c["name"] for c in result["clis"]["clis"]] == ["coworld", "softmax", "research"]
    for entry in result["clis"]["clis"]:
        assert entry["version"] and entry["lock"] and entry["stale"] is False
    assert result["commits"]["source"] and result["core_path"]["writable"] in (True, False)
    assert result["api_keys"]["note"] == "HERMES_HOME is not set"
    assert "broken capabilities: build" in result["problems"]
    assert not any("heartbeat" in p for p in result["problems"])
    assert ".state" not in json.dumps(result)


def test_doctor_without_heartbeat_or_active_coworld(ctx_factory):
    ctx = ctx_factory(coworld=None)
    result = json.loads(run_tool("workspace", {"action": "doctor"}, ctx))
    assert result["ok"] is True and result["stores"]["coworld"] is None
    assert result["capabilities"] == [] and result["skill_overrides"] == []
    assert "the watcher has not recorded a heartbeat yet" in result["problems"]


def test_doctor_compares_api_keys_across_profiles(tmp_path, workspace_env, ctx_factory):
    ctx_factory()
    profiles = tmp_path / "profiles"
    (profiles / "orchestrator").mkdir(parents=True)
    (profiles / "orchestrator" / ".env").write_text('API_SERVER_KEY="key-one"\nOTHER=x\n')
    (profiles / "researcher-1").mkdir()
    (profiles / "researcher-1" / ".env").write_text("export API_SERVER_KEY=key-two\n")
    (profiles / "researcher-2").mkdir()
    (profiles / "orchestrator" / ".softmax-runtime.json").write_text(
        json.dumps({"image": {"ref": "ghcr.io/x/ide:abc123"}, "profile": {}})
    )
    env = {
        **workspace_env,
        "IDE_SESSION_ID": "s1",
        "HERMES_HOME": str(profiles / "orchestrator"),
        "IDE_RESEARCHER_PROFILES": "researcher-1,researcher-2",
    }
    result = json.loads(
        run_tool("workspace", {"action": "doctor"}, ToolContext.from_env("orchestrator", env))
    )
    assert result["api_keys"] == {
        "checked": ["orchestrator", "researcher-1", "researcher-2"],
        "differing": ["researcher-1"],
        "missing": ["researcher-2"],
    }
    assert result["commits"]["image"] == "ghcr.io/x/ide:abc123"
    assert "API_SERVER_KEY differs from this profile's in: researcher-1" in result["problems"]
    assert "key-one" not in json.dumps(result) and "key-two" not in json.dumps(result)


def test_environment_guide_falls_back_to_the_player_id_the_login_check_stored(
    workspace_env, ctx_factory
):
    ctx = ctx_factory()
    assert "Player identity: `unknown`" in environment_guide.render(ctx)
    ctx.wstore.set_meta("player_id", "ply_checked")
    assert "Player identity: `ply_checked`" in environment_guide.render(ctx)


def test_environment_guide_names_the_workspace_and_player_but_no_secret(workspace_env, ctx_factory):
    ctx_factory()
    env = {
        **workspace_env,
        "IDE_SESSION_ID": "s1",
        "SOFTMAX_PLAYER_ID": "ply_test",
        "SOFTMAX_PLAYER_CREDENTIAL": "cga_secret_value",
        "API_SERVER_KEY": "hermes_secret_value",
        "TMPDIR": "/scratch/tmp",
    }
    ctx = ToolContext.from_env("researcher", env)
    text = environment_guide.render(ctx)
    assert text.startswith("# Environment guide")
    assert str(ctx.workspace.root) in text and "cow_fake" in text
    assert "`ply_test`" in text and "credential present" in text
    assert "User identity" in text and "not provisioned" in text
    assert "cga_secret_value" not in text and "hermes_secret_value" not in text
    assert ".state" not in text and "store.db" not in text
    assert "`coworld`" in text and "`softmax`" in text and "`research`" in text
    assert (
        "task_ask" in text
        and "chat_update" not in text.split("## Reaching the user")[1].split("##")[0]
    )
    assert "every 60 seconds" in text and "/scratch/tmp" in text
    assert "No capability check is recorded yet" in text
    assert "in no git repository" in text and "`skill_manage` `patch`" in text

    result = json.loads(run_tool("workspace", {"action": "environment_guide"}, ctx))
    assert result["ok"] is True and str(ctx.workspace.root) in result["guide"]
    assert ".state" not in result["guide"] and "<state>" not in result["guide"]

    orchestrator = ToolContext.from_env("orchestrator", env)
    capabilities.mark(orchestrator.store, Actor.harness(), "build", status="ok", output="fine")
    capabilities.mark(orchestrator.store, Actor.harness(), "upload", status="broken", output="no")
    guide = environment_guide.render(orchestrator)
    assert "chat_update" in guide and "1 broken, 1 ok" in guide and "Broken: upload" in guide


def test_capabilities_lists_and_check_marks_due(call, ctx_factory):
    ctx = ctx_factory()
    listed = call("workspace", {"action": "capabilities"}, role="researcher")
    assert listed == {"ok": True, "capabilities": [], "broken": []}
    assert ctx.store.get_meta("checks_due") is None

    due = call("workspace", {"action": "capabilities", "check": True})
    assert due["checks_due"] == ctx.store.get_meta("checks_due")
    assert "watcher runs them on its next tick" in due["note"]
    capabilities.mark(ctx.store, Actor.harness(), "build", status="broken", output="boom")
    capabilities.mark(ctx.store, Actor.harness(), "cli:coworld", status="ok", output="help " * 999)
    again = call("workspace", {"action": "capabilities"})
    rows = {c["name"]: c for c in again["capabilities"]}
    assert list(rows) == ["build", "cli:coworld"] and again["broken"] == ["build"]
    assert rows["build"]["output_tail"] == "boom" and "output" not in rows["build"]
    assert "output" not in rows["cli:coworld"] and "output_tail" not in rows["cli:coworld"]
    assert "checks_due" not in again


def test_worktree_open_creates_orch_worktrees_from_main(call, ctx_factory):
    ctx = ctx_factory()
    coworld = ctx.coworld()
    refused = call("workspace", {"action": "worktree_open"})
    assert refused["ok"] is False and "code repository does not exist" in refused["error"]

    gitops.init_repo(coworld.code)
    (coworld.code / "policy.json").write_text("{}\n")
    main = gitops.commit_all(coworld.code, "Add the policy")

    first = call("workspace", {"action": "worktree_open"})
    assert first["ok"] is True and first["name"] == "orch-1" and first["branch"] == "orch/1"
    assert first["worktree"] == str(coworld.worktrees / "orch-1") and first["head"] == main
    assert (coworld.worktrees / "orch-1" / "policy.json").is_file()
    assert gitops.current_branch(coworld.worktrees / "orch-1") == "orch/1"

    second = call("workspace", {"action": "worktree_open"})
    assert second["name"] == "orch-2" and second["branch"] == "orch/2"
    reopened = call("workspace", {"action": "worktree_open", "name": "orch-1"})
    assert reopened["worktree"] == first["worktree"] and reopened["branch"] == "orch/1"
    bad = call("workspace", {"action": "worktree_open", "name": "task-3"})
    assert bad["ok"] is False and "orch-2" in bad["error"]
    assert call("workspace", {"action": "worktree_open"}, role="researcher")["ok"] is False


def test_coworld_switch_through_the_tool(call, ctx_factory, tmp_path):
    ctx = ctx_factory()
    repo, sha = make_fakegame_repo(tmp_path / "upstream")
    ctx.platform().configure(source_url=repo.as_uri(), source_commit=sha)
    request = {"text": "submit the starter as my champion", "cites": "msg-1"}

    result = call(
        "workspace",
        {
            "action": "coworld_switch",
            "coworld": "cow_fake",
            "cites": "msg-1",
            "pending_request": request,
        },
    )
    assert result["ok"] is True, result
    assert result["coworld"]["active"] is True and result["coworld"]["pending_request"] == request
    assert result["setup"]["status"] == "needed"
    assert result["setup"]["fetch"]["mixin"]["path"] == str(ctx.coworld().mixin)
    assert result["setup"]["fetch"]["lessons"]["claims"] == 2
    assert result["notice"] == "N-1"
    events = [e for e in ctx.wstore.events_after(0) if e.kind == "coworld.setup_needed"]
    assert len(events) == 1 and events[0].payload["pending_request"] == request
    assert coworlds.active(ctx.wstore)["coworld_id"] == "cow_fake"
    assert ".state" not in json.dumps(result)

    missing = call("workspace", {"action": "coworld_switch"})
    assert missing["ok"] is False and "coworld is required" in missing["error"]
    researcher = call(
        "workspace", {"action": "coworld_switch", "coworld": "cow_fake"}, role="researcher"
    )
    assert researcher["ok"] is False and "not available to the researcher" in researcher["error"]


def test_change_complete_makes_a_reviewed_maintenance_task(call, ctx_factory):
    from research_core import gitops
    from research_core.store import tasks

    ctx = ctx_factory("orchestrator", session_id="chat1")
    code = ctx.coworld().code
    if not (code / ".git").exists():
        gitops.init_repo(code)
    opened = call("workspace", {"action": "worktree_open"})
    assert opened["ok"] is True, opened
    worktree = Path(opened["worktree"] if "worktree" in opened else opened["path"])
    (worktree / "note.md").write_text("a change\n")
    sha = gitops.commit_all(worktree, "Add a note")
    done = call(
        "workspace",
        {"action": "change_complete", "worktree": worktree.name, "summary": "Add a note to main"},
    )
    assert done["ok"] is True, done
    record = done["record"]
    assert record["type"] == "maintenance" and record["status"] == "complete"
    assert record["completed_commit"] == sha and record["goal_id"] is None
    task_id = int(record["id"].split("-")[-1])
    assert tasks.read(ctx.store, task_id)["merge"] in ("pending", None)
    kinds = [row["kind"] for row in ctx.store.q("SELECT kind FROM events ORDER BY seq")]
    assert "task.completed" in kinds
    refused = call(
        "workspace",
        {"action": "change_complete", "worktree": "/elsewhere", "summary": "x"},
    )
    assert refused["ok"] is False


def test_coworld_switch_offers_the_source_a_listed_coworld_does_not_need():
    """The working-with-the-ide skill sends these for a fork; a closed schema drops hidden ones."""
    spec = research_core.tools.workspace.SPEC
    assert {"source", "commit"} <= set(spec.properties("orchestrator"))


IDENTITY_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    timestamp REAL NOT NULL
);
"""


@pytest.fixture
def transcript(workspace_env, tmp_path):
    """A Hermes state.db for the workspace; ``say(session, role, text)`` stores a message and
    returns the transcript reference ``session:<id>/<message id>#<sha256>`` that cites it."""
    home = tmp_path / "hermes"
    home.mkdir()
    workspace_env["HERMES_HOME"] = str(home)
    conn = sqlite3.connect(home / "state.db")
    conn.executescript(IDENTITY_SCHEMA)

    def say(session, role, text):
        conn.execute("INSERT OR IGNORE INTO sessions VALUES (?, 'api', 1.0)", (session,))
        cursor = conn.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, 2.0)",
            (session, role, text),
        )
        conn.commit()
        digest = hashlib.sha256(text.encode()).hexdigest()
        return f"session:{session}/{cursor.lastrowid}#{digest}"

    yield say
    conn.close()


@pytest.fixture
def register_session(ctx_factory):
    def register(session_id, kind, **extra):
        # A chat is the user's IDE chat unless a test says another client opened it.
        if kind == "chat":
            extra.setdefault("client", {"id": "aide", "kind": "ide"})
        return sessions.register(
            ctx_factory().wstore,
            Actor.harness(),
            coworld_id="cow_fake",
            session_id=session_id,
            profile="orchestrator",
            kind=kind,
            **extra,
        )

    return register


def identity(call, session_id="c1", **args):
    return call("workspace", {"action": "identity", **args}, session_id=session_id)


def test_identity_names_once_then_the_user_rewrites_once(call, register_session, transcript):
    register_session("c1", "chat")
    named = identity(call, name="Pip")
    assert named["identity"] == {
        "name": "Pip",
        "name_confirmed": False,
        "met_user": False,
        "offer_made": False,
    }
    again = identity(call, name="Rex")
    assert again["ok"] is False and "already named" in str(again)
    assert identity(call)["identity"]["name"] == "Pip"
    renamed = identity(call, name="Rex", cites=transcript("c1", "user", "Call yourself Rex"))
    assert renamed["identity"]["name"] == "Rex" and renamed["identity"]["name_confirmed"]
    locked = identity(call, name="Ace", cites=transcript("c1", "user", "No, Ace"))
    assert locked["ok"] is False and "cannot change" in str(locked)
    met = identity(call, met_user=True, cites=transcript("c1", "user", "I am Sam"))
    assert met["identity"]["met_user"] is True
    researcher = call("workspace", {"action": "identity", "name": "Zed"}, role="researcher")
    assert researcher["ok"] is False


def test_the_settings_tool_cannot_write_the_name(call):
    refused = call(
        "settings", {"action": "update", "key": "identity.name", "value": "Rex", "cites": "m:1"}
    )
    assert refused["ok"] is False


def test_a_cite_must_be_a_stored_user_message_of_a_chat_with_a_matching_hash(
    call, register_session, transcript
):
    register_session("c1", "chat")
    register_session("w1", "wake")
    identity(call, name="Pip")
    for bad in ("setup", "C-5", "msg:99", "the user agreed"):
        for args in ({"name": "Rex"}, {"confirm": True}, {"met_user": True}):
            refused = identity(call, cites=bad, **args)
            assert refused["ok"] is False, (bad, args)
    assert identity(call, confirm=True)["ok"] is False
    good = transcript("c1", "user", "Keep Pip")
    digest = good.split("#")[1]
    cases = {
        "was not found": f"session:c1/999#{digest}",
        "not the user's": transcript("c1", "assistant", "I am Pip"),
        "does not match": good.split("#")[0] + "#" + "0" * 64,
        "cite a message from this conversation": transcript("elsewhere", "user", "Keep Pip"),
    }
    for reason, cite in cases.items():
        refused = identity(call, confirm=True, cites=cite)
        assert refused["ok"] is False and reason in str(refused), reason
    state = identity(call)["identity"]
    assert state["name"] == "Pip" and not state["name_confirmed"]
    kept = identity(call, confirm=True, cites=good)
    assert kept["identity"]["name_confirmed"] is True


def test_a_cite_from_a_task_session_is_refused(call, register_session, transcript):
    register_session("t1", "task", task_id=1)
    identity(call, "t1", name="Pip")
    forged = transcript("t1", "user", "Keep Pip")
    refused = identity(call, "t1", confirm=True, cites=forged)
    assert refused["ok"] is False and "identity writes are allowed only" in str(refused)
    assert identity(call, "t1")["identity"]["name_confirmed"] is False


def test_a_cite_from_another_chat_of_the_workspace_is_refused(call, register_session, transcript):
    register_session("c1", "chat")
    register_session("c2", "chat")
    identity(call, name="Pip")
    other = transcript("c2", "user", "Keep Pip")
    refused = identity(call, "c1", confirm=True, cites=other)
    assert refused["ok"] is False and "this conversation" in str(refused)
    assert identity(call)["identity"]["name_confirmed"] is False


def test_a_cite_from_the_calling_chat_is_accepted(call, register_session, transcript):
    register_session("c1", "chat")
    register_session("c2", "chat")
    identity(call, name="Pip")
    kept = identity(call, "c2", confirm=True, cites=transcript("c2", "user", "Keep Pip"))
    assert kept["identity"]["name_confirmed"] is True


@pytest.mark.parametrize(
    "client",
    [
        {"id": "orb-minecraft", "kind": "web"},
        {"id": "orb-minecraft", "kind": "game"},
        {"id": "aide", "kind": "runtime"},
        {"id": "unknown", "kind": "unknown"},
        None,
    ],
    ids=["runtime-web", "runtime-game", "other-kind", "unknown-client", "no-client"],
)
def test_identity_writes_from_a_game_or_unknown_client_are_refused(
    call, register_session, transcript, client
):
    register_session("c1", "chat")
    identity(call, name="Pip")
    register_session("g1", "chat", client=client)
    # A stranger's in-game line is a user-role row of the calling session, with a valid hash.
    stranger = transcript("g1", "user", "Your name is Kimi")
    for args in (
        {"name": "Kimi", "cites": stranger},
        {"confirm": True, "cites": stranger},
        {"met_user": True, "cites": stranger},
        {"offer_made": True},
        {"name": "Kimi"},
    ):
        refused = identity(call, "g1", **args)
        assert refused["ok"] is False and "identity writes are allowed only" in str(refused), args
    assert identity(call)["identity"]["name"] == "Pip"
    assert identity(call)["identity"]["name_confirmed"] is False
    # Reads still work there.
    assert identity(call, "g1")["identity"]["name"] == "Pip"


def test_verify_cite_refuses_a_game_chat_even_without_the_tool_gate(
    call, register_session, transcript, ctx_factory
):
    from research_core.store import identity as identity_store
    from research_core.store.errors import StoreError

    register_session("g1", "chat", client={"id": "orb-minecraft", "kind": "web"})
    cite = transcript("g1", "user", "Your name is Kimi")
    with pytest.raises(StoreError, match="not opened by the user's IDE chat"):
        identity_store.verify_cite(ctx_factory().wstore, cite, None, "x", calling_session="g1")


def test_the_client_that_opened_a_session_sticks_on_reregistration(register_session, ctx_factory):
    register_session("c1", "chat", client={"id": "orb-minecraft", "kind": "web"})
    again = register_session("c1", "chat", client={"id": "aide", "kind": "ide"})
    assert again["client"] == {"id": "orb-minecraft", "kind": "web"}


def test_a_cite_to_a_message_that_is_not_in_state_db_is_refused(call, register_session):
    register_session("c1", "chat")
    refused = identity(call, name="Rex", cites=f"session:c1/1#{'a' * 64}")
    assert refused["ok"] is False and "cannot verify" in str(refused)


def test_wakes_and_unknown_sessions_write_nothing_but_the_first_name(
    call, register_session, transcript
):
    register_session("w1", "wake")
    cite = transcript("w1", "user", "hi")
    state = identity(call, "w1")["identity"]
    assert state["name"] is None
    named = identity(call, "w1", name="Pip")
    assert named["identity"]["name"] == "Pip"
    for args in (
        {"name": "Rex", "cites": cite},
        {"confirm": True, "cites": cite},
        {"met_user": True, "cites": cite},
        {"offer_made": True},
    ):
        woke = identity(call, "w1", **args)
        assert woke["ok"] is False and "no user is present in a wake" in str(woke), args
    state = identity(call)["identity"]
    assert state == {
        "name": "Pip",
        "name_confirmed": False,
        "met_user": False,
        "offer_made": False,
    }
    unknown = identity(call, "nobody", offer_made=True)
    assert unknown["ok"] is False and "not registered" in str(unknown)
    assert identity(call, "nobody", name="Zed")["ok"] is False
    assert identity(call, "nobody")["identity"]["name"] == "Pip"


def test_a_nameless_existing_workspace_is_named_by_a_user_cited_call_then_locked(
    call, register_session, transcript
):
    register_session("c1", "chat")
    assert identity(call)["identity"]["name"] is None
    named = identity(call, name="Pip", cites=transcript("c1", "user", "Be Pip"))
    assert named["identity"]["name"] == "Pip" and named["identity"]["name_confirmed"]
    again = identity(call, name="Rex", cites=transcript("c1", "user", "Be Rex"))
    assert again["ok"] is False and "cannot change" in str(again)


def test_the_rename_offer_is_recorded_once_in_a_chat(call, register_session):
    register_session("c1", "chat")
    identity(call, name="Pip")
    offered = identity(call, offer_made=True)
    assert offered["identity"]["offer_made"] is True and offered["identity"]["name"] == "Pip"
    refused = identity(call, name="Rex")
    assert refused["ok"] is False and "name and cites" in str(refused)
