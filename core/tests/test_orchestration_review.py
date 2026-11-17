"""Regressions for the ordinary chat, release and watcher paths raised in PR review."""

import json
import threading

import pytest

from research_core import intake, serve
from research_core.client import ScriptedClient
from research_core.coordinator import Coordinator
from research_core.store import admissions, sessions, tasks
from research_core.watcher.hermes_api import FakeHermesRuns
from tests.test_serve import HARNESS, make_ctx


def test_assignment_enqueue_failure_rolls_back_session(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    task = tasks.create(ctx.store, HARNESS, type="maintenance", title="Repair", description="Fix")
    before = ctx.wstore.one("SELECT COUNT(*) FROM sessions")[0]

    def fail(*args, **kwargs):
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(admissions, "enqueue_in_transaction", fail)
    with pytest.raises(RuntimeError, match="enqueue failed"):
        Coordinator.reserve(
            ctx,
            task_id=1,
            task_updated=task["updated"],
            profile="researcher",
            assignment_id="one",
            context="fix",
            rationale="fit",
        )
    assert ctx.wstore.one("SELECT COUNT(*) FROM sessions")[0] == before
    assert ctx.wstore.one("SELECT COUNT(*) FROM task_assignments")[0] == 0


def test_release_stops_worker_then_returns_task_to_model_assignment(tmp_path):
    ctx = make_ctx(tmp_path)
    task = tasks.create(ctx.store, HARNESS, type="maintenance", title="Repair", description="Fix")
    receipt = Coordinator.reserve(
        ctx,
        task_id=1,
        task_updated=task["updated"],
        profile="researcher",
        assignment_id="one",
        context="fix",
        rationale="fit",
    )
    native = FakeHermesRuns()
    coordinator = Coordinator(ctx.workspace, native)
    row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (receipt["op_id"],))
    run_id = coordinator.start(
        profile=row["profile"],
        session_id=row["session_id"],
        text=json.loads(row["payload"])["input"],
        idempotency_key=row["native_key"],
    )
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute("UPDATE task_assignments SET state='release_requested'")
    coordinator.finish_assignments(ctx.wstore)
    assert native.status(run_id, profile="researcher").terminal
    assert tasks.read(ctx.store, 1)["held_by"] == receipt["session_id"]
    coordinator.finish_assignments(ctx.wstore)
    task = tasks.read(ctx.store, 1)
    assert task["status"] == "unclaimed" and task["held_by"] is None
    assert ctx.wstore.one("SELECT COUNT(*) FROM task_assignments")[0] == 0
    next_receipt = Coordinator.reserve(
        ctx,
        task_id=1,
        task_updated=task["updated"],
        profile="researcher",
        assignment_id="two",
        context="new approach",
        rationale="fit",
    )
    assert next_receipt["session_id"] != receipt["session_id"]


def test_legacy_uncertain_admission_is_closed_once_without_resending(tmp_path):
    ctx = make_ctx(tmp_path)
    sessions.register(ctx.wstore, HARNESS, session_id="chat", profile="orchestrator", kind="chat")
    op_id = admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="legacy",
        session_id="chat",
        profile="orchestrator",
        text="uncertain",
        origin="client",
    )
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute(
            "UPDATE client_admissions SET dispatch_started=1,resolution_required=1,error='legacy'"
        )
    native = FakeHermesRuns()
    coordinator = Coordinator(ctx.workspace, native, "boot-one")
    coordinator.reconcile()
    coordinator.reconcile()
    row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
    assert row["status"] == "interrupted" and row["resolution_required"] == 0
    assert len(ctx.wstore.q("SELECT * FROM events WHERE kind='client.delivery_failed'")) == 1
    assert not native.runs
    assert admissions.restart_required(ctx.wstore)
    coordinator = Coordinator(ctx.workspace, native, "boot-two")
    coordinator.reconcile()
    coordinator.reconcile()
    assert ctx.wstore.get_meta("runtime_recovery_count") == "1"
    assert not admissions.restart_required(ctx.wstore)
    run_id = coordinator.start(
        profile="orchestrator", session_id="chat", text="new input", idempotency_key="new"
    )
    assert native.runs[run_id].input == "new input"


