"""One owner for normal completion, missed facts, continuation and crash handoffs."""

import json
from types import SimpleNamespace

import pytest

from research_core import intake, transitions
from research_core.coordinator import Coordinator
from research_core.store import admissions, events, sessions, tasks
from research_core.store.actors import Actor
from research_core.tools.base import ToolContext, run_tool
from research_core.watcher import wakes
from research_core.watcher.hermes_api import (
    FakeHermesRuns,
    HermesRuns,
    RunsAuthError,
    RunsUnavailable,
)
from tests.watcher._support import (
    HARNESS,
    coordinator,
    dispatch_inputs,
    make_ctx,
    make_task,
    set_setting,
)


def assigned(ctx):
    """Create through the model-facing coordinator API, then the real dispatcher."""
    task = make_task(ctx, type="maintenance")

    class Command:
        env = ctx.env
        store = ctx.store
        wstore = ctx.wstore
        actor = HARNESS

        def coworld(self):
            return ctx.coworld

    receipt = Coordinator.reserve(
        Command(),
        task_id=int(task["id"][2:]),
        task_updated=task["updated"],
        profile="researcher",
        assignment_id=task["id"],
        context="Do the recorded work",
        rationale="Matches worker capabilities",
    )
    dispatch_inputs(ctx)
    return ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (receipt["op_id"],))


def complete_task(ctx, row):
    return tasks.complete(
        ctx.store,
        Actor.session(row["session_id"]),
        1,
        checklist={"summary": "fixed", "commit": "a" * 40},
        merge_request="no",
        merge_reason="no code",
        result="fixed",
    )


@pytest.mark.parametrize("source", ["notification", "reconciliation"])
def test_normal_and_recovered_completion_have_one_review_handoff(tmp_path, source):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    complete_task(ctx, row)
    owner = coordinator(ctx)
    owner.advance_events(ctx)
    assert not ctx.wstore.q("SELECT * FROM client_admissions WHERE origin='wake'")
    ctx.hermes.complete(row["run_id"])
    if source == "notification":
        owner.settle_attempt(
            ctx.wstore, row["op_id"], ctx.hermes.status(row["run_id"], profile="researcher")
        )
    else:
        owner.reconcile()
    for _ in range(3):
        owner.advance_attempts(ctx)
        owner.advance_events(ctx)
        owner.reconcile()
    [handoff] = ctx.wstore.q("SELECT * FROM client_admissions WHERE origin='wake'")
    assert handoff["status"] == "queued"
    assert sessions.read(ctx.wstore, handoff["session_id"])["event"] == "task.completed"
    assert ctx.wstore.one("SELECT count(*) FROM events WHERE kind='client.finished'")[0] == 1


