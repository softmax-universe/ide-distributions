from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from research_core.plugin import session_context, turn
from research_core.store import coworlds, notices, sessions
from research_core.store.actors import Actor
from research_core.tools.base import ToolContext

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def make_ctx(tmp_path, session_id="chat1", coworld=True):
    ctx = ToolContext.from_env(
        "orchestrator",
        {
            "IDE_WORKSPACE": str(tmp_path),
            "IDE_SESSION_ID": session_id,
            "IDE_COWORLD_ID": "cow_fake" if coworld else "",
        },
    )
    ctx.workspace.ensure_dirs()
    if coworld:
        harness = Actor.parse("harness")
        coworlds.register(ctx.wstore, harness, "cow_fake", folder="cow_fake")
        coworlds.activate(ctx.wstore, harness, "cow_fake")
        ctx.workspace.coworld("cow_fake").ensure_dirs()
    return ctx


def test_context_block_bad_input_never_crashes():
    assert turn.render_context_block({}) is None
    text = turn.render_context_block(
        {
            "contract_version": 1,
            "mode": {"name": "research", "instruction": "brief"},
            "client_data": {"z": "q" * 9000},
            "bogus": 1,
            "on_screen": ["G-1"],
        }
    )
    assert "mode: research — brief" in text
    assert "on screen: G-1" in text
    assert "ignored:" in text and "client_data" in text and "bogus" in text


def test_notices_appear_once_and_changed_since_note(tmp_path):
    ctx = make_ctx(tmp_path)
    harness = Actor.parse("harness")
    notices.add(
        ctx.wstore, harness, text="Submitted V-1 to the league", refs=["V-1"], coworld_id="cow_fake"
    )
    first = turn.render_turn(ctx, "chat1", is_first_turn=True, now=NOW)
    assert "[notices since your last turn]" in first and "Submitted V-1" in first
    assert "[session Coworld] cow_fake" in first
    assert "[changed since" not in first  # no earlier turn to compare with

    with ctx.store.tx(harness) as tx:
        tx.event("task.completed", "T-1", {})
        tx.event("task.completed", "T-2", {})
    second = turn.render_turn(ctx, "chat1", is_first_turn=False, now=NOW)
    assert "[notices" not in second
    assert "[changed since your last turn] task.completed ×2" in second


def test_away_summary_only_after_an_hour_on_a_new_chat(tmp_path):
    ctx = make_ctx(tmp_path)
    harness = Actor.parse("harness")
    turn.render_turn(ctx, "old-chat", is_first_turn=True, now=NOW)
    with ctx.store.tx(harness) as tx:
        tx.event("version.submitted", "V-3", {})
    fresh = turn.render_turn(ctx, "new-chat", is_first_turn=True, now=NOW + timedelta(minutes=10))
    assert "while you were away" not in fresh
    with ctx.store.tx(harness) as tx:
        tx.event("version.submitted", "V-4", {})
    # Events carry the real clock, so the recorded last turn is "now"; two hours later:
    later = turn.render_turn(
        ctx, "newer-chat", is_first_turn=True, now=datetime.now(UTC) + timedelta(hours=2)
    )
    assert "while you were away" in later and "Submissions: 1" in later


def test_pre_llm_call_only_for_orchestrator_chat_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    ctx = make_ctx(tmp_path)
    sessions.register(
        ctx.wstore, Actor.parse("harness"), session_id="wake1", profile="orchestrator", kind="wake"
    )
    hook = turn.make_pre_llm_call("orchestrator")
    assert hook(session_id="wake1", user_message="x", is_first_turn=True) is None
    sessions.register(
        ctx.wstore,
        Actor.harness(),
        session_id="chat9",
        profile="orchestrator",
        kind="chat",
        coworld_id="cow_fake",
    )
    result = hook(session_id="chat9", user_message="hello", is_first_turn=True)
    assert "[session Coworld] cow_fake" in result["context"]
    assert sessions.read(ctx.wstore, "chat9")["kind"] == "chat"
    assert turn.make_pre_llm_call("researcher")(session_id="chat9", user_message="x") is None