def test_failed_and_blocked_watcher_does_not_stop_chat(tmp_path, monkeypatch, caplog):
    ctx = make_ctx(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def observe():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("watcher defect")
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(serve, "WATCHER_INTERVAL_S", 0.01)
    native = FakeHermesRuns()
    server = serve.SeamServer(ws=ctx.workspace, hermes=native, env={"HERMES_HOME": "fixture"})
    monkeypatch.setattr(server, "observe_once", observe)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ScriptedClient(server.url)
    try:
        assert entered.wait(3)
        assert "watcher defect" in caplog.text
        client.open()
        receipt = client.say("Chat still works")
        started = client.wait_admission(receipt["op_id"], timeout=3)
        assert started["run_id"] in native.runs
        assert client.health()["ok"]
    finally:
        release.set()
        server.shutdown()
        thread.join(3)
    assert not thread.is_alive()


def test_upgrade_adopts_existing_holder_without_starting_a_new_worker(tmp_path):
    from research_core.store.actors import Actor

    ctx = make_ctx(tmp_path)
    tasks.create(ctx.store, HARNESS, type="maintenance", title="Repair", description="Fix")
    sessions.register(
        ctx.wstore,
        HARNESS,
        session_id="existing",
        profile="researcher",
        kind="task",
        coworld_id="cow_fake",
        task_id=1,
    )
    native = FakeHermesRuns()
    run_id = native.start(profile="researcher", session_id="existing", text="already working")
    sessions.set_run(ctx.wstore, HARNESS, "existing", run_id)
    tasks.claim(
        ctx.store, Actor.session("existing"), 1, run_id=run_id, worktree="work", branch="task/1"
    )
    coordinator = Coordinator(ctx.workspace, native)
    coordinator.adopt_existing_workers(ctx.workspace, ctx.wstore)
    coordinator.adopt_existing_workers(ctx.workspace, ctx.wstore)
    [row] = ctx.wstore.q("SELECT * FROM task_assignments")
    assert row["session_id"] == "existing"
    assert ctx.wstore.one("SELECT run_id FROM client_admissions")[0] == run_id
    assert tasks.read(ctx.store, 1)["held_by"] == "existing"
    assert len(native.runs) == 1
    assert sessions.read(ctx.wstore, "existing")["attempts"] == 1


def test_upgrade_retains_unreceipted_holder_until_runtime_exclusion(tmp_path):
    from research_core.store.actors import Actor

    ctx = make_ctx(tmp_path)
    tasks.create(ctx.store, HARNESS, type="maintenance", title="Repair", description="Fix")
    sessions.register(
        ctx.wstore,
        HARNESS,
        session_id="existing",
        profile="researcher",
        kind="task",
        coworld_id="cow_fake",
        task_id=1,
    )
    tasks.claim(
        ctx.store, Actor.session("existing"), 1, run_id=None, worktree="work", branch="task/1"
    )
    native = FakeHermesRuns()
    Coordinator.adopt_existing_workers(ctx.workspace, ctx.wstore, "old-runtime")
    task = tasks.read(ctx.store, 1)
    assert task["status"] == "in-progress" and task["held_by"] == "existing"
    assert admissions.restart_required(ctx.wstore)
    Coordinator(ctx.workspace, native, "new-runtime").reconcile_inputs(ctx.wstore)
    assert not admissions.restart_required(ctx.wstore)
    assert not native.runs


def test_uncertain_execution_still_blocks_sleep_after_delivery_failure(tmp_path):
    from research_core.watcher import sleep
    from tests.watcher._support import make_ctx as tick_context

    ctx = tick_context(tmp_path)
    sessions.register(ctx.wstore, HARNESS, session_id="chat", profile="orchestrator", kind="chat")
    op_id = admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="lost",
        session_id="chat",
        profile="orchestrator",
        text="may be running",
        origin="client",
    )
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute(
            "UPDATE client_admissions SET dispatch_started=1,resolution_required=1,error='lost'"
        )
    owner = Coordinator(ctx.ws, ctx.hermes, "old-runtime")
    owner.reconcile()
    row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
    assert row["status"] == "interrupted" and row["execution_uncertain"] == 1
    report = sleep.go_to_sleep(ctx, wait_s=0, sleep=lambda _: None)
    assert f"admission:{op_id}:reconciling" in report["still_running"]
    Coordinator(ctx.ws, ctx.hermes, "new-runtime").reconcile()
    assert not admissions.restart_required(ctx.wstore)
    assert sleep.go_to_sleep(ctx, wait_s=0, sleep=lambda _: None)["still_running"] == []


