"""The dispatcher's handling of failed chat runs (``research_core.run_failures``).

Each test drives ``SeamServer.dispatch_messages`` one pass at a time against
:class:`FakeHermesRuns` and a temporary inference state, written the way the profile plugin's
``api_request_error`` hook writes it. The server's clock is the test's, so holds can pass
without sleeping.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from research_core import inference, run_failures, serve
from research_core.plugin.inference_hook import OUTAGE_COUNT, record_api_error
from research_core.serve import SeamServer
from research_core.store import admissions, settings
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.tools.base import ToolContext
from research_core.watcher.hermes_api import FakeHermesRuns, RunsAuthError


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class Dispatch:
    def __init__(self, tmp_path) -> None:
        ctx = ToolContext.from_env(
            "orchestrator", {"IDE_WORKSPACE": str(tmp_path), "IDE_SESSION_ID": "test-harness"}
        )
        ctx.workspace.ensure_dirs()
        self.lease = tmp_path / "inference-lease"
        # Far enough ahead that the clock can pass a refill without the lease expiring.
        self.lease.write_text((datetime.now(UTC) + timedelta(days=3)).isoformat())
        self.clock = Clock()
        self.hermes = FakeHermesRuns(inference_lease_file=self.lease)
        self.server = SeamServer(
            ws=ctx.workspace, hermes=self.hermes, inference_lease_file=self.lease, clock=self.clock
        )
        self.store = Store.open(ctx.workspace.store_path, kind="workspace")
        self.session_id = self.server.session_open(
            {"client": {"id": "ide", "kind": "test"}, "op_id": str(uuid.uuid4())}
        )["session_id"]

    def close(self) -> None:
        # The HTTP server never served (the test drives the dispatcher itself), so its
        # shutdown() would wait forever: only close its socket.
        self.server._http.server_close()
        self.store.close()

    def say(self, text: str, instructions: str | None = None) -> str:
        op_id = str(uuid.uuid4())
        self.server.message(
            {
                "session_id": self.session_id,
                "message": {"role": "user", "content": text},
                "instructions": instructions,
                "op_id": op_id,
            }
        )
        return op_id

    def tick(self) -> None:
        asyncio.run(self.server.dispatch_messages())

    def admission(self, op_id: str) -> dict:
        return next(row for row in admissions.snapshot(self.store) if row["op_id"] == op_id)

    def starts(self) -> list[dict]:
        return [call for method, call in self.hermes.calls if method == "start"]

    def api_error(self, reason: str, status_code: int | None = None) -> None:
        record_api_error(
            self.lease,
            session_id=self.session_id,
            reason=reason,
            status_code=status_code,
            now=self.clock(),
        )

    def start_and_fail(self, op_id: str, error: str, reason: str | None = None, **fields) -> str:
        """Dispatch the message, record Hermes's error class (if any), fail the run, dispatch."""
        self.tick()
        run_id = self.admission(op_id)["run_id"]
        assert run_id is not None
        if reason is not None:
            self.api_error(reason, fields.pop("status_code", None))
        self.hermes.fail(run_id, error, **fields)
        self.tick()
        return run_id


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    # Each tick must see the run's latest status, however close together the ticks are.
    monkeypatch.setattr(serve, "RUN_POLL_INTERVAL_S", 0)
    dispatch = Dispatch(tmp_path)
    yield dispatch
    dispatch.close()


# --- one test per class ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "status_code", "error", "status", "error_class"),
    [
        ("billing", 403, "HTTP 403: Key limit exceeded", "waiting", "budget_exhausted"),
        ("rate_limit", 429, "HTTP 429: slow down", "waiting", "rate_limited"),
        ("upstream_rate_limit", 429, "HTTP 429 upstream", "waiting", "rate_limited"),
        ("overloaded", 503, "HTTP 503: overloaded", "waiting", "provider_unavailable"),
        ("context_overflow", 400, "prompt is too long", "failed", "too_large"),
        ("payload_too_large", 413, "HTTP 413", "failed", "too_large"),
        ("auth_permanent", 401, "HTTP 401: no auth", "failed", "misconfigured"),
        ("model_not_found", 404, "HTTP 404: no such model", "failed", "misconfigured"),
    ],
)
def test_a_failed_run_takes_the_class_hermes_reported(
    dispatch, reason, status_code, error, status, error_class
):
    op_id = dispatch.say("analyse the replays")
    dispatch.start_and_fail(op_id, error, reason, status_code=status_code)
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error_class"], admission["error"]) == (
        status,
        error_class,
        error,
    )
    assert (admission["resume_after"] is not None) == (status == "waiting")


