"""Step 9: tool checks run when due, mark capabilities, open one repair Task per broken
capability, link the Tasks that need it, and notify after two failed repairs (design B.15)."""

from __future__ import annotations

import json

import pytest

from research_core import coworld_setup, gitops
from research_core.platform.fake import FakePlatform
from research_core.store import capabilities, coworlds, notices, sessions, tasks
from research_core.store.actors import Actor
from research_core.watcher import checks
from tests.watcher._support import (
    advance,
    claim,
    events_of,
    make_ctx,
    make_task,
    register_session,
    task_number,
)
from tests.watcher._support_w3 import platform_with_faults, setup_code_repo

FAILURE = {"what_failed": "the fix did not hold", "next_step": "ask the user"}


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(checks, "_sleep", lambda _s: None)


def fake_ctx(tmp_path, **env):
    return make_ctx(tmp_path, env={"IDE_PLATFORM": "fake", **env})


def last_event(store, kind):
    return [e for e in store.events_after(0, 10_000) if e.kind == kind][-1]


def break_check_command(ctx) -> None:
    path = ctx.coworld.code / "mixin" / "MIXIN.md"
    text = path.read_text()
    assert "  check: python3 mixin/tools/check.py\n" in text
    path.write_text(
        text.replace(
            "  check: python3 mixin/tools/check.py\n",
            "  check: sh -c 'echo the checker is broken >&2; exit 3'\n",
        )
    )
    gitops.commit_all(ctx.coworld.code, "Break the check command")


def test_full_run_marks_capabilities_and_sets_the_coworld_up(tmp_path):
    ctx = fake_ctx(tmp_path)
    setup_code_repo(ctx)

    report = checks.run(ctx)

    assert report["mode"] == "full" and report["broken"] == []
    assert set(report["ok"]) >= {"login:player", "cli:coworld", "cli:softmax", "build", "check"}
    assert "upload" in report["ok"] and "credits" in report["ok"]
    assert report["skipped"] == ["evaluate"]  # no downloaded results yet
    status = {row["name"]: row["status"] for row in capabilities.status(ctx.store)}
    assert status == {
        "build": "ok",
        "check": "ok",
        "cli:coworld": "ok",
        "cli:research": "ok",
        "cli:softmax": "ok",
        "login:player": "ok",
        "upload": "ok",
    }
    row = coworlds.read(ctx.wstore, "cow_fake")
    assert row["set_up_at"] and row["mixin_version"] == "1" and row["tool_check_status"] == "ok"
    assert len(events_of(ctx.wstore, "coworld.set_up")) == 1
    store = ctx.store
    assert store.get_meta("checks_at") == store.get_meta("checks_quick_at")
    assert store.get_meta("checks_version") == checks.research_core.__version__
    assert json.loads(store.get_meta("credits"))["balance"] == 100
    versions = json.loads(ctx.wstore.get_meta("cli_versions"))
    assert versions["coworld"]["installed"] and versions["softmax"]["installed"]
    assert "lock_refreshed" in versions
    uploaded = [p for p in ctx.platform.snapshot()["policies"].values()]
    assert [p["name"] for p in uploaded] == ["cow_fake-tool-check-fake"]  # Player ply_fake
    [ran] = events_of(store, "checks.ran")
    assert ran["payload"]["mode"] == "full" and ran["payload"]["skipped"] == {
        "evaluate": "no results to evaluate"
    }
    assert not any(
        w["path"].name == checks.CHECK_WORKTREE for w in gitops.list_worktrees(ctx.coworld.code)
    )
    assert gitops.is_clean(ctx.coworld.code)
    assert checks.run(ctx)["skipped"] == "not due"


def test_quick_runs_hourly_or_after_a_session_starts_and_full_daily(tmp_path):
    ctx = fake_ctx(tmp_path)
    setup_code_repo(ctx)
    checks.run(ctx)
    advance(ctx, 1800)
    assert checks.due_mode(ctx) is None
    register_session(ctx, "api_1761690000_cccc0001", make_task(ctx))  # session.started
    assert checks.due_mode(ctx) == "quick"
    report = checks.run(ctx)
    assert report["mode"] == "quick"
    assert "build" not in report["ok"] and "login:player" in report["ok"]
    assert checks.due_mode(ctx) is None
    advance(ctx, 3600)
    assert checks.due_mode(ctx) == "quick"
    advance(ctx, 24 * 3600)
    assert checks.due_mode(ctx) == "full"


def test_marked_due_or_a_new_release_forces_a_full_run(tmp_path):
    ctx = fake_ctx(tmp_path)
    setup_code_repo(ctx)
    checks.run(ctx)
    ctx.store.set_meta("checks_due", ctx.store.now())  # workspace.capabilities {check: true}
    assert checks.due_mode(ctx) == "full"
    checks.run(ctx)
    assert ctx.store.get_meta("checks_due") == ""
    assert checks.due_mode(ctx) is None
    ctx.store.set_meta("checks_version", "0.0.0")
    assert checks.due_mode(ctx) == "full"