def test_pre_llm_call_reports_each_source_it_adds(tmp_path, monkeypatch):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    ctx = make_ctx(tmp_path)
    sessions.register(
        ctx.wstore,
        Actor.harness(),
        session_id="chat9",
        profile="orchestrator",
        kind="chat",
        coworld_id="cow_fake",
    )
    notices.add(ctx.wstore, Actor.parse("harness"), text="a notice", coworld_id="cow_fake")
    heard = []
    hook = turn.make_pre_llm_call(
        "orchestrator", lambda session, typed, parts: heard.append((session, typed, parts))
    )
    result = hook(session_id="chat9", user_message="hello", is_first_turn=True)
    ((session, typed, parts),) = heard
    assert (session, typed) == ("chat9", "hello")
    assert [source for source, _text in parts] == ["notices", "session_coworld"]
    # What telemetry hears is exactly what joins the message.
    assert "\n\n".join(text for _source, text in parts) == result["context"]


def test_session_context_section(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, coworld=False)
    sessions.register(
        ctx.wstore,
        Actor.parse("harness"),
        session_id="chat1",
        profile="orchestrator",
        kind="chat",
        session_context={"mode": {"name": "research"}},
        client_capabilities={"views": ["replay"]},
    )
    text = session_context.render(ctx, "chat1")
    assert text.startswith("[session context]") and "outrank" in text
    assert '"research"' in text and '"replay"' in text and len(text) <= 3800
    none = session_context.render(ctx, "unknown")
    assert "No session context" in none and "No client capabilities" in none
    monkeypatch.setattr("research_core.plugin.executor.profile_env", lambda **_: ctx.env)
    # Hermes passes the session info as a read-only mapping, not a dict.
    section = session_context.make_section("researcher")(
        MappingProxyType({"session_id": "chat1", "model": "m"})
    )
    assert '"research"' in section and "No session context" not in section


def test_only_a_chat_is_told_to_ask_in_chat(tmp_path):
    from research_core.store import sessions

    ctx = make_ctx(tmp_path)
    for session_id, kind in (("chat2", "chat"), ("wake1", "wake")):
        sessions.register(
            ctx.wstore,
            Actor.parse("harness"),
            session_id=session_id,
            profile="orchestrator",
            kind=kind,
        )
    assert "ask in chat" in session_context.render(ctx, "chat2")
    wake = session_context.render(ctx, "wake1")
    assert "ask in chat" not in wake and "nobody reads" in wake and "ui.tell" in wake
    assert "No session context" not in wake
    assert f"Work only in {ctx.coworld().root}." in wake
    researcher = ToolContext.from_env("researcher", ctx.env)
    text = session_context.render(researcher, "unknown")
    assert "ui." not in text and "task_ask" in text


def test_reactions_reach_the_agent_at_its_next_turn(tmp_path):
    from research_core.store import ui_state

    ctx = make_ctx(tmp_path)
    turn.render_turn(ctx, "chat1", is_first_turn=True, now=NOW)
    user = Actor.user("u1")
    ui_state.patch(
        ctx.wstore,
        user,
        "chat1",
        [
            {"op": "add", "path": "/reactions", "value": {}},
            {"op": "add", "path": "/reactions/m-7", "value": {"value": "👍", "by": "user:u1"}},
        ],
    )
    ui_state.patch(
        ctx.wstore,
        user,
        "other-chat",
        [
            {"op": "add", "path": "/reactions", "value": {}},
            {"op": "add", "path": "/reactions/m-9", "value": {"value": "👎", "by": "user:u1"}},
        ],
    )
    text = turn.render_turn(ctx, "chat1", is_first_turn=False, now=NOW)
    assert "[reactions since your last turn]\n- message m-7: 👍" in text
    assert "m-9" not in text  # another conversation's reaction
    assert "ui.patch" not in text  # not counted as a changed-since kind
    assert "[reactions" not in turn.render_turn(ctx, "chat1", is_first_turn=False, now=NOW)


def test_an_opening_goal_is_said_to_be_the_first_request():
    block = turn.render_context_block({"opening_goal": "Reach the top 10"})
    assert "opening_goal: Reach the top 10" in block and session_context.OPENING_GOAL in block
    assert session_context.OPENING_GOAL not in turn.render_context_block({"league": "league_x"})


def test_context_block_prints_the_guide_and_the_agent_profile():
    text = turn.render_context_block(
        {
            "contract_version": 1,
            "client": {"id": "aide", "kind": "ide", "guide": "client-web-ide"},
            "agent_profile": {"name": "Ada", "focus": "openings"},
        }
    )
    assert "(guide skill: client-web-ide)" in text
    assert "agent profile:" in text and "Ada" in text