def test_slow_worker_native_admission_does_not_hold_chat_admission_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    ctx = make_ctx(tmp_path)
    task = tasks.create(ctx.store, HARNESS, type="maintenance", title="Repair", description="Fix")
    receipt = Coordinator.reserve(
        ctx,
        task_id=1,
        task_updated=task["updated"],
        profile="researcher",
        assignment_id="one",
        context="fix",
        rationale="fit",
    )
    entered, release = threading.Event(), threading.Event()

    class SlowWorker(FakeHermesRuns):
        def capabilities(self, profile):
            if profile == "researcher":
                entered.set()
                assert release.wait(10)
            return super().capabilities(profile)

    native = SlowWorker()
    coordinator = Coordinator(ctx.workspace, native)
    row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (receipt["op_id"],))
    sessions.register(ctx.wstore, HARNESS, session_id="chat", profile="orchestrator", kind="chat")
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(
            coordinator.start,
            profile="researcher",
            session_id=row["session_id"],
            text=json.loads(row["payload"])["input"],
            idempotency_key=row["native_key"],
        )
        try:
            assert entered.wait(3)
            chat = workers.submit(
                coordinator.start,
                profile="orchestrator",
                session_id="chat",
                text="independent",
                idempotency_key="independent",
            )
            assert chat.result(timeout=3) in native.runs
        finally:
            release.set()
        assert pending.result(timeout=3) in native.runs


@pytest.mark.parametrize("status", [None, 400, 403, 429, 503, "refused"])
def test_definite_steer_rejection_preserves_exact_recovery_policy_without_restart(tmp_path, status):
    from research_core.watcher.hermes_api import (
        RunsAuthError,
        RunsBusy,
        RunsError,
        RunsUnavailable,
    )

    class RejectedSteer(FakeHermesRuns):
        reject = True

        attempts = 0

        def steer(self, run_id, text, *, profile):
            self.attempts += 1
            if self.reject:
                self.reject = False
                if status == "refused":
                    return False
                error_type = {
                    None: RunsAuthError,
                    400: RunsError,
                    403: RunsError,
                    429: RunsBusy,
                    503: RunsUnavailable,
                }[status]
                raise error_type(
                    "Rejected", status=status, code="gateway_draining" if status == 503 else ""
                )
            return super().steer(run_id, text, profile=profile)

    ctx = make_ctx(tmp_path)
    native = RejectedSteer()
    owner = Coordinator(ctx.workspace, native, "same-runtime")
    sessions.register(ctx.wstore, HARNESS, session_id="chat", profile="orchestrator", kind="chat")
    run_id = owner.start(
        profile="orchestrator", session_id="chat", text="original", idempotency_key="first"
    )
    other = native.start(profile="researcher", session_id="independent-worker", text="work")
    op_id = admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="followup",
        session_id="chat",
        profile="orchestrator",
        text="followup",
        origin="client",
    )
    with pytest.raises(RunsError) as raised:
        owner.start(
            profile="orchestrator", session_id="chat", text="followup", idempotency_key="followup"
        )
    assert raised.value.status == (None if status == "refused" else status)
    row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
    assert row["dispatch_started"] == 0 and row["execution_uncertain"] == 0
    owner.reconcile()
    assert not admissions.restart_required(ctx.wstore)
    assert ctx.wstore.get_meta("runtime_recovery_count") is None
    assert native.runs[other].status == "running"
    if status in {429, 503, "refused"}:
        assert row["status"] == "queued" and row["retry_at"] is not None
        assert raised.value.retryable
        with pytest.raises(RunsBusy) as waiting:
            owner.start(
                profile="orchestrator",
                session_id="chat",
                text="followup",
                idempotency_key="followup",
            )
        assert waiting.value.code == "delivery_backoff" and native.attempts == 1
        with ctx.wstore.tx(HARNESS) as tx:
            tx.execute(
                "UPDATE client_admissions SET retry_at='2000-01-01T00:00:00Z' WHERE op_id=?",
                (op_id,),
            )
        retry_key = "followup"
    else:
        assert row["status"] == "failed" and row["retry_at"] is None
        with pytest.raises(RunsError) as closed:
            owner.start(
                profile="orchestrator",
                session_id="chat",
                text="followup",
                idempotency_key="followup",
            )
        assert closed.value.code == "execution_closed" and closed.value.detail == "Rejected"
        assert native.attempts == 1
        retry_key = "followup-retry"
        admissions.enqueue(
            ctx.wstore,
            HARNESS,
            key=retry_key,
            session_id="chat",
            profile="orchestrator",
            text="followup",
            origin="client",
        )
    assert (
        owner.start(
            profile="orchestrator", session_id="chat", text="followup", idempotency_key=retry_key
        )
        == run_id
    )
    assert native.steered[run_id] == ["followup"] and native.attempts == 2
    assert native.runs[other].status == "running" and not admissions.restart_required(ctx.wstore)
    current = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
    if status in {429, 503, "refused"}:
        assert current["status"] == "running" and current["retry_at"] is None
        assert current["native_key"] == "followup" and current["run_id"] == run_id
        assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 2
    else:
        assert current["status"] == "failed" and current["run_id"] is None
        assert current["native_key"] == "followup" and current["payload"] == row["payload"]
        assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 3