def test_failing_check_creates_one_repair_task_and_a_second_failure_joins_it(tmp_path):
    ctx = fake_ctx(tmp_path)
    setup_code_repo(ctx)
    break_check_command(ctx)

    report = checks.run(ctx)

    assert report["broken"] == ["check"]
    repairs = tasks.list(ctx.store, type="maintenance")
    assert len(repairs) == 1
    [repair] = repairs
    assert repair["capability"] == "check" and repair["priority"] == "high"
    assert repair["status"] == "unclaimed" and "the checker is broken" in repair["description"]
    record = capabilities.read(ctx.store, "check")
    assert record["status"] == "broken" and record["maintenance_task_id"] == task_number(repair)
    assert "exit code 3" in record["output"]
    assert coworlds.read(ctx.wstore, "cow_fake")["set_up_at"] is None  # B.14: checks must pass

    ctx.store.set_meta("checks_due", "1")
    assert checks.run(ctx)["broken"] == ["check"]
    assert len(tasks.list(ctx.store, type="maintenance")) == 1  # the repeat joined it
    assert len(events_of(ctx.store, "capability.failed")) == 1


def test_dependents_gain_the_dependency_on_the_repair_task(tmp_path):
    ctx = fake_ctx(tmp_path)
    improvement = make_task(ctx, type="improvement", title="Tune aggression")
    research = make_task(ctx, type="research", title="Read the replays")
    report = capabilities.report_failure(
        ctx.store, ctx.actor, "check", output="boom", command="python3 mixin/tools/check.py"
    )
    repair = report["maintenance_task"]["id"]

    result = checks.handle_capability_failed(
        ctx, last_event(ctx.store, "capability.failed"), ctx.store
    )

    assert result == {"capability": "check", "repair": repair, "linked": [improvement["id"]]}
    assert tasks.read(ctx.store, task_number(improvement))["depends_on"] == [repair]
    assert tasks.read(ctx.store, task_number(research))["depends_on"] == []
    assert tasks.read(ctx.store, task_number(repair))["depends_on"] == []
    assert [t["id"] for t in tasks.dispatchable(ctx.store)] == [repair, research["id"]]
    again = checks.handle_capability_failed(
        ctx, last_event(ctx.store, "capability.failed"), ctx.store
    )
    assert again["linked"] == []  # idempotent


def test_research_tasks_depend_on_evaluate_only(tmp_path):
    ctx = fake_ctx(tmp_path)
    research = make_task(ctx, type="research", title="Read the replays")
    report = capabilities.report_failure(ctx.store, ctx.actor, "evaluate", output="x", command="e")
    checks.handle_capability_failed(ctx, last_event(ctx.store, "capability.failed"), ctx.store)
    assert tasks.read(ctx.store, task_number(research))["depends_on"] == [
        report["maintenance_task"]["id"]
    ]


def test_repair_failing_twice_raises_a_notice(tmp_path):
    ctx = fake_ctx(tmp_path)
    setup_code_repo(ctx)
    for attempt in (1, 2):
        report = capabilities.report_failure(
            ctx.store, ctx.actor, "check", output="boom", command="python3 mixin/tools/check.py"
        )
        repair = report["maintenance_task"]
        assert report["maintenance_task_created"]
        session_id = f"api_1761690000_dddd000{attempt}"
        register_session(ctx, session_id, repair)
        claim(ctx, session_id, repair)
        tasks.complete(
            ctx.store,
            Actor.session(session_id),
            task_number(repair),
            outcome="failed",
            failure=FAILURE,
        )
        result = checks.handle_task_failed(ctx, last_event(ctx.store, "task.failed"), ctx.store)
        assert result["failures"] == attempt
        assert len(events_of(ctx.store, "repair.failed_twice")) == (attempt == 2)
    event = last_event(ctx.store, "repair.failed_twice")
    assert event.payload["capability"] == "check" and event.payload["failures"] == 2

    checks.handle_repair_failed_twice(ctx, event, ctx.store)

    [notice] = notices.unread(ctx.wstore, coworld_id="cow_fake")
    assert "check capability has failed twice" in notice["text"]
    assert notice["refs"] == [event.payload["task"]] and notice["urgency"] == "high"

    checks.run(ctx)  # the real check passes: the counter resets and the capability is ok
    assert ctx.store.get_meta("repair_failures:check") == "0"
    assert capabilities.read(ctx.store, "check")["status"] == "ok"
    assert len(events_of(ctx.store, "capability.repaired")) == 1


def test_task_failed_for_an_ordinary_task_is_not_counted(tmp_path):
    ctx = fake_ctx(tmp_path)
    task = make_task(ctx)
    register_session(ctx, "api_1761690000_eeee0001", task)
    claim(ctx, "api_1761690000_eeee0001", task)
    tasks.complete(
        ctx.store,
        Actor.session("api_1761690000_eeee0001"),
        task_number(task),
        outcome="failed",
        failure=FAILURE,
    )
    result = checks.handle_task_failed(ctx, last_event(ctx.store, "task.failed"), ctx.store)
    assert result == {"review": task["id"]}
    review = last_event(ctx.store, "task.failure_review")
    assert review.payload["what_failed"] == FAILURE["what_failed"]
    assert review.payload["next_step"] == FAILURE["next_step"]