def test_a_misconfigured_workspace_is_logged_for_the_operator(dispatch, caplog):
    op_id = dispatch.say("hello")
    dispatch.start_and_fail(op_id, "HTTP 401: no auth", "auth_permanent", status_code=401)
    assert "misconfigured" in caplog.text


def test_a_provider_block_holds_the_message_until_the_block_lapses(dispatch):
    op_id = dispatch.say("hello")
    dispatch.tick()
    run_id = dispatch.admission(op_id)["run_id"]
    for _ in range(OUTAGE_COUNT):
        dispatch.api_error("server_error", 502)
    state = inference.read_state(dispatch.lease)
    assert state.blocked == "provider_unavailable"
    dispatch.hermes.fail(run_id, "HTTP 502")
    dispatch.tick()
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error_class"]) == ("waiting", "provider_unavailable")
    assert datetime.fromisoformat(admission["resume_after"]) >= state.until


def test_the_loop_guard_halt_ends_completed_with_its_explanation(dispatch):
    op_id = dispatch.say("hello")
    dispatch.tick()
    dispatch.hermes.complete(dispatch.admission(op_id)["run_id"], "I stopped: the loop guard ...")
    dispatch.tick()
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error_class"]) == ("completed", None)


# --- fallbacks and unknown failures ----------------------------------------------------------


def test_a_block_classifies_a_run_whose_own_error_was_not_recorded(dispatch):
    op_id = dispatch.say("hello")
    dispatch.tick()
    # Another Session's billing error blocked the workspace; this run's error is not recorded.
    record_api_error(
        dispatch.lease,
        session_id="api_other",
        reason="billing",
        status_code=402,
        now=dispatch.clock(),
    )
    dispatch.hermes.fail(dispatch.admission(op_id)["run_id"], "agent run failed")
    dispatch.tick()
    assert dispatch.admission(op_id)["error_class"] == "budget_exhausted"


def test_the_turn_exit_reason_classifies_without_an_inference_state(dispatch):
    op_id = dispatch.say("hello")
    dispatch.start_and_fail(
        op_id, "agent run failed", turn_exit_reason="context_compression_exhausted"
    )
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error_class"]) == ("failed", "too_large")


def test_the_error_text_classifies_as_a_last_resort(dispatch):
    op_id = dispatch.say("hello")
    dispatch.start_and_fail(op_id, "Billing or credits exhausted: HTTP 403 Key limit exceeded")
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error_class"]) == ("waiting", "budget_exhausted")


def test_an_unknown_failure_still_ends_failed_with_its_error_text(dispatch):
    op_id = dispatch.say("hello")
    dispatch.start_and_fail(
        op_id, "the tool crashed", turn_exit_reason="max_iterations_reached(90/90)"
    )
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error"], admission["error_class"]) == (
        "failed",
        "the tool crashed",
        None,
    )
    assert admission["resume_after"] is None
    dispatch.tick()
    assert len(dispatch.starts()) == 1


# --- waiting and resuming --------------------------------------------------------------------