def test_lost_steering_receipt_closes_only_delivery_and_is_not_resent(tmp_path):
    from research_core.watcher.hermes_api import RunsError, RunsUnavailable

    class LostSteer(FakeHermesRuns):
        def steer(self, run_id, text, *, profile):
            super().steer(run_id, text, profile=profile)
            raise RunsUnavailable("Reply lost after acceptance")

    ctx = make_ctx(tmp_path)
    native = LostSteer()
    owner = Coordinator(ctx.workspace, native, "same-runtime")
    sessions.register(ctx.wstore, HARNESS, session_id="chat", profile="orchestrator", kind="chat")
    run_id = owner.start(
        profile="orchestrator", session_id="chat", text="original", idempotency_key="first"
    )
    admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="followup",
        session_id="chat",
        profile="orchestrator",
        text="followup",
        origin="client",
    )
    with pytest.raises(RunsUnavailable):
        owner.start(
            profile="orchestrator", session_id="chat", text="followup", idempotency_key="followup"
        )
    with pytest.raises(RunsError, match="delivery unconfirmed"):
        owner.start(
            profile="orchestrator", session_id="chat", text="followup", idempotency_key="followup"
        )
    assert not admissions.restart_required(ctx.wstore)
    assert native.steered[run_id] == ["followup"]


def test_release_retains_unknown_run_until_runtime_exclusion(tmp_path):
    class LostRun(FakeHermesRuns):
        def stop(self, run_id, *, profile):
            raise AssertionError("A missing run must not loop through stop")

    ctx = make_ctx(tmp_path)
    task = tasks.create(ctx.store, HARNESS, type="maintenance", title="Repair", description="Fix")
    receipt = Coordinator.reserve(
        ctx,
        task_id=1,
        task_updated=task["updated"],
        profile="researcher",
        assignment_id="one",
        context="fix",
        rationale="fit",
    )
    native = LostRun()
    owner = Coordinator(ctx.workspace, native, "old-runtime")
    row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (receipt["op_id"],))
    run_id = owner.start(
        profile="researcher",
        session_id=row["session_id"],
        text=json.loads(row["payload"])["input"],
        idempotency_key=row["native_key"],
    )
    native.crash(run_id)
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute("UPDATE task_assignments SET state='release_requested'")
    owner.reconcile()
    owner.finish_assignments(ctx.wstore)
    assert tasks.read(ctx.store, 1)["status"] == "in-progress"
    assert ctx.wstore.one("SELECT execution_uncertain FROM client_admissions")[0] == 1
    replacement = Coordinator(ctx.workspace, native, "new-runtime")
    replacement.reconcile_inputs(ctx.wstore)
    replacement.finish_assignments(ctx.wstore)
    assert tasks.read(ctx.store, 1)["status"] == "unclaimed"
    assert ctx.wstore.one("SELECT COUNT(*) FROM task_assignments")[0] == 0