def test_a_task_the_watcher_gave_up_on_wakes_the_orchestrator_once(tmp_path):
    ctx = fake_ctx(tmp_path)
    task = make_task(ctx)
    register_session(ctx, "api_1761690000_eeee0002", task)
    claim(ctx, "api_1761690000_eeee0002", task)
    tasks.complete(
        ctx.store,
        Actor.session("api_1761690000_eeee0002"),
        task_number(task),
        outcome="failed",
        failure=FAILURE,
    )
    with ctx.wstore.tx(ctx.actor) as tx:
        tx.event("recovery.failed", task["id"], {"coworld_id": ctx.coworld.coworld_id})
    result = checks.handle_task_failed(ctx, last_event(ctx.store, "task.failed"), ctx.store)
    assert "skipped" in result
    assert events_of(ctx.store, "task.failure_review") == []


def test_the_login_check_keeps_the_player_id_for_the_briefing(tmp_path):
    ctx = fake_ctx(tmp_path)
    checks.login_checks(ctx)
    assert ctx.wstore.get_meta("player_id") == "ply_fake"


def test_platform_down_breaks_the_login_and_leaves_the_coworld_unset(tmp_path):
    ctx = fake_ctx(tmp_path)
    setup_code_repo(ctx)
    platform_with_faults(ctx, "platform=down")

    report = checks.run(ctx)

    assert set(report["broken"]) == {"login:player", "upload"}
    assert report["unavailable"] == ["credits"]  # a fact the briefing lacks, not a capability
    assert "build" in report["ok"] and "check" in report["ok"]
    repairs = {t["capability"] for t in tasks.list(ctx.store, type="maintenance")}
    assert repairs == {"login:player", "upload"}
    assert coworlds.read(ctx.wstore, "cow_fake")["set_up_at"] is None
    assert ctx.store.get_meta("credits") is None
    assert ctx.wstore.get_meta("player_id") is None
    login = capabilities.read(ctx.store, "login:player")
    assert "status 503" in login["output"] and "GET /whoami" in login["command"]


def test_without_a_mixin_the_command_checks_are_skipped(tmp_path):
    ctx = fake_ctx(tmp_path)
    report = checks.run(ctx)
    assert report["mode"] == "full" and report["skipped"] == ["mixin"]
    assert capabilities.read(ctx.store, "build") is None
    assert coworlds.read(ctx.wstore, "cow_fake")["set_up_at"] is None


def test_user_login_is_checked_only_when_provisioned(tmp_path):
    ctx = fake_ctx(tmp_path)
    assert [r["name"] for r in checks.login_checks(ctx)] == ["login:player"]
    ctx.env = {**ctx.env, "SOFTMAX_USER_CREDENTIAL": "user-token"}
    ctx.platform = FakePlatform(ctx.platform.state_path, env=ctx.env, clock=ctx.platform._clock)
    results = {r["name"]: r for r in checks.login_checks(ctx)}
    assert results["login:user"]["ok"] is True and "usr_fake" in results["login:user"]["output"]


def test_real_platform_skips_the_upload_once_verified(tmp_path):
    ctx = make_ctx(tmp_path)  # IDE_PLATFORM unset: "real" rules, on the fake object
    setup_code_repo(ctx)
    first = {r["name"]: r for r in checks.run_all(ctx)}
    assert first["upload"]["ok"] is True  # never verified before: the dry run is made
    second = {r["name"]: r for r in checks.run_all(ctx)}
    assert second["upload"]["ok"] is None
    assert second["upload"]["note"].startswith("skipped: real upload not attempted")
    assert capabilities.read(ctx.store, "upload")["status"] == "ok"
    assert len(ctx.platform.snapshot()["policies"]) == 1
    sessions.read(ctx.wstore, "nobody")  # the workspace store is untouched by the checks


def test_a_full_check_installs_the_mixin_toolchains(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    setup_code_repo(ctx)
    declared = []

    def install(workspace, toolchains):
        declared.append(toolchains)
        return {"toolchains": [], "problems": []}

    monkeypatch.setattr(coworld_setup, "install_toolchains", install)

    checks.mixin_checks(ctx)

    assert len(declared) == 1


def test_the_upload_probe_name_is_this_players_own():
    """Policy names are global; a bare ``<coworld>-tool-check`` is soon another account's (409)."""
    player = "ply_f39295d0-ac38-4a62-ac4f-e1d62d7dc9d1"
    assert checks.upload_probe_name("cow_x", player) == "cow_x-tool-check-f39295d0ac38"
    other = checks.upload_probe_name("cow_x", "ply_0b8c1d2e-0000-4000-8000-000000000000")
    assert other != checks.upload_probe_name("cow_x", player)
    assert checks.upload_probe_name("cow_x", None) == "cow_x-tool-check"
