"""Durable admission scenarios at the native boundary, using real workspace stores."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from research_core.coordinator import Coordinator, ownership
from research_core.store import admissions, sessions, settings, tasks
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.store.errors import LeaseError, StoreError
from research_core.watcher.hermes_api import FakeHermesRuns, RunsBusy, RunsError, RunsUnavailable
from tests.test_serve import HARNESS, make_ctx


class LostResponse(FakeHermesRuns):
    def __init__(self):
        super().__init__()
        self.lose_response = True

    def start(self, **kwargs):
        run_id = super().start(**kwargs)
        if self.lose_response:
            self.lose_response = False
            raise RunsUnavailable("response lost after native acceptance")
        return run_id


def chat(ctx, session_id="api_test"):
    sessions.register(
        ctx.wstore, HARNESS, session_id=session_id, profile="orchestrator", kind="chat"
    )
    return {
        "profile": "orchestrator",
        "session_id": session_id,
        "text": "work",
        "idempotency_key": "stable-input",
    }


def test_lost_acceptance_is_recovered_with_same_identity_after_owner_restart(tmp_path):
    ctx = make_ctx(tmp_path)
    native = LostResponse()
    request = chat(ctx)
    with pytest.raises(RunsUnavailable):
        Coordinator(ctx.workspace, native).start(**request)
    [admission] = ctx.wstore.q("SELECT * FROM client_admissions")
    assert admission["dispatch_started"] == 1 and admission["run_id"] is None
    assert admission["auth_scope"] and admission["retry_until"]
    run_id = Coordinator(ctx.workspace, native).start(**request)
    assert len(native.runs) == 1 and run_id in native.runs
    assert sessions.read(ctx.wstore, request["session_id"])["run_id"] == run_id


@pytest.mark.parametrize("cause", ["expired", "key_rotation"])
def test_uncertain_start_is_not_replayed_outside_its_contract(tmp_path, cause):
    ctx = make_ctx(tmp_path)
    native = LostResponse()
    request = chat(ctx)
    coordinator = Coordinator(ctx.workspace, native)
    with pytest.raises(RunsUnavailable):
        coordinator.start(**request)
    if cause == "expired":
        with ctx.wstore.tx(HARNESS) as tx:
            tx.execute(
                "UPDATE client_admissions SET retry_until=?",
                ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
            )
    else:
        native.keys["orchestrator"] = "new-authentication-generation"
    with pytest.raises(RunsError) as failure:
        coordinator.start(**request)
    assert failure.value.code == "execution_interrupted"
    assert len(native.calls) == 1
    assert ctx.wstore.one("SELECT resolution_required FROM client_admissions")[0] == 0
    assert ctx.wstore.one("SELECT status FROM client_admissions")[0] == "interrupted"


def test_payload_conflict_never_bumps_the_execution_key(tmp_path):
    ctx = make_ctx(tmp_path)
    native = FakeHermesRuns()
    request = chat(ctx)
    coordinator = Coordinator(ctx.workspace, native)
    coordinator.start(**request)
    with pytest.raises(RunsError) as failure:
        coordinator.start(**(request | {"text": "changed payload"}))
    assert failure.value.code == "payload_conflict" and len(native.runs) == 1


def test_pause_closes_fresh_admission_but_recovers_an_already_admitted_start(tmp_path):
    ctx = make_ctx(tmp_path)
    native = LostResponse()
    request = chat(ctx)
    coordinator = Coordinator(ctx.workspace, native)
    with pytest.raises(RunsUnavailable):
        coordinator.start(**request)
    settings.update(ctx.wstore, HARNESS, "paused", True, cites="test pause")
    assert coordinator.start(**request) in native.runs
    other = chat(ctx, "api_other") | {"idempotency_key": "new-input"}
    with pytest.raises(RunsBusy):
        coordinator.start(**other)
    assert len(native.runs) == 1


def test_only_one_service_can_own_workspace_dispatch(tmp_path):
    ctx = make_ctx(tmp_path)
    with ownership(ctx.workspace):
        with pytest.raises(BlockingIOError):
            with ownership(ctx.workspace):
                pytest.fail("a second owner entered")
    with ownership(ctx.workspace):
        pass


def test_assignment_is_fixed_and_task_is_claimed_before_native_execution(tmp_path):
    ctx = make_ctx(tmp_path)
    task = tasks.create(
        ctx.store, HARNESS, type="maintenance", title="Repair", description="Repair the build"
    )
    decision = {
        "task_id": 1,
        "task_updated": task["updated"],
        "profile": "researcher",
        "assignment_id": "repair-1",
        "context": "Inspect compiler output",
        "rationale": "Build expertise",
    }
    receipt = Coordinator.reserve(ctx, **decision)
    assert Coordinator.reserve(ctx, **decision) == receipt
    [entry] = ctx.wstore.q("SELECT * FROM client_admissions")
    observed = []

    def inspect_claim(run):
        with Store.open(ctx.workspace.coworld("cow_fake").store_path, kind="coworld") as reader:
            current = tasks.read(reader, 1)
            observed.append((current["status"], current["held_by"]))

    native = FakeHermesRuns(script=inspect_claim)
    Coordinator(ctx.workspace, native).start(
        profile=entry["profile"],
        session_id=entry["session_id"],
        text=json.loads(entry["payload"])["input"],
        idempotency_key=entry["native_key"],
    )
    assert observed == [("in-progress", receipt["session_id"])]
    with pytest.raises(StoreError):
        Coordinator.reserve(ctx, **(decision | {"assignment_id": "another-owner"}))
    assert len(native.runs) == 1


def test_assignments_reserve_capacity_before_any_worker_starts(tmp_path):
    ctx = make_ctx(tmp_path)
    settings.update(ctx.wstore, HARNESS, "max_researchers", 1, cites="one slot")
    first = tasks.create(ctx.store, HARNESS, type="maintenance", title="One", description="First")
    second = tasks.create(ctx.store, HARNESS, type="maintenance", title="Two", description="Second")
    Coordinator.reserve(
        ctx,
        task_id=1,
        task_updated=first["updated"],
        profile="researcher",
        assignment_id="one",
        context="One",
        rationale="Fit",
    )
    with pytest.raises(StoreError, match="slot is taken"):
        Coordinator.reserve(
            ctx,
            task_id=2,
            task_updated=second["updated"],
            profile="researcher",
            assignment_id="two",
            context="Two",
            rationale="Fit",
        )
    assert ctx.wstore.one("SELECT COUNT(*) FROM task_assignments")[0] == 1
    settings.update(ctx.wstore, HARNESS, "max_researchers", 2, cites="allow another reservation")
    Coordinator.reserve(
        ctx,
        task_id=2,
        task_updated=second["updated"],
        profile="researcher",
        assignment_id="two",
        context="Two",
        rationale="Fit",
    )
    settings.update(ctx.wstore, HARNESS, "max_researchers", 1, cites="lower limit with backlog")
    first_request, second_request = [
        {
            "profile": entry["profile"],
            "session_id": entry["session_id"],
            "text": json.loads(entry["payload"])["input"],
            "idempotency_key": entry["native_key"],
        }
        for entry in ctx.wstore.q("SELECT * FROM client_admissions ORDER BY rowid")
    ]
    native = FakeHermesRuns()
    coordinator = Coordinator(ctx.workspace, native)
    first_run = coordinator.start(**first_request)
    assert first_run in native.runs
    with pytest.raises(RunsBusy) as refusal:
        coordinator.start(**second_request)
    assert refusal.value.code == "capacity_reserved"
    assert len(native.runs) == 1
    native.complete(first_run)
    coordinator.reconcile()
    assert coordinator.start(**second_request) in native.runs


def test_a_stale_lease_does_not_allow_a_second_worker(tmp_path):
    ctx = make_ctx(tmp_path)
    tasks.create(ctx.store, HARNESS, type="maintenance", title="One", description="First")
    tasks.claim(
        ctx.store, Actor.session("first"), 1, run_id="run_1", worktree="work", branch="task/1"
    )
    with ctx.store.tx(HARNESS) as tx:
        tx.execute("UPDATE tasks SET lease_renewed='2000-01-01T00:00:00Z' WHERE id=1")
    with pytest.raises(LeaseError):
        tasks.claim(
            ctx.store, Actor.session("second"), 1, run_id="run_2", worktree="work2", branch="task/1"
        )


def test_unknown_outcome_is_interrupted_and_allows_following_input(tmp_path):
    ctx = make_ctx(tmp_path)
    native = FakeHermesRuns()
    request = chat(ctx)
    coordinator = Coordinator(ctx.workspace, native, "old-runtime")
    run_id = coordinator.start(**request)
    del native.runs[run_id]
    admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="following",
        session_id=request["session_id"],
        profile="orchestrator",
        text="following",
        origin="client",
    )
    with pytest.raises(RunsBusy):
        coordinator.start(**(request | {"idempotency_key": "following", "text": "following"}))
    assert admissions.restart_required(ctx.wstore)
    coordinator = Coordinator(ctx.workspace, native, "new-runtime")
    coordinator.reconcile()
    following = coordinator.start(
        **(request | {"idempotency_key": "following", "text": "following"})
    )
    assert following != run_id
    assert (
        ctx.wstore.one("SELECT status FROM client_admissions WHERE native_key='stable-input'")[0]
        == "interrupted"
    )
    assert len([call for call in native.calls if call[0] == "start"]) == 2


def test_a_run_lost_in_a_gateway_restart_is_resolved_and_following_input_runs(tmp_path):
    ctx = make_ctx(tmp_path)
    native = FakeHermesRuns()
    request = chat(ctx)
    run_id = Coordinator(ctx.workspace, native, "old-runtime").start(**request)
    native.crash(run_id)  # the gateway restarted under the turn: Hermes holds it ``unknown``
    following = request | {"idempotency_key": "following", "text": "following"}
    admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="following",
        session_id=request["session_id"],
        profile="orchestrator",
        text="following",
        origin="client",
    )
    coordinator = Coordinator(ctx.workspace, native, "new-runtime")

    following_run = coordinator.start(**following)

    assert following_run != run_id and native.runs[following_run].status == "running"
    assert native.status(run_id, profile="orchestrator").status == "cancelled"
    coordinator.reconcile()
    assert tuple(
        ctx.wstore.one(
            "SELECT status, error FROM client_admissions WHERE native_key='stable-input'"
        )
    ) == ("interrupted", "The workspace restarted during the run")
    assert not admissions.restart_required(ctx.wstore)


def test_recovery_replays_old_message_ids_and_counts_a_running_continuation(tmp_path):
    from research_core.watcher.context import TickContext

    ctx = make_ctx(tmp_path)
    settings.update(ctx.wstore, HARNESS, "max_researchers", 1, cites="one worker")
    task = tasks.create(ctx.store, HARNESS, type="maintenance", title="One", description="Work")
    assigned = Coordinator.reserve(
        ctx,
        task_id=1,
        task_updated=task["updated"],
        profile="researcher",
        assignment_id="one",
        context="work",
        rationale="fit",
    )
    row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (assigned["op_id"],))
    native = LostResponse()
    native.lose_response = False
    coordinator = Coordinator(ctx.workspace, native)
    initial = coordinator.start(
        profile=row["profile"],
        session_id=row["session_id"],
        text=json.loads(row["payload"])["input"],
        idempotency_key=row["native_key"],
    )
    native.complete(initial)
    tasks.message(ctx.store, HARNESS, 1, text="first message", direction="to_researcher")
    tick = TickContext(
        ws=ctx.workspace,
        wstore=ctx.wstore,
        store=ctx.store,
        coworld=ctx.coworld(),
        hermes=coordinator,
        platform=None,
        env=ctx.env,
        now=datetime.now(UTC),
    )
    native.lose_response = True
    pending = coordinator.continue_session(
        tick, sessions.read(ctx.wstore, row["session_id"]), "continue"
    )
    assert pending["delivered"] == "queued"
    queued = ctx.wstore.one("SELECT * FROM client_admissions WHERE native_key=?", (pending["key"],))
    payload = json.loads(queued["payload"])
    with pytest.raises(RunsUnavailable):
        coordinator.start(
            profile=queued["profile"],
            session_id=queued["session_id"],
            text=payload["input"],
            idempotency_key=queued["native_key"],
        )
    tasks.message(ctx.store, HARNESS, 1, text="new message", direction="to_researcher")
    resumed = coordinator.continue_session(
        tick, sessions.read(ctx.wstore, row["session_id"]), "new context"
    )
    assert resumed["deferred"] and resumed["reason"] == "ownership handoff pending"
    coordinator.start(
        profile=queued["profile"],
        session_id=queued["session_id"],
        text=payload["input"],
        idempotency_key=queued["native_key"],
    )
    coordinator.acknowledge_inputs(
        ctx.wstore,
        ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (queued["op_id"],)),
    )
    assert len(native.runs) == 2
    assert [m["text"] for m in tasks.pending_messages(ctx.store, 1, "to_researcher")] == [
        "new message"
    ]
    second = tasks.create(ctx.store, HARNESS, type="maintenance", title="Two", description="Other")
    with pytest.raises(StoreError, match="slot is taken"):
        Coordinator.reserve(
            ctx,
            task_id=2,
            task_updated=second["updated"],
            profile="researcher",
            assignment_id="two",
            context="work",
            rationale="fit",
        )