def test_budget_failure_waits_queues_new_messages_and_resumes_once_with_a_continuation(dispatch):
    first = dispatch.say("tune the policy", instructions="answer in French")
    failed_run = dispatch.start_and_fail(
        first, "HTTP 403: Key limit exceeded", "billing", status_code=403
    )
    assert dispatch.admission(first)["status"] == "waiting"
    second = dispatch.say("and then submit it")
    until = inference.read_state(dispatch.lease).until
    assert until is not None
    resume_after = datetime.fromisoformat(dispatch.admission(first)["resume_after"])
    assert resume_after >= until  # the user is told the refill time

    # Past the refill time, but the gate stays closed until a new key: nothing starts.
    dispatch.clock.now = resume_after + timedelta(minutes=1)
    dispatch.tick()
    assert len(dispatch.starts()) == 1
    assert dispatch.admission(first)["status"] == "waiting"
    assert dispatch.admission(second)["status"] == "queued"

    # The lease loop installs a new key: exactly one continuation run in the same Session.
    inference.lease_installed(
        dispatch.lease, until + timedelta(days=1), "a-new-key", now=dispatch.clock()
    )
    dispatch.tick()
    dispatch.tick()
    starts = dispatch.starts()
    assert len(starts) == 2
    resume = starts[1]
    assert resume["session_id"] == dispatch.session_id
    # The continuation is a new native user message; the admission's instructions still apply.
    assert resume["text"] == [
        {"role": "user", "content": run_failures.continuation("budget_exhausted")}
    ]
    assert "tune the policy" not in resume["text"][0]["content"]
    assert resume["instructions"] == "answer in French"
    assert resume["idempotency_key"] == f"{first}:resume:1"
    resumed = dispatch.admission(first)
    assert resumed["status"] == "running" and resumed["run_id"] != failed_run
    assert (resumed["error"], resumed["error_class"]) == (None, None)

    # Once the resumed run is live, the message that queued behind the wait steers into it
    # (a text follow-up joins the Session's live run) instead of starting a run of its own.
    followed = dispatch.admission(second)
    assert (followed["status"], followed["run_id"]) == ("running", resumed["run_id"])
    dispatch.hermes.complete(resumed["run_id"], "done")
    dispatch.tick()
    dispatch.tick()
    assert dispatch.admission(first)["status"] == "completed"
    assert dispatch.admission(second)["status"] == "completed"
    assert len(dispatch.starts()) == 2


def test_a_resumed_run_that_fails_again_waits_again_without_a_tight_loop(dispatch):
    op_id = dispatch.say("hello")
    dispatch.start_and_fail(op_id, "HTTP 429", "rate_limit", status_code=429)
    for _ in range(5):
        dispatch.tick()
    assert len(dispatch.starts()) == 1

    dispatch.clock.now += run_failures.HOLDS["rate_limited"]
    dispatch.tick()
    assert len(dispatch.starts()) == 2
    assert dispatch.starts()[1]["idempotency_key"] == f"{op_id}:resume:1"

    # The resumed run hits the same limit: back to waiting, with a new hold.
    dispatch.api_error("rate_limit", 429)
    dispatch.hermes.fail(dispatch.admission(op_id)["run_id"], "HTTP 429")
    for _ in range(5):
        dispatch.tick()
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error_class"]) == ("waiting", "rate_limited")
    assert datetime.fromisoformat(admission["resume_after"]) > dispatch.clock()
    assert len(dispatch.starts()) == 2

    # The hold doubled: the first hold's length is no longer enough.
    assert run_failures.hold("rate_limited", 1) == 2 * run_failures.HOLDS["rate_limited"]
    dispatch.clock.now += run_failures.HOLDS["rate_limited"]
    dispatch.tick()
    assert len(dispatch.starts()) == 2
    dispatch.clock.now += run_failures.HOLDS["rate_limited"]
    dispatch.tick()
    assert [call["idempotency_key"] for call in dispatch.starts()[1:]] == [
        f"{op_id}:resume:1",
        f"{op_id}:resume:2",
    ]


def test_a_resume_waits_while_the_workspace_is_paused(dispatch):
    op_id = dispatch.say("hello")
    dispatch.start_and_fail(op_id, "HTTP 429", "rate_limit", status_code=429)
    settings.update(dispatch.store, Actor.harness(), "paused", True, cites="test")
    dispatch.clock.now += run_failures.HOLDS["rate_limited"]
    dispatch.tick()
    assert dispatch.admission(op_id)["status"] == "waiting"
    assert len(dispatch.starts()) == 1


def test_a_waiting_message_can_be_cancelled(dispatch):
    op_id = dispatch.say("hello")
    dispatch.start_and_fail(op_id, "HTTP 429", "rate_limit", status_code=429)
    dispatch.server.message_cancel({"op_id": op_id})
    dispatch.clock.now += run_failures.HOLDS["rate_limited"]
    dispatch.tick()
    assert dispatch.admission(op_id)["status"] == "cancelled"
    assert len(dispatch.starts()) == 1