def test_retry_survives_crash_after_enqueue_without_failing_successor(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    ctx.hermes.fail(row["run_id"], "crashed")
    owner = coordinator(ctx)
    owner.reconcile()
    owner.advance_attempts(ctx)
    [successor] = ctx.wstore.q(
        "SELECT * FROM client_admissions WHERE native_key=?", (f"retry:{row['op_id']}",)
    )
    assert successor["op_id"] != row["op_id"]
    # The effect committed but the predecessor acknowledgment did not.
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute("UPDATE client_admissions SET advanced_at=NULL WHERE op_id=?", (row["op_id"],))
    dispatch_inputs(ctx)
    owner.advance_attempts(ctx)
    assert tasks.read(ctx.store, 1)["status"] == "in-progress"
    assert len(ctx.hermes.runs) == 2
    assert (
        ctx.wstore.one("SELECT status FROM client_admissions WHERE op_id=?", (row["op_id"],))[0]
        == "failed"
    )
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 2


def test_exhausted_worker_records_failure_without_requeueing_old_attempt(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    set_setting(ctx, "recovery.max_attempts", 1)
    ctx.hermes.fail(row["run_id"], "crashed")
    owner = coordinator(ctx)
    owner.reconcile()
    owner.advance_attempts(ctx)
    owner.advance_attempts(ctx)
    assert tasks.read(ctx.store, 1)["outcome"] == "failed"
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 1
    assert ctx.wstore.one("SELECT count(*) FROM events WHERE kind='recovery.failed'")[0] == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_unreachable_or_missing_attempt_never_authorizes_retry(tmp_path, unknown):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    owner = coordinator(ctx)
    if unknown:
        del ctx.hermes.runs[row["run_id"]]
        owner.reconcile()
    else:

        def unavailable(*args, **kwargs):
            raise RunsUnavailable("gateway unreachable")

        ctx.hermes.status = unavailable
        with pytest.raises(RunsUnavailable):
            owner.reconcile()
    owner.advance_attempts(ctx)
    assert tasks.read(ctx.store, 1)["status"] == "in-progress"
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 1
    assert bool(ctx.wstore.one("SELECT execution_uncertain FROM client_admissions")[0]) == unknown


def test_pause_defers_retry_and_native_admission_through_same_gate(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    ctx.hermes.fail(row["run_id"], "crashed")
    owner = coordinator(ctx)
    owner.reconcile()
    set_setting(ctx, "paused", True)
    owner.advance_attempts(ctx)
    dispatch_inputs(ctx)
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 1
    set_setting(ctx, "paused", False)
    owner.advance_attempts(ctx)
    set_setting(ctx, "paused", True)  # pause races the queued successor
    dispatch_inputs(ctx)
    assert len(ctx.hermes.runs) == 1
    set_setting(ctx, "paused", False)
    dispatch_inputs(ctx)
    assert len(ctx.hermes.runs) == 2


def test_pending_messages_are_durable_steers_not_duplicate_worker_starts(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    tasks.message(ctx.store, HARNESS, 1, text="change direction", direction="to_researcher")
    owner = coordinator(ctx)
    for _ in range(3):
        owner.deliver_worker_messages(ctx)
    assert tasks.pending_messages(ctx.store, 1, "to_researcher")
    dispatch_inputs(ctx)
    dispatch_inputs(ctx)  # receipt commits source-message delivery
    assert len(ctx.hermes.runs) == 1
    assert len(ctx.hermes.steered[row["run_id"]]) == 1
    assert not tasks.pending_messages(ctx.store, 1, "to_researcher")


def test_wait_resolution_queues_once_and_never_starts_in_handler(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    owner = coordinator(ctx)
    with ctx.store.tx(HARNESS) as tx:
        seq = tx.event("task.wait_resolved", "T-1", {"kind": "user", "note": "approved"})
    owner.advance_events(ctx)
    owner.advance_events(ctx)
    assert len(ctx.hermes.runs) == 1
    assert not events.unhandled(ctx.store, {"task.wait_resolved"})
    assert ctx.store.one("SELECT handled_at FROM handled_events WHERE seq=?", (seq,))[0]
    dispatch_inputs(ctx)
    assert len(ctx.hermes.steered[row["run_id"]]) == 1


def test_inactive_coworld_outcomes_remain_pending_until_selected(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    ctx.hermes.fail(row["run_id"], "crashed")
    owner = coordinator(ctx)
    owner.reconcile()
    original = ctx.coworld
    ctx.coworld = None
    owner.advance_attempts(ctx)
    assert ctx.wstore.one("SELECT advanced_at FROM client_admissions")[0] is None
    ctx.coworld = original
    owner.advance_attempts(ctx)
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 2


def test_wake_claim_interrupted_before_session_registration_is_reused(tmp_path):
    ctx = make_ctx(tmp_path)
    with ctx.store.tx(HARNESS) as tx:
        seq = tx.event("task.asked", "T-1", {"question": "what next?"})
    events.claim_handling(ctx.store, HARNESS, seq, "task.asked", "claimed")
    coordinator(ctx).advance_events(ctx)
    [row] = ctx.wstore.q("SELECT * FROM client_admissions")
    assert row["session_id"] == "claimed"
    assert ctx.store.one("SELECT attempts FROM handled_events")[0] == 1


def test_wake_failure_retries_then_stops_at_budget(tmp_path):
    ctx = make_ctx(tmp_path)
    with ctx.store.tx(HARNESS) as tx:
        tx.event("task.asked", "T-1", {"question": "what next?"})
    owner = coordinator(ctx)
    for _ in range(2):
        owner.advance_events(ctx)
        dispatch_inputs(ctx)
        row = ctx.wstore.one("SELECT * FROM client_admissions ORDER BY rowid DESC LIMIT 1")
        ctx.hermes.fail(row["run_id"], "model failed")
        owner.reconcile()
        owner.advance_attempts(ctx)
    owner.advance_events(ctx)
    assert ctx.wstore.one("SELECT count(*) FROM events WHERE kind='wake.failed'")[0] == 1
    assert len(ctx.hermes.runs) == 2


def test_repeated_events_join_live_wake_without_second_admission(tmp_path):
    ctx = make_ctx(tmp_path)
    owner = coordinator(ctx)
    for _ in range(2):
        with ctx.store.tx(HARNESS) as tx:
            tx.event("task.asked", "T-1", {"question": "what next?"})
        owner.advance_events(ctx)
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 1
    dispatch_inputs(ctx)
    [run] = ctx.hermes.runs.values()
    ctx.hermes.complete(run.run_id)
    owner.reconcile()
    owner.advance_attempts(ctx)
    assert not events.unhandled(ctx.store, {"task.asked"})


@pytest.mark.parametrize("disposition", ["answer", "escalation", "abandon", "empty_reply"])
def test_question_wake_finishes_from_its_durable_disposition(tmp_path, monkeypatch, disposition):
    ctx = make_ctx(tmp_path)
    worker = assigned(ctx)
    task = tasks.ask(ctx.store, Actor.session(worker["session_id"]), 1, question="Which seed?")
    question = task["waiting_on"]["ref"]
    owner = coordinator(ctx)
    owner.advance_events(ctx)
    dispatch_inputs(ctx)
    [wake] = ctx.wstore.q("SELECT * FROM client_admissions WHERE origin='wake'")
    assert wake["status"] == "running"
    tool_ctx = ToolContext(
        "orchestrator",
        Actor.session(wake["session_id"]),
        ctx.ws,
        {**ctx.env, "IDE_SESSION_ID": wake["session_id"]},
    )
    tool_ctx.store, tool_ctx.wstore = ctx.store, ctx.wstore
    monkeypatch.setattr(transitions, "REQUEST_WAIT_SECONDS", 0)
    forbidden = json.loads(
        run_tool("task", {"action": "message", "task": "T-1", "text": "Unrelated work"}, tool_ctx)
    )
    assert not forbidden["ok"] and "bound question" in forbidden["error"]

    if disposition == "answer":
        json.loads(
            run_tool(
                "task",
                {
                    "action": "answer",
                    "task": "T-1",
                    "question_id": question,
                    "text": "Use seed 7 when credits refill.",
                    "wait_on": {"kind": "credits", "detail": "Need 10 credits"},
                },
                tool_ctx,
            )
        )
        transitions.apply(ctx, owner.instance_id)
        assert tasks.read(ctx.store, 1)["waiting_on"]["kind"] == "credits"
    elif disposition == "escalation":
        set_setting(ctx, "chat_updates.categories", [])
        result = json.loads(
            run_tool(
                "chat_update",
                {"category": "decision", "text": "Which seed should we use?"},
                tool_ctx,
            )
        )
        assert result["ok"] and result["notice"]["refs"] == ["T-1"]
        assert tasks.read(ctx.store, 1)["waiting_on"]["ref"] == question
    elif disposition == "abandon":
        tasks.abandon(
            ctx.store, HARNESS, 1, failure={"what_failed": "Deprecated", "next_step": "None"}
        )
    else:
        ctx.hermes.complete(wake["run_id"])
        owner.reconcile()
        owner.advance_attempts(ctx)
        event = ctx.store.one("SELECT * FROM handled_events WHERE kind='task.asked'")
        assert event["handled_at"] is None and "before handling" in event["last_error"]
        return

    owner.advance_events(ctx)
    dispatch_inputs(ctx)
    assert ("stop", {"run_id": wake["run_id"], "profile": "orchestrator"}) in ctx.hermes.calls
    ctx.hermes.runs[wake["run_id"]].status = "cancelled"
    owner.reconcile()
    owner.advance_attempts(ctx)
    owner.advance_events(ctx)
    assert not events.unhandled(ctx.store, {"task.asked"})
    assert len(ctx.wstore.q("SELECT * FROM client_admissions WHERE origin='wake'")) == 1


def test_model_planning_is_durable_and_observer_cannot_schedule(tmp_path):
    ctx = make_ctx(tmp_path)
    make_task(ctx)
    owner = coordinator(ctx)
    owner.reconcile()
    assert not ctx.wstore.q("SELECT * FROM client_admissions")
    owner.plan_assignments(ctx)
    owner.plan_assignments(ctx)
    [row] = ctx.wstore.q("SELECT * FROM client_admissions")
    assert row["origin"] == "planning" and row["status"] == "queued"
    assert "task.assign" in json.loads(row["payload"])["input"]
    assert not ctx.hermes.runs


def test_missing_old_generation_receipt_uses_existing_exclusion_proof(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute("UPDATE client_admissions SET runtime_id='old-runtime'")
    del ctx.hermes.runs[row["run_id"]]
    owner = Coordinator(ctx.ws, ctx.hermes, "new-runtime")
    owner.reconcile()
    assert not admissions.restart_required(ctx.wstore)
    owner.advance_attempts(ctx)
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 2


def test_busy_wake_keeps_its_claim_and_immutable_admission(tmp_path):
    ctx = make_ctx(tmp_path)
    with ctx.store.tx(HARNESS) as tx:
        seq = tx.event("task.asked", "T-1", {"question": "what next?"})
    owner = coordinator(ctx)
    owner.advance_events(ctx)
    [row] = ctx.wstore.q("SELECT * FROM client_admissions")
    ctx.hermes.set_busy(True)
    dispatch_inputs(ctx)
    owner.advance_events(ctx)
    assert ctx.store.one("SELECT attempts FROM handled_events WHERE seq=?", (seq,))[0] == 1
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 1
    ctx.hermes.set_busy(False)
    # The refusal backs off for recovery.delivery_backoff_seconds; let that pass.
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute("UPDATE client_admissions SET retry_at=NULL")
    dispatch_inputs(ctx)
    assert ctx.wstore.one("SELECT run_id FROM client_admissions WHERE op_id=?", (row["op_id"],))[0]
    assert len(ctx.hermes.runs) == 1


def test_workspace_wake_without_active_coworld_uses_same_handoff(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.store, ctx.coworld = None, None
    with ctx.wstore.tx(HARNESS) as tx:
        tx.event("store.unmarked_write", "workspace", {"tables": ["sessions"]})
    owner = coordinator(ctx)
    owner.advance_events(ctx)
    dispatch_inputs(ctx)
    [run] = ctx.hermes.runs.values()
    ctx.hermes.complete(run.run_id)
    owner.reconcile()
    owner.advance_attempts(ctx)
    assert not events.unhandled(ctx.wstore, {"store.unmarked_write"})


def test_wake_prompt_keeps_large_payload_bounded(tmp_path):
    ctx = make_ctx(tmp_path)
    with ctx.store.tx(HARNESS) as tx:
        tx.event("task.asked", "T-1", {"question": "x" * 9000})
    [event] = events.unhandled(ctx.store, {"task.asked"})
    text = wakes.render_wake("management", event, ctx)
    assert "payload cut here" in text and len(text) < wakes.PAYLOAD_CHARS + 4000


def test_distinct_wait_events_on_live_worker_keep_both_inputs(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    owner = coordinator(ctx)
    for note in ("first approval", "second approval"):
        with ctx.store.tx(HARNESS) as tx:
            tx.event("task.wait_resolved", "T-1", {"kind": "user", "note": note})
        owner.advance_events(ctx)
        dispatch_inputs(ctx)
    assert len(ctx.hermes.runs) == 1
    assert len(ctx.hermes.steered[row["run_id"]]) == 2
    assert "first approval" in ctx.hermes.steered[row["run_id"]][0]
    assert "second approval" in ctx.hermes.steered[row["run_id"]][1]


def test_event_handoff_replay_after_native_receipt_has_no_second_delivery(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    ctx.hermes.complete(row["run_id"])
    owner = coordinator(ctx)
    owner.reconcile()
    # Waiting resolves after its worker deliberately ended; no crash retry is due.
    with ctx.store.tx(HARNESS) as tx:
        seq = tx.event("task.wait_resolved", "T-1", {"kind": "user", "note": "approved"})
    owner.advance_events(ctx)
    dispatch_inputs(ctx)
    with ctx.store.tx(HARNESS) as tx:
        tx.execute("DELETE FROM handled_events WHERE seq=?", (seq,))
    owner.advance_events(ctx)
    dispatch_inputs(ctx)
    assert len(ctx.hermes.runs) == 2
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 2


def test_arriving_message_does_not_copy_already_reserved_message(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    owner = coordinator(ctx)
    tasks.message(ctx.store, HARNESS, 1, text="first message", direction="to_researcher")
    owner.deliver_worker_messages(ctx)
    tasks.message(ctx.store, HARNESS, 1, text="second message", direction="to_researcher")
    owner.deliver_worker_messages(ctx)
    dispatch_inputs(ctx)
    dispatch_inputs(ctx)
    deliveries = ctx.hermes.steered[row["run_id"]]
    assert len(deliveries) == 2
    assert sum(text.count("first message") for text in deliveries) == 1
    assert sum(text.count("second message") for text in deliveries) == 1


def test_pause_retries_failed_drain_until_quiet(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    set_setting(ctx, "paused", True)
    reports = iter(
        [
            {"quiet": False, "errors": [{"error": "temporary stop failure"}]},
            {"quiet": True, "errors": []},
        ]
    )
    monkeypatch.setattr("research_core.coordinator.drain.run", lambda context: next(reports))
    owner = coordinator(ctx)
    owner.advance(ctx)
    assert ctx.meta("paused_drained") is None
    owner.advance(ctx)
    assert ctx.meta("paused_drained") == "1"
    owner.advance(ctx)  # no third drain invocation


def test_drained_worker_continuation_has_one_identity_across_resume_events(tmp_path):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    ctx.hermes.stop(row["run_id"], profile="researcher")
    sessions.set_stopped_by(ctx.wstore, HARNESS, row["session_id"], "watcher:drain")
    sessions.mark_drained(ctx.wstore, HARNESS, [row["session_id"]], True)
    owner = coordinator(ctx)
    owner.reconcile()
    owner.advance_attempts(ctx)
    owner.plan_assignments(ctx)
    owner.plan_assignments(ctx)
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 2
    dispatch_inputs(ctx)
    assert len(ctx.hermes.runs) == 2
    assignment = ctx.wstore.one("SELECT * FROM task_assignments")
    current = sessions.read(ctx.wstore, assignment["session_id"])
    assert current["id"] != row["session_id"]
    assert not current["drained"] and current["stopped_by"] is None
    assert sessions.read(ctx.wstore, row["session_id"])["stopped_by"] == "watcher:drain"


def test_open_receipt_is_not_a_completed_execution_attempt(tmp_path):
    ctx = make_ctx(tmp_path)
    sessions.register(
        ctx.wstore, HARNESS, session_id="new-chat", profile="orchestrator", kind="chat"
    )
    intake.accept(
        ctx.wstore,
        HARNESS,
        op_id="open-receipt",
        kind="open",
        request={},
        session_id="new-chat",
        message="",
        payload="{}",
    )
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute("UPDATE client_admissions SET status='completed' WHERE kind='open'")
    coordinator(ctx).advance_attempts(ctx)
    assert sessions.read(ctx.wstore, "new-chat")["ended"] is None
    assert ctx.wstore.one("SELECT advanced_at FROM client_admissions")[0] is None


def test_malformed_accepted_start_replays_its_original_identity(tmp_path):
    class MalformedReply(FakeHermesRuns):
        start = HermesRuns.start
        malformed = True

        def _request(self, method, profile, path, *, body, headers):
            assert (method, path) == ("POST", "/v1/runs")
            run_id = FakeHermesRuns.start(
                self,
                profile=profile,
                session_id=body["session_id"],
                text=body["input"],
                idempotency_key=headers["Idempotency-Key"],
            )
            payload = {} if self.malformed else {"run_id": run_id}
            return SimpleNamespace(status=202, payload=payload)

    ctx = make_ctx(tmp_path)
    ctx.hermes = MalformedReply()
    row = assigned(ctx)
    owner = coordinator(ctx)
    assert row["status"] == "queued" and row["dispatch_started"] == 1
    assert row["run_id"] is None
    for _ in range(3):
        owner.advance_attempts(ctx)
        owner.finish_assignments(ctx.wstore)
        dispatch_inputs(ctx)
    assert len(ctx.hermes.runs) == 1
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 1
    assert ctx.wstore.one("SELECT count(*) FROM task_assignments")[0] == 1
    assert tasks.read(ctx.store, 1)["held_by"] == row["session_id"]
    ctx.hermes.malformed = False
    dispatch_inputs(ctx)
    recovered = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (row["op_id"],))
    assert recovered["status"] == "running"
    assert recovered["run_id"] == next(iter(ctx.hermes.runs))
    assert len(ctx.hermes.runs) == 1


def test_definitely_rejected_worker_messages_get_immutable_redelivery(tmp_path):
    class RejectedSteer(FakeHermesRuns):
        reject = True

        def steer(self, run_id, text, *, profile):
            if self.reject:
                raise RunsAuthError("Credentials rejected", status=403)
            return super().steer(run_id, text, profile=profile)

    ctx = make_ctx(tmp_path)
    ctx.hermes = RejectedSteer()
    set_setting(ctx, "recovery.max_attempts", 3)
    initial = assigned(ctx)
    owner = coordinator(ctx)
    tasks.message(ctx.store, HARNESS, 1, text="Preserve this message", direction="to_researcher")
    rejected = []
    for _ in range(2):
        for _ in range(3):
            owner.advance_attempts(ctx)
            owner.deliver_worker_messages(ctx)
        dispatch_inputs(ctx)
        [attempt] = ctx.wstore.q(
            "SELECT * FROM client_admissions WHERE status='failed' AND advanced_at IS NULL"
        )
        assert attempt["dispatch_started"] == 0
        rejected.append(attempt)
    ctx.hermes.reject = False
    for _ in range(3):
        owner.advance_attempts(ctx)
        owner.deliver_worker_messages(ctx)
    dispatch_inputs(ctx)
    dispatch_inputs(ctx)
    assert not tasks.pending_messages(ctx.store, 1, "to_researcher")
    assert len(ctx.hermes.runs) == 1
    assert len(ctx.hermes.steered[initial["run_id"]]) == 1
    assert "Preserve this message" in ctx.hermes.steered[initial["run_id"]][0]
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 4
    for attempt in rejected:
        current = ctx.wstore.one(
            "SELECT * FROM client_admissions WHERE op_id=?", (attempt["op_id"],)
        )
        assert current["status"] == "failed"
        assert current["payload"] == attempt["payload"]
        assert current["native_key"] == attempt["native_key"]
        assert current["run_id"] is None


def test_dependency_reopened_after_reservation_settles_without_native_start(tmp_path):
    ctx = make_ctx(tmp_path)
    dependency = assigned(ctx)
    complete_task(ctx, dependency)
    task = tasks.create(
        ctx.store,
        HARNESS,
        type="maintenance",
        title="Dependent work",
        description="Requires completed prerequisite",
        depends_on=["T-1"],
    )
    command = SimpleNamespace(
        env=ctx.env, store=ctx.store, wstore=ctx.wstore, actor=HARNESS, coworld=lambda: ctx.coworld
    )
    reserved = Coordinator.reserve(
        command,
        task_id=2,
        task_updated=task["updated"],
        profile="researcher",
        assignment_id="dependent",
        context="Continue after prerequisite",
        rationale="Dependency is complete",
    )
    tasks.reopen(ctx.store, HARNESS, 1, reason="Prerequisite needs revision")
    for _ in range(3):
        dispatch_inputs(ctx)
        coordinator(ctx).finish_assignments(ctx.wstore)
    admission = ctx.wstore.one(
        "SELECT * FROM client_admissions WHERE op_id=?", (reserved["op_id"],)
    )
    assert admission["status"] == "failed" and not admission["dispatch_started"]
    assert "waits for T-1" in admission["error"]
    assert len(ctx.hermes.runs) == 1
    assert tasks.read(ctx.store, 2)["status"] == "unclaimed"
    assert ctx.wstore.one("SELECT 1 FROM task_assignments WHERE task_id=2") is None


def test_dispatch_polls_running_receipt_once_per_interval_and_acknowledges_once(
    tmp_path, monkeypatch
):
    import asyncio
    import time

    from research_core import serve

    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    server = serve.SeamServer(ws=ctx.ws, hermes=ctx.hermes, env=dict(ctx.env))
    monkeypatch.setattr(serve, "RUN_POLL_INTERVAL_S", 60)
    acknowledged = []
    acknowledge = server.coordinator.acknowledge_inputs

    def track(store, admission):
        acknowledged.append(admission["op_id"])
        acknowledge(store, admission)

    monkeypatch.setattr(server.coordinator, "acknowledge_inputs", track)
    ctx.hermes.calls.clear()
    try:
        for _ in range(3):
            asyncio.run(server.dispatch_messages())
        assert acknowledged == [row["op_id"]]
        assert sum(method == "status" for method, _ in ctx.hermes.calls) == 1
        ctx.hermes.complete(row["run_id"])
        server._last_native_poll[(row["profile"], row["run_id"])] = time.monotonic() - 61
        asyncio.run(server.dispatch_messages())
        assert (
            ctx.wstore.one("SELECT status FROM client_admissions WHERE op_id=?", (row["op_id"],))[0]
            == "completed"
        )
        assert acknowledged == [row["op_id"]]
    finally:
        server._http.server_close()