def test_local_recovery_preserves_store_and_refuses_managed_obligations(
    tmp_path, monkeypatch, capsys
):
    from research_core import cli

    ctx = make_ctx(tmp_path)
    monkeypatch.setenv("IDE_WORKSPACE", str(ctx.workspace.root))
    monkeypatch.delenv("IDE_RUNTIME_ID", raising=False)
    sessions.register(ctx.wstore, HARNESS, session_id="chat", profile="orchestrator", kind="chat")
    op_id = admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="lost",
        session_id="chat",
        profile="orchestrator",
        text="preserved",
        origin="client",
    )
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute(
            "UPDATE client_admissions SET execution_uncertain=1,status='interrupted',"
            "runtime_id='managed'"
        )
    assert cli.main(["workspace", "recover", "--runtime-stopped"]) == 1
    assert admissions.restart_required(ctx.wstore)
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute("UPDATE client_admissions SET runtime_id=NULL")
    active = admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="recorded-running",
        session_id="chat",
        profile="orchestrator",
        text="old running input",
        origin="client",
    )
    queued = admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="not-admitted",
        session_id="chat",
        profile="orchestrator",
        text="new queued input",
        origin="client",
    )
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute(
            "UPDATE client_admissions SET status='running',dispatch_started=1,"
            "run_id='old-run' WHERE op_id=?",
            (active,),
        )
    with pytest.raises(SystemExit):
        cli.main(["workspace", "recover"])
    assert admissions.restart_required(ctx.wstore)
    assert cli.main(["workspace", "recover", "--runtime-stopped"]) == 0
    assert (
        ctx.wstore.one("SELECT status FROM client_admissions WHERE op_id=?", (active,))[0]
        == "interrupted"
    )
    assert (
        ctx.wstore.one("SELECT status FROM client_admissions WHERE op_id=?", (queued,))[0]
        == "queued"
    )
    assert not admissions.restart_required(ctx.wstore)
    row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
    assert json.loads(row["payload"])["input"] == "preserved" and row["status"] == "interrupted"
    assert row["run_id"] is None
    assert "replayed" in capsys.readouterr().out


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_accepted_steer_then_server_error_is_not_resent_and_does_not_restart_workers(
    tmp_path, status
):
    from research_core.watcher.hermes_api import RunsError

    class AcceptedThenError(FakeHermesRuns):
        def steer(self, run_id, text, *, profile):
            super().steer(run_id, text, profile=profile)
            raise RunsError("Error after acceptance", status=status)

    ctx = make_ctx(tmp_path)
    native = AcceptedThenError()
    owner = Coordinator(ctx.workspace, native, "same-runtime")
    sessions.register(ctx.wstore, HARNESS, session_id="chat", profile="orchestrator", kind="chat")
    run_id = owner.start(
        profile="orchestrator", session_id="chat", text="original", idempotency_key="first"
    )
    other = native.start(profile="researcher", session_id="independent-worker", text="work")
    op_id = admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="followup",
        session_id="chat",
        profile="orchestrator",
        text="followup",
        origin="client",
    )
    with pytest.raises(RunsError, match="after acceptance"):
        owner.start(
            profile="orchestrator", session_id="chat", text="followup", idempotency_key="followup"
        )
    with pytest.raises(RunsError, match="delivery unconfirmed"):
        owner.start(
            profile="orchestrator", session_id="chat", text="followup", idempotency_key="followup"
        )
    row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
    assert row["status"] == "interrupted" and row["target_run_id"] == run_id
    assert sessions.read(ctx.wstore, "chat")["run_id"] == run_id
    assert row["execution_uncertain"] == 0
    assert native.steered[run_id] == ["followup"]
    assert native.runs[other].status == "running" and native.runs[run_id].status == "running"
    assert not admissions.restart_required(ctx.wstore)