def test_a_compression_rotated_session_id_still_matches_the_run(dispatch):
    """Hermes reports the agent's live session id, which compression rotates; the hook's
    task_id is the session id the run started with, and that is what matches."""
    op_id = dispatch.say("hello")
    dispatch.tick()
    run_id = dispatch.admission(op_id)["run_id"]
    record_api_error(
        dispatch.lease,
        session_id="api_9999_rotated",
        task_id=dispatch.session_id,
        reason="rate_limit",
        status_code=429,
        now=dispatch.clock(),
    )
    dispatch.hermes.fail(run_id, "agent run failed")
    dispatch.tick()
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error_class"]) == ("waiting", "rate_limited")


def test_a_cancelled_message_whose_run_fails_never_waits(dispatch):
    op_id = dispatch.say("hello")
    dispatch.tick()
    run_id = dispatch.admission(op_id)["run_id"]
    dispatch.server.message_cancel({"op_id": op_id})
    # The run failed on a rate limit before the stop took effect.
    dispatch.api_error("rate_limit", 429)
    dispatch.hermes.fail(run_id, "HTTP 429")
    dispatch.tick()
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error_class"]) == ("failed", "rate_limited")
    dispatch.clock.now += run_failures.MAX_HOLD
    dispatch.tick()
    assert len(dispatch.starts()) == 1
    kinds = {row["kind"] for row in dispatch.store.q("SELECT kind FROM events")}
    assert "client.resumed" not in kinds


def test_a_resume_refused_for_good_drops_the_earlier_class(dispatch):
    op_id = dispatch.say("hello")
    dispatch.start_and_fail(op_id, "HTTP 429", "rate_limit", status_code=429)

    def refuse(**_):
        raise RunsAuthError(
            "HTTP 401 invalid key", route="POST /p/orchestrator/v1/runs", status=401
        )

    dispatch.hermes.start = refuse
    dispatch.clock.now += run_failures.HOLDS["rate_limited"]
    dispatch.tick()
    admission = dispatch.admission(op_id)
    assert (admission["status"], admission["error"], admission["error_class"]) == (
        "failed",
        "HTTP 401 invalid key",
        None,
    )


def test_a_resume_prepares_no_attachments_again(dispatch, monkeypatch):
    """Attachments are prepared at the first start only. A resume sends the continuation, so
    preparing the admitted input again would re-ingest files the first run already staged."""
    from research_core import attachments

    prepared = []

    def prepare(message, **_):
        prepared.append(message)
        return message

    monkeypatch.setattr(attachments, "prepare", prepare)
    op_id = dispatch.say("read the attached notes")
    dispatch.start_and_fail(op_id, "HTTP 429: slow down", "rate_limit", status_code=429)
    assert dispatch.admission(op_id)["status"] == "waiting"
    dispatch.clock.now = datetime.fromisoformat(
        dispatch.admission(op_id)["resume_after"]
    ) + timedelta(seconds=1)
    dispatch.tick()
    dispatch.tick()
    assert len(dispatch.starts()) == 2
    assert dispatch.starts()[1]["idempotency_key"] == f"{op_id}:resume:1"
    assert len(prepared) == 1


def test_a_steered_follow_up_ends_with_the_run_and_only_the_start_resumes(dispatch):
    """A follow-up steered into a run that then fails on a rate limit ends ``failed`` with the
    class; the message that started the run waits and its one resume continues the whole
    conversation, the follow-up included."""
    first = dispatch.say("tune the policy")
    dispatch.tick()
    run_id = dispatch.admission(first)["run_id"]
    second = dispatch.say("and keep it small")
    dispatch.tick()
    assert dispatch.admission(second)["run_id"] == run_id  # steered, not a run of its own
    dispatch.api_error("rate_limit", 429)
    dispatch.hermes.fail(run_id, "HTTP 429")
    dispatch.tick()
    assert dispatch.admission(first)["status"] == "waiting"
    steered = dispatch.admission(second)
    assert (steered["status"], steered["error_class"]) == ("failed", "rate_limited")
    dispatch.clock.now += run_failures.MAX_HOLD
    dispatch.tick()
    assert len(dispatch.starts()) == 2
    assert dispatch.starts()[1]["idempotency_key"] == f"{first}:resume:1"


