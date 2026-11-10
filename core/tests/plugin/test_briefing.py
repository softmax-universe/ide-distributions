"""The briefing section: budget, priorities, directives, the Session's Task, no store path."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from research_core.plugin import briefing
from research_core.store import (
    capabilities,
    champion,
    claims,
    coworlds,
    directives,
    experiments,
    goals,
    sessions,
    settings,
    tasks,
    versions,
)
from research_core.store._common import now_iso
from research_core.store.actors import Actor
from research_core.tools.base import ToolContext

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
HARNESS = Actor.parse("harness")
PVID = "7b1c0f4e-2f1a-4c0d-9c3e-1a2b3c4d5e6f"
CODE = {"type": "code", "ref": "policy/agent.py:10-20@0123abcd", "stance": "supports", "note": "n"}
ARTIFACT = {"kind": "file", "path": "dist/policy.bin", "digest": "sha256:" + "0" * 64, "size": 2048}
HYPOTHESIS = {
    "statement": "Retreating at 30% health loses fewer units because fights end earlier",
    "if_true": "the candidate's win rate rises by at least two points",
    "if_false": "the win rate stays within noise of the baseline",
    "motivation": "C-1 says early retreats save units; nothing has measured the win-rate effect",
}
METHODS = {
    "baseline": "V-1",
    "candidate": None,
    "procedure": "Build the retreat change on the task branch; run 40 paired episodes per arm",
    "metrics": [
        {"name": "win_rate", "direction": "up", "role": "primary", "min_effect": 0.02},
        {"name": "step_ms", "direction": "down", "role": "guardrail", "tolerance": 5},
    ],
    "decision_rule": {
        "text": "confirmed when win_rate rises by at least 0.02 with the interval above zero",
        "n_min": 40,
        "k": 0.5,
    },
}


def make_ctx(tmp_path, role="orchestrator", session_id="chat1", coworld=True, **env):
    ctx = ToolContext.from_env(
        role,
        {
            "IDE_WORKSPACE": str(tmp_path),
            "IDE_SESSION_ID": session_id,
            "IDE_COWORLD_ID": "cow_fake" if coworld else "",
            **env,
        },
    )
    ctx.workspace.ensure_dirs()
    if coworld:
        coworlds.register(ctx.wstore, HARNESS, "cow_fake", folder="cow_fake")
        coworlds.activate(ctx.wstore, HARNESS, "cow_fake")
        ctx.workspace.coworld("cow_fake").ensure_dirs()
    return ctx


def make_goal(store, n):
    return goals.create(
        store,
        HARNESS,
        title=f"Goal {n}: reach division {n}",
        end_state=f"the champion sits in division {n}",
        reason="the user asked",
    )


def make_claim(store, n, tags):
    return claims.write(
        store,
        HARNESS,
        summary=f"Finding {n}: scouts first wins more often than a greedy opening.",
        body=f"Detail {n}: 12 of 20 episodes on 2026-10-28; a losing streak changes our mind.",
        tags=tags,
        evidence=[CODE],
    )


def make_version(store):
    return versions.create(
        store,
        HARNESS,
        summary="tuned opening",
        commit_sha="0123abc4567def890123abc4567def8901234567",
        branch="main",
        artifact=ARTIFACT,
        based_on=[],
    )


def assert_no_store_path(text, tmp_path):
    assert ".state" not in text
    assert "store.db" not in text
    assert str(tmp_path) not in text


def test_a_preference_kept_as_a_workspace_directive_shows_with_or_without_a_coworld(tmp_path):
    ctx = make_ctx(tmp_path, coworld=False)
    directives.add(
        ctx.wstore, HARNESS, text="Keep your replies short", scope="workspace", source="msg:1"
    )
    expected = 'W-1 (workspace): "Keep your replies short"'
    assert ctx.coworld_or_none() is None
    assert expected in briefing.render(ctx, "chat1", role="orchestrator", now=NOW)

    bound = make_ctx(tmp_path)
    assert bound.coworld_or_none() is not None
    assert expected in briefing.render(bound, "chat1", role="orchestrator", now=NOW)


def test_the_briefing_does_not_ask_for_preference_claims(tmp_path):
    ctx = make_ctx(tmp_path)
    text = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "preference" not in text.lower()


def test_existing_preference_claims_still_show_and_yield_to_directives(tmp_path):
    ctx = make_ctx(tmp_path)
    make_claim(ctx.store, 1, ["preference"])
    text = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "C-1: Finding 1" in text
    assert "directives and settings override" in text


def test_overflow_cuts_the_lowest_priority_list_first_and_keeps_the_budget(tmp_path):
    ctx = make_ctx(tmp_path)
    for n in range(1, 51):
        make_goal(ctx.store, n)
    for n in range(1, 101):
        make_claim(ctx.store, n, ["preference", "style"])
    for n in range(101, 201):
        make_claim(ctx.store, n, [f"G-{(n % 50) + 1}", "opening"])

    text = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert text.startswith("[briefing]")
    assert len(text) <= 3600
    assert "more, search with claim.search" in text
    assert "Active Goals (50):" in text
    assert "Workspace: Coworld cow_fake" in text
    assert "[+ truncated]" not in text
    assert_no_store_path(text, tmp_path)

    researcher = briefing.render(ctx, "chat1", role="researcher", now=NOW)
    assert len(researcher) <= 3800
    assert "User identity" not in researcher


def test_a_directive_added_after_the_first_render_appears_in_the_next(tmp_path):
    ctx = make_ctx(tmp_path)
    first = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "Directives: none active" in first
    directives.add(
        ctx.wstore, HARNESS, text="Never submit on a Friday", scope="workspace", source="msg:1"
    )
    make_goal(ctx.store, 1)
    directives.add(
        ctx.store, HARNESS, text="Keep XP runs under 50 episodes", scope="goal:1", source="msg:2"
    )
    sessions.register(
        ctx.wstore,
        HARNESS,
        session_id="chat1",
        profile="orchestrator",
        kind="chat",
        goal_id=1,
        coworld_id="cow_fake",
    )
    second = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert 'W-1 (workspace): "Never submit on a Friday"' in second
    assert 'D-1 (G-1): "Keep XP runs under 50 episodes"' in second


def test_a_task_session_shows_its_task_experiment_and_pending_messages(tmp_path):
    ctx = make_ctx(tmp_path, role="researcher", session_id="task-run-1")
    make_goal(ctx.store, 1)
    make_version(ctx.store)
    tasks.create(
        ctx.store,
        HARNESS,
        type="improvement",
        title="Tune the opening",
        description="Try scouts first",
        goal_id=1,
    )
    experiments.create(
        ctx.store,
        HARNESS,
        summary="Does retreating at 30% health raise the win rate?",
        hypothesis=HYPOTHESIS,
        methods=METHODS,
        goal_id=1,
        task_id=1,
    )
    sessions.register(
        ctx.wstore,
        HARNESS,
        session_id="task-run-1",
        profile="researcher",
        kind="task",
        coworld_id="cow_fake",
        task_id=1,
        goal_id=1,
    )
    tasks.message(ctx.store, HARNESS, 1, text="Use seeds 1-40 only", direction="to_researcher")

    text = briefing.render(ctx, "task-run-1", role="researcher", now=NOW)
    assert 'Your Task: T-1 "Tune the opening"' in text
    assert "improvement for G-1, unclaimed" in text
    assert 'Experiment: X-1 "Does retreating at 30% health raise the win rate?" (proposed)' in text
    assert 'Messages for you (1 undelivered):\n"Use seeds 1-40 only"' in text
    assert_no_store_path(text, tmp_path)

    chat = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "Your Task" not in chat


def test_workspace_status_capabilities_champion_and_window(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.wstore.set_meta("heartbeat", now_iso(lambda: NOW - timedelta(minutes=3)))
    ctx.wstore.set_meta("has_work", "true")
    ctx.wstore.set_meta(
        "cli_versions",
        json.dumps(
            {
                "coworld": {"installed": "1.2.0", "lock": "1.3.0"},
                "softmax-cli": {"installed": "0.9.0", "lock": "0.9.0"},
                "lock_refreshed": "2026-10-27",
            }
        ),
    )
    ctx.store.set_meta("checks_at", now_iso(lambda: NOW - timedelta(hours=2)))
    ctx.store.set_meta("credits", json.dumps({"balance": 120}))
    ctx.store.set_meta(
        "league_versions", "The workspace is on g version 0 (cow_0); its leagues run 1"
    )
    capabilities.mark(ctx.store, HARNESS, "upload", status="ok", output="")
    capabilities.report_failure(ctx.store, HARNESS, "build", output="boom", command="build.py")
    settings.update(ctx.wstore, HARNESS, "paused", True, cites="msg:2")
    settings.update(ctx.store, HARNESS, "submissions.until", "2026-10-30T00:00:00Z", cites="m:3")
    settings.update(ctx.store, HARNESS, "submissions.max_count", 2, cites="msg:3")
    make_version(ctx.store)
    versions.mark_uploaded(
        ctx.store, HARNESS, 1, policy_version_id=PVID, at=now_iso(), digest_checked=True
    )
    champion.sync(
        ctx.store,
        HARNESS,
        league_id="league-1",
        division_id=None,
        membership_id=None,
        policy_version_id=PVID,
        state="champion",
    )

    text = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "paused: yes" in text
    assert "watcher heartbeat 3 min ago, work pending" in text
    assert "credits: 120" in text
    assert "CLI versions: coworld installed 1.2.0, lock 1.3.0; lock refreshed 2026-10-27" in text
    assert "softmax-cli" not in text
    assert "Capability check: 2 h ago; 1 of 2 ok" in text
    assert "broken: build (repair T-1)" in text
    assert "User identity: not provisioned" in text
    assert "Champion in league-1: V-1 (champion since 20" in text
    assert "The workspace is on g version 0 (cow_0); its leagues run 1" in text
    assert "V-1 uploaded" in text
    assert "Autonomous submissions: open until 2026-10-30T00:00:00Z, at most 2" in text
    assert_no_store_path(text, tmp_path)

    with_user = make_ctx(tmp_path / "other", SOFTMAX_USER_CREDENTIAL="tok")
    assert "User identity: available" in briefing.render(with_user, "c", role="orchestrator")


def test_the_player_id_comes_from_the_environment_or_the_login_check(tmp_path):
    ctx = make_ctx(tmp_path)
    assert "Your Player: id not known yet; platform.results gives it" in briefing.render(
        ctx, "chat1", now=NOW
    )
    ctx.wstore.set_meta("player_id", "ply_checked")
    assert "Your Player: id ply_checked\n" in briefing.render(ctx, "chat1", now=NOW)

    ctx = make_ctx(tmp_path, SOFTMAX_PLAYER_ID="ply_env")
    assert "Your Player: id ply_env\n" in briefing.render(ctx, "chat1", now=NOW)


def test_no_active_coworld_renders_the_workspace_status_only(tmp_path):
    ctx = make_ctx(tmp_path, coworld=False)
    text = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "Workspace: Coworld none" in text
    assert "no heartbeat yet: background work is not running" in text
    assert "credits: unknown" in text
    assert "No session Coworld" in text
    absent = ("Capability check", "Champion", "Active Goals", "Goals:", "claims", "unavailable")
    for header in absent:
        assert header not in text
    assert_no_store_path(text, tmp_path)


def test_an_item_that_raises_does_not_break_the_rest(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    make_claim(ctx.store, 1, ["preference"])
    monkeypatch.setattr(goals, "list", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    text = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "Goals: unavailable: RuntimeError" in text
    assert "Workspace: Coworld cow_fake" in text
    assert "C-1: Finding 1" in text
    assert "Traceback" not in text


def test_make_section_reads_the_session_id_and_never_raises(tmp_path, monkeypatch):
    """Hermes passes the session info as a read-only mapping, not a dict."""
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    make_ctx(tmp_path)
    section = briefing.make_section("researcher")
    info = MappingProxyType({"session_id": "chat1", "model": "m"})
    assert section(info).startswith("[briefing]")
    rendered_for = []
    monkeypatch.setattr(
        briefing, "render", lambda ctx, session_id, **_: rendered_for.append(session_id) or ""
    )
    section(info)
    assert rendered_for == ["chat1"]

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(briefing, "render", boom)
    assert section(info) == "Briefing unavailable: RuntimeError: boom"


def test_the_briefing_says_when_it_was_written_and_that_it_does_not_update(tmp_path):
    ctx = make_ctx(tmp_path)
    moment = datetime(2026, 11, 4, 18, 5, tzinfo=UTC)
    first_line = briefing.render(ctx, "chat1", role="orchestrator", now=moment).splitlines()[0]
    assert first_line.startswith("[briefing] Written 2026-11-04 18:05 UTC")
    assert "not updated in this Session" in first_line


def test_the_orchestrator_is_told_when_nothing_has_started_setup(tmp_path):
    ctx = make_ctx(tmp_path)
    due = "Call workspace.coworld_switch first"
    assert due in briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "Coworld setup" not in briefing.render(ctx, "chat1", role="researcher", now=NOW)

    with ctx.wstore.tx(HARNESS) as tx:
        tx.event("coworld.setup_needed", "cow_fake", {})
    text = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert due not in text and "Coworld setup: started, not finished" in text

    coworlds.register(ctx.wstore, HARNESS, "cow_fake", folder="cow_fake")
    coworlds.mark_set_up(ctx.wstore, HARNESS, "cow_fake", mixin_version="1", tool_check_status="ok")
    assert "Coworld setup" not in briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