@pytest.mark.parametrize("lost_start_reply", [False, True])
def test_failed_reopening_does_not_discard_existing_execution(tmp_path, lost_start_reply):
    import asyncio

    from research_core.watcher.hermes_api import RunsUnavailable
    from tests.test_coordinator import LostResponse

    ctx = make_ctx(tmp_path)
    native = LostResponse()
    native.lose_response = lost_start_reply
    server = serve.SeamServer(ws=ctx.workspace, hermes=native, env={})
    sessions.register(
        ctx.wstore, HARNESS, session_id="existing", profile="orchestrator", kind="chat"
    )
    try:
        if lost_start_reply:
            with pytest.raises(RunsUnavailable):
                server.coordinator.start(
                    profile="orchestrator",
                    session_id="existing",
                    text="work",
                    idempotency_key="first",
                )
        else:
            server.coordinator.start(
                profile="orchestrator", session_id="existing", text="work", idempotency_key="first"
            )
        intake.accept(
            ctx.wstore,
            HARNESS,
            op_id="failed-reopening",
            kind="open",
            request={},
            session_id="existing",
            message="",
            payload="",
        )
        with ctx.wstore.tx(HARNESS) as tx:
            tx.execute(
                "UPDATE client_admissions SET status='failed',error='Opening rejected'"
                " WHERE kind='open'"
            )
        asyncio.run(server.dispatch_messages())
        first = ctx.wstore.one("SELECT * FROM client_admissions WHERE native_key='first'")
        assert first["status"] == "running" and first["run_id"]
        assert len(native.runs) == 1
        new_input = admissions.enqueue(
            ctx.wstore,
            HARNESS,
            key="new-input",
            session_id="existing",
            profile="orchestrator",
            text="new work",
            origin="client",
        )
        asyncio.run(server.dispatch_messages())
        assert (
            ctx.wstore.one("SELECT status FROM client_admissions WHERE op_id=?", (new_input,))[0]
            == "failed"
        )
        assert (
            ctx.wstore.one("SELECT status FROM client_admissions WHERE native_key='first'")[0]
            == "running"
        )
    finally:
        server._http.server_close()


def test_unsent_steer_retries_after_connection_recovers_without_uncertainty(tmp_path):
    from research_core.watcher.hermes_api import RunsBusy, RunsNotSent

    class RefusedConnection(FakeHermesRuns):
        refuse = True

        attempts = 0

        def steer(self, run_id, text, *, profile):
            self.attempts += 1
            if self.refuse:
                raise RunsNotSent("Connection refused before send")
            return super().steer(run_id, text, profile=profile)

    ctx = make_ctx(tmp_path)
    native = RefusedConnection()
    owner = Coordinator(ctx.workspace, native, "same-runtime")
    sessions.register(ctx.wstore, HARNESS, session_id="chat", profile="orchestrator", kind="chat")
    first = owner.start(
        profile="orchestrator", session_id="chat", text="original", idempotency_key="one"
    )
    admissions.enqueue(
        ctx.wstore,
        HARNESS,
        key="two",
        session_id="chat",
        profile="orchestrator",
        text="followup",
        origin="client",
    )
    with pytest.raises(RunsNotSent):
        owner.start(
            profile="orchestrator", session_id="chat", text="followup", idempotency_key="two"
        )
    [pending] = ctx.wstore.q("SELECT * FROM client_admissions WHERE native_key='two'")
    assert not pending["dispatch_started"] and not pending["execution_uncertain"]
    assert pending["status"] == "queued" and pending["retry_at"] is not None
    native.refuse = False
    with pytest.raises(RunsBusy) as waiting:
        owner.start(
            profile="orchestrator", session_id="chat", text="followup", idempotency_key="two"
        )
    assert waiting.value.code == "delivery_backoff" and native.attempts == 1
    with ctx.wstore.tx(HARNESS) as tx:
        tx.execute(
            "UPDATE client_admissions SET retry_at='2000-01-01T00:00:00Z' WHERE op_id=?",
            (pending["op_id"],),
        )
    assert (
        owner.start(
            profile="orchestrator", session_id="chat", text="followup", idempotency_key="two"
        )
        == first
    )
    assert native.steered[first] == ["followup"] and native.attempts == 2
    [recovered] = ctx.wstore.q("SELECT * FROM client_admissions WHERE native_key='two'")
    assert recovered["op_id"] == pending["op_id"] and recovered["run_id"] == first
    assert recovered["status"] == "running" and recovered["retry_at"] is None
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 2
    assert not recovered["execution_uncertain"] and not admissions.restart_required(ctx.wstore)