def test_has_work_counts_neither_a_waiting_message_nor_the_messages_behind_it(dispatch):
    """A budget wait lasts until the refill; it must not keep the workspace awake."""
    first = dispatch.say("tune the policy")
    dispatch.start_and_fail(first, "HTTP 403: Key limit exceeded", "billing", status_code=403)
    assert dispatch.admission(first)["status"] == "waiting"
    dispatch.say("and then submit it")
    assert dispatch.server.status()["has_work"] is False


def test_a_resume_never_binds_the_context_to_its_continuation(dispatch, tmp_path, monkeypatch):
    """The client hides the continuation only while it has no context, so the native-message
    hook must not record a resume's user message as the admission's message, even when the
    first run failed before the hook recorded the user's own message."""
    from research_core.plugin import messages as message_plugin

    monkeypatch.setattr(
        "research_core.plugin.executor.profile_env",
        lambda **_: {"IDE_WORKSPACE": str(tmp_path), "IDE_SESSION_ID": "test-harness"},
    )
    op_id = dispatch.say("hello")
    dispatch.start_and_fail(op_id, "HTTP 429", "rate_limit", status_code=429)
    dispatch.clock.now += run_failures.MAX_HOLD
    dispatch.tick()
    assert dispatch.admission(op_id)["status"] == "running"
    message_plugin.record_native_message(
        bind_origin=lambda *_: None,
        session_id=dispatch.session_id,
        conversation_history=[{"role": "user", "_row_id": 9}],
    )
    row = dispatch.store.one(
        "SELECT native_message_id FROM client_admissions WHERE op_id=?", (op_id,)
    )
    assert row["native_message_id"] is None


# --- the workspace transition contract ------------------------------------------------------


def test_work_status_reports_a_held_message_as_waiting_with_its_class(dispatch):
    op_id = dispatch.say("tune the policy")
    dispatch.start_and_fail(op_id, "HTTP 429", "rate_limit", status_code=429)
    resume_after = dispatch.admission(op_id)["resume_after"]
    work = next(
        row
        for row in admissions.work_status(dispatch.store, dispatch.lease)
        if row["attempt_id"] == op_id
    )
    assert (work["state"], work["reason"]) == ("waiting", "rate_limited")
    assert resume_after in work["trigger"]


def test_a_late_report_of_the_failed_run_does_not_hold_the_message_again(dispatch):
    """The reconcile loop can read a run's status just before the dispatcher holds its message."""
    op_id = dispatch.say("tune the policy")
    run_id = dispatch.start_and_fail(op_id, "HTTP 429", "rate_limit", status_code=429)
    held = dispatch.admission(op_id)
    dispatch.clock.now += timedelta(seconds=30)
    dispatch.server.coordinator.settle_attempt(
        dispatch.store, op_id, dispatch.hermes.status(run_id, profile="orchestrator")
    )
    assert dispatch.admission(op_id) == held


def test_a_refused_start_backs_off_and_does_not_keep_the_workspace_awake(dispatch):
    """A retryable refusal records ``retry_at``: the dispatcher waits it out instead of retrying
    on every pass, and neither it nor the message behind it counts as work."""
    dispatch.hermes.set_busy(True)
    first = dispatch.say("tune the policy")
    dispatch.tick()
    row = dispatch.store.one(
        "SELECT status,retry_at FROM client_admissions WHERE op_id=?", (first,)
    )
    assert row["status"] == "queued" and row["retry_at"] is not None
    attempts = len([c for c in dispatch.hermes.calls if c[0] == "start"])
    dispatch.say("and then submit it")
    dispatch.tick()
    dispatch.tick()
    assert len([c for c in dispatch.hermes.calls if c[0] == "start"]) == attempts
    status = dispatch.server.status()
    assert status["has_work"] is False and status["waiting_on"] == []
    # Once the backoff has passed and the gateway takes runs, the message starts.
    dispatch.hermes.set_busy(False)
    with dispatch.store.tx(Actor.user("test")) as tx:
        tx.execute(
            "UPDATE client_admissions SET retry_at=? WHERE op_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), first),
        )
    dispatch.tick()
    started = dispatch.store.one(
        "SELECT status,retry_at FROM client_admissions WHERE op_id=?", (first,)
    )
    assert (started["status"], started["retry_at"]) == ("running", None)
    assert f"admission:{first}:running" in dispatch.server.status()["waiting_on"]


def test_an_admission_awaiting_resolution_does_not_keep_the_workspace_awake(dispatch):
    """Resolution waits on the user, like a budget hold, so it must not hold the workspace up."""
    op_id = dispatch.say("tune the policy")
    assert f"admission:{op_id}:queued" in dispatch.server.status()["waiting_on"]
    with dispatch.store.tx(Actor.user("test")) as tx:
        tx.execute(
            "UPDATE client_admissions SET resolution_required=1,error='needs resolution'"
            " WHERE op_id=?",
            (op_id,),
        )
    status = dispatch.server.status()
    assert f"admission:{op_id}:queued" not in status["waiting_on"]
    assert status["blocked_executions"][0]["op_id"] == op_id


def test_a_finished_legacy_admission_flagged_for_resolution_is_settled(dispatch):
    """0006 flagged legacy admissions whatever their status. A finished one has nothing to
    resolve: the dispatcher's reconciliation clears it, so it neither reserves Researcher
    capacity nor blocks sleep."""
    from research_core.store import sessions

    actor = Actor.user("test")
    sessions.register(
        dispatch.store,
        actor,
        session_id="worker-1",
        profile="researcher",
        kind="task",
        coworld_id="world",
        task_id=1,
    )
    op_id = admissions.enqueue(
        dispatch.store,
        actor,
        key="legacy",
        session_id="worker-1",
        profile="researcher",
        text="Repair",
        origin="assignment",
    )
    with dispatch.store.tx(actor) as tx:
        tx.execute(
            "UPDATE client_admissions SET status='failed',dispatch_started=1,"
            "resolution_required=1,error='legacy admission requires reconciliation'"
            " WHERE op_id=?",
            (op_id,),
        )
    assert admissions.reserved_workers(dispatch.store) == 1
    dispatch.tick()
    row = dispatch.store.one(
        "SELECT status,resolution_required FROM client_admissions WHERE op_id=?", (op_id,)
    )
    assert (row["status"], row["resolution_required"]) == ("failed", 0)
    assert admissions.reserved_workers(dispatch.store) == 0
    status = dispatch.server.status()
    assert status["blocked_executions"] == [] and status["has_work"] is False


# --- Hermes's AGENTS.md write prompt --------------------------------------------------------


def _instruction_prompt(prompt_id: str) -> dict:
    return {
        "kind": "approval",
        "prompt_id": prompt_id,
        "execution_generation": 1,
        "command": "<write to AGENTS.md>",
        "description": f"{serve.PROTECTED_INSTRUCTION_PROMPT}AGENTS.md. These files steer ...",
        "choices": ["once", "deny"],
    }


@pytest.mark.parametrize(("kind", "approved"), [("wake", True), ("chat", False)])
def test_an_agents_md_prompt_is_approved_only_where_no_person_can_answer(dispatch, kind, approved):
    """A wake or Task run approves Hermes's AGENTS.md prompt at the next poll; a chat leaves it
    for the user, as it does every other approval."""
    with dispatch.store.tx(Actor.harness()) as tx:
        tx.execute("UPDATE sessions SET kind=? WHERE id=?", (kind, dispatch.session_id))
    op_id = dispatch.say("set up the lab")
    dispatch.tick()
    run = dispatch.hermes.runs[dispatch.admission(op_id)["run_id"]]
    other = {**_instruction_prompt("p2"), "description": "Run a dangerous command"}
    run.pending_controls = [_instruction_prompt("p1"), other]
    dispatch.tick()
    answered = [c["prompt_id"] for m, c in dispatch.hermes.calls if m == "approve_once"]
    assert answered == (["p1"] if approved else [])
    assert [c["prompt_id"] for c in run.pending_controls] == (["p2"] if approved else ["p1", "p2"])
