"""One intent is applied once, with no id from the model, through the real tool and coordinator.

The tools run on their own store connections, as the plugin's do, against a real SQLite workspace.
What a retry looks like comes from the pinned Hermes: Hermes never runs a call again by itself. A
call whose reply was lost comes back as the model's new call, with a new tool-call id and the same
arguments, after an "effect is UNKNOWN" notice if the gateway restarted (agent/replay_cleanup.py).
Two identical calls in one response never reach a tool (``_deduplicate_tool_calls``).
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType

import pytest

from research_core import transitions
from research_core.store import db, goals, sessions, tasks
from research_core.tools.base import ToolContext, run_tool
from tests.test_coordinator_transitions import assigned
from tests.watcher._support import HARNESS, USER, coordinator, make_ctx, make_task

MESSAGE = {"action": "message", "task": "T-1", "text": "Use the practice division."}
CREATE = {
    "action": "create",
    "type": "maintenance",
    "title": "Repair",
    "description": "Repair the build",
}


class ProcessDied(BaseException):
    """The gateway stops between accepting a command and returning its reply."""


def call(ctx, args, *, session="planner", role="orchestrator", epoch=None, tool="task"):
    """One tool call from a fresh process: its own context and store connections."""
    env = {
        "IDE_WORKSPACE": str(ctx.ws.root),
        "IDE_COWORLD_ID": ctx.coworld.coworld_id,
        "IDE_SESSION_ID": session,
    }
    if epoch is not None:
        env["IDE_LEASE_EPOCH"] = str(epoch)
    tool_ctx = ToolContext.from_env(role, env)
    try:
        return json.loads(run_tool(tool, args, tool_ctx))
    finally:
        tool_ctx.store.close()
        tool_ctx.wstore.close()


def apply(ctx):
    transitions.apply(ctx, "coordinator:test")


def call_applied(ctx, args, **kwargs):
    """A call with the coordinator working: the reply carries the finished result."""
    version = db.change_version()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, ctx, args, **kwargs)
        db.wait_for_change(version, 5)
        apply(ctx)
        return future.result(timeout=20)


def count(ctx, table):
    return ctx.store.one(f"SELECT count(*) FROM {table}")[0]


def messages(ctx):
    return ctx.store.q("SELECT * FROM task_messages WHERE direction='to_researcher'")


@pytest.fixture
def ctx(tmp_path):
    context = make_ctx(tmp_path)
    for session_id in ("planner", "other"):
        sessions.register(
            context.wstore,
            HARNESS,
            session_id=session_id,
            profile="orchestrator",
            kind="wake",
            coworld_id=context.coworld.coworld_id,
        )
    return context


@pytest.fixture
def no_wait(monkeypatch):
    """The coordinator is away: a call returns ``transition_pending`` at once."""
    monkeypatch.setattr(transitions, "REQUEST_WAIT_SECONDS", 0)


def die_before_the_reply(monkeypatch):
    def die(*_):
        raise ProcessDied()

    monkeypatch.setattr(transitions, "_await_result", die)


def test_a_repeat_after_a_pending_reply_joins_the_command(ctx, no_wait):
    make_task(ctx, type="maintenance")
    first = call(ctx, CREATE)
    assert first["kind"] == "transition_pending"
    assert "will not do it twice" in first["next"]
    assert "task.command" in first["next"]
    second = call(ctx, CREATE)
    assert second["command_id"] == first["command_id"]
    apply(ctx)
    finished = call(ctx, CREATE)
    assert finished["ok"] and finished["command_id"] == first["command_id"]
    assert count(ctx, "tasks") == 2  # make_task's and this one


def test_a_repeat_through_the_other_form_of_the_tool_joins_the_command(ctx, no_wait):
    """An action with a tool of its own: both forms are the same intent, in either field order."""
    grouped = {"action": "propose", "type": "maintenance", "title": "Repair", "description": "Fix"}
    first = call(ctx, grouped, role="researcher")
    flat = {"description": "Fix", "title": "Repair", "type": "maintenance"}
    second = call(ctx, flat, tool="task_propose", role="researcher")
    assert second["command_id"] == first["command_id"]
    apply(ctx)
    assert (
        call(ctx, flat, tool="task_propose", role="researcher")["command_id"] == first["command_id"]
    )
    assert count(ctx, "tasks") == 1


def test_an_explicit_operation_id_joins_the_command_through_the_other_form(ctx, no_wait):
    """A chat frozen on the old schema sent one; its tools then refresh and it retries flat."""
    fields = {"type": "maintenance", "title": "Repair", "description": "Fix", "operation_id": "op"}
    first = call(ctx, {"action": "propose", **fields}, role="researcher")
    assert (
        call(ctx, fields, tool="task_propose", role="researcher")["command_id"]
        == first["command_id"]
    )
    apply(ctx)
    assert count(ctx, "tasks") == 1


def test_a_repeat_after_the_gateway_died_returns_the_earlier_result(ctx, monkeypatch):
    with monkeypatch.context() as patch:
        die_before_the_reply(patch)
        with pytest.raises(ProcessDied):
            call(ctx, CREATE)
    apply(ctx)  # the coordinator finishes the accepted command while the gateway restarts
    retried = call(ctx, CREATE)
    assert retried["ok"] and retried["record"]["id"] == "T-1"
    assert "did nothing" in retried["repeat_of_earlier_call"]
    assert count(ctx, "tasks") == 1


def test_a_refusal_found_by_a_retry_is_not_called_done_and_the_next_call_runs(ctx, monkeypatch):
    args = {**CREATE, "type": "research", "goal": "G-1"}
    with monkeypatch.context() as patch:
        die_before_the_reply(patch)
        with pytest.raises(ProcessDied):
            call(ctx, args)
    apply(ctx)
    retried = call(ctx, args)
    assert retried["transition_state"] == "refused" and "repeat_of_earlier_call" not in retried
    goals.create(ctx.store, HARNESS, title="Goal", end_state="Done", reason="Asked")
    assert call_applied(ctx, args)["record"]["id"] == "T-1"


def test_an_interrupt_ends_the_wait_and_the_retry_joins_the_command(ctx, monkeypatch):
    interrupt = ModuleType("tools.interrupt")
    interrupt.is_interrupted = lambda: True
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "tools", ModuleType("tools"))
        patch.setitem(sys.modules, "tools.interrupt", interrupt)
        started = time.monotonic()
        interrupted = call(ctx, CREATE)
        assert time.monotonic() - started < 2
    assert interrupted["kind"] == "transition_pending"
    apply(ctx)
    retried = call(ctx, CREATE)  # Hermes has cleared the interrupt by now
    assert retried["command_id"] == interrupted["command_id"]
    assert "did nothing" in retried["repeat_of_earlier_call"]
    assert count(ctx, "tasks") == 1


def test_an_interrupted_retry_that_finds_the_result_leaves_the_next_retry_protected(
    ctx, monkeypatch
):
    with monkeypatch.context() as patch:
        die_before_the_reply(patch)
        with pytest.raises(ProcessDied):
            call(ctx, CREATE)
    apply(ctx)
    interrupt = ModuleType("tools.interrupt")
    interrupt.is_interrupted = lambda: True
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "tools", ModuleType("tools"))
        patch.setitem(sys.modules, "tools.interrupt", interrupt)
        assert call(ctx, CREATE)["ok"]
    assert call(ctx, CREATE)["record"]["id"] == "T-1"
    assert count(ctx, "tasks") == 1


def test_a_call_that_waited_long_for_the_lock_leaves_its_retry_protected(ctx, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(transitions, "HERMES_INTERRUPT_GRACE_SECONDS", 0)
        first = call_applied(ctx, CREATE)
    retried = call(ctx, CREATE)
    assert retried["command_id"] == first["command_id"]
    assert "did nothing" in retried["repeat_of_earlier_call"]
    assert count(ctx, "tasks") == 1


def test_a_repeat_that_joins_a_pending_command_and_sees_it_finish_says_so(ctx, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(transitions, "REQUEST_WAIT_SECONDS", 0)
        call(ctx, CREATE)
    joined = call_applied(ctx, CREATE)
    assert joined["ok"] and "did nothing" in joined["repeat_of_earlier_call"]
    assert count(ctx, "tasks") == 1


def test_a_repeat_while_the_coordinator_died_mid_transaction_applies_once(ctx, monkeypatch):
    class CoordinatorDied(BaseException):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(transitions, "REQUEST_WAIT_SECONDS", 0)
        accepted = call(ctx, CREATE)
    original = db.Tx.__exit__

    def die_at_commit(tx, *exc):
        if tx.store.one("SELECT 1 FROM events WHERE kind='transition.applied'"):
            original(tx, CoordinatorDied, CoordinatorDied(), None)
            raise CoordinatorDied()
        return original(tx, *exc)

    with monkeypatch.context() as patch:
        patch.setattr(db.Tx, "__exit__", die_at_commit)
        with pytest.raises(CoordinatorDied):
            apply(ctx)
    assert count(ctx, "tasks") == 0
    with monkeypatch.context() as patch:
        patch.setattr(transitions, "REQUEST_WAIT_SECONDS", 0)
        assert call(ctx, CREATE)["command_id"] == accepted["command_id"]
    apply(ctx)
    assert call(ctx, CREATE)["record"]["id"] == "T-1"
    assert count(ctx, "tasks") == 1


def test_concurrent_identical_calls_make_one_command(ctx, no_wait):
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(lambda _: call(ctx, CREATE), range(6)))
    assert len({reply["command_id"] for reply in replies}) == 1
    assert count(ctx, "transition_commands") == 1
    apply(ctx)
    assert count(ctx, "tasks") == 1


def test_two_identical_messages_on_purpose_are_both_sent(ctx):
    make_task(ctx, type="maintenance")
    first = call_applied(ctx, MESSAGE)
    second = call_applied(ctx, MESSAGE)
    assert first["ok"] and second["ok"]
    assert first["command_id"] != second["command_id"]
    assert "repeat_of_earlier_call" not in second
    assert len(messages(ctx)) == 2


def test_a_retry_after_reading_the_result_is_a_new_intent(ctx, no_wait):
    make_task(ctx, type="maintenance")
    pending = call(ctx, MESSAGE)
    apply(ctx)
    read = call(ctx, {"action": "command", "command_id": pending["command_id"]})
    assert read["ok"] and read["transition_state"] == "applied"
    again = call(ctx, MESSAGE)
    assert again["command_id"] != pending["command_id"]
    apply(ctx)
    assert len(messages(ctx)) == 2


def test_the_same_call_runs_again_after_a_refusal(ctx):
    refused_args = {**CREATE, "type": "research", "goal": "G-1"}
    refused = call_applied(ctx, refused_args)
    assert refused["ok"] is False and refused["transition_state"] == "refused"
    goals.create(ctx.store, HARNESS, title="Goal", end_state="Done", reason="Asked")
    retried = call_applied(ctx, refused_args)
    assert retried["ok"] and retried["record"]["id"] == "T-1"
    assert retried["command_id"] != refused["command_id"]


def test_another_session_is_never_joined(ctx, no_wait):
    first = call(ctx, CREATE)
    other = call(ctx, CREATE, session="other")
    assert other["command_id"] != first["command_id"]


def test_padding_does_not_change_the_intent(ctx, no_wait):
    first = call(ctx, CREATE)
    padded = call(ctx, {**CREATE, "reasoning": "", "goal": None, "operation_id": ""})
    assert padded["command_id"] == first["command_id"]


def test_an_explicit_operation_id_keeps_its_meaning(ctx, no_wait):
    named = {**CREATE, "operation_id": "from-a-frozen-schema"}
    first = call(ctx, named)
    apply(ctx)
    # Reuse returns the same receipt however often it was already read.
    assert call(ctx, named)["record"]["id"] == "T-1"
    assert call(ctx, named)["command_id"] == first["command_id"]
    # A generated identity never joins it, and a different payload is refused.
    assert call(ctx, CREATE)["command_id"] != first["command_id"]
    refused = call(ctx, {**named, "title": "Something else"})
    assert refused["ok"] is False and "different transition intent" in refused["error"]


def test_a_worker_report_retried_after_a_lost_reply_applies_once(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    row = assigned(ctx)
    worker = {"session": row["session_id"], "role": "researcher"}
    args = {"action": "ask", "task": "T-1", "question": "Which division?"}
    # The real dispatcher started the worker, so its reports have authority.
    epoch = ctx.store.one("SELECT lease_epoch FROM tasks WHERE id=1")[0]
    with monkeypatch.context() as patch:
        die_before_the_reply(patch)
        with pytest.raises(ProcessDied):
            call(ctx, args, epoch=epoch, **worker)
    apply(ctx)
    retried = call(ctx, args, epoch=epoch, **worker)
    assert retried["ok"] and "repeat_of_earlier_call" in retried
    assert (
        ctx.store.one("SELECT count(*) FROM task_messages WHERE direction='to_orchestrator'")[0]
        == 1
    )


# --- task.assign: no id and no revision from the model ----------------------------------------


ASSIGN = {
    "action": "assign",
    "task": "T-1",
    "profile": "researcher",
    "context": "Repair",
    "rationale": "Fit",
}


def edit_as_user(ctx):
    tasks.update(ctx.store, USER, 1, title="Repair the other build")


def test_assign_needs_no_read_when_the_session_saw_no_older_revision(ctx):
    make_task(ctx, type="maintenance")
    assert call_applied(ctx, ASSIGN)["kind"] == "reserved"


def test_assign_is_refused_when_the_task_changed_after_the_session_read_it(ctx):
    make_task(ctx, type="maintenance")
    assert call(ctx, {"action": "read", "task": "T-1"})["ok"]
    ctx.clock.advance(1)
    edit_as_user(ctx)
    refused = call_applied(ctx, ASSIGN)
    assert refused["ok"] is False and "read it with task.read, then assign" in refused["error"]
    assert call(ctx, {"action": "read", "task": "T-1"})["ok"]
    assert call_applied(ctx, ASSIGN)["kind"] == "reserved"


def test_assign_after_the_sessions_own_write_needs_no_second_read(ctx):
    tasks.propose(
        ctx.store, HARNESS, type="maintenance", title="Repair", description="Repair the build"
    )
    listed = call(ctx, {"action": "list"})["records"][0]
    ctx.clock.advance(1)
    approved = call_applied(ctx, {"action": "approve", "task": "T-1"})
    assert approved["record"]["updated"] != listed["updated"]
    assert call_applied(ctx, ASSIGN)["kind"] == "reserved"


def test_assign_after_reading_the_sessions_own_write_from_a_pending_reply(ctx, no_wait):
    tasks.propose(
        ctx.store, HARNESS, type="maintenance", title="Repair", description="Repair the build"
    )
    assert call(ctx, {"action": "list"})["ok"]
    ctx.clock.advance(1)
    pending = call(ctx, {"action": "approve", "task": "T-1"})
    apply(ctx)
    assert call(ctx, {"action": "command", "command_id": pending["command_id"]})["ok"]
    call(ctx, ASSIGN)
    apply(ctx)
    assert ctx.wstore.one("SELECT count(*) FROM task_assignments")[0] == 1


def test_a_read_showing_nothing_new_does_not_take_the_write_lock(ctx):
    make_task(ctx, type="maintenance")
    assert call(ctx, {"action": "read", "task": "T-1"})["ok"]
    blocker = ctx.store
    blocker.conn.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        assert call(ctx, {"action": "read", "task": "T-1"})["ok"]
        assert time.monotonic() - started < 2
    finally:
        blocker.conn.execute("ROLLBACK")


def test_another_sessions_read_of_a_receipt_leaves_the_retry_protected(ctx, monkeypatch):
    make_task(ctx, type="maintenance")
    with monkeypatch.context() as patch:
        die_before_the_reply(patch)
        with pytest.raises(ProcessDied):
            call(ctx, MESSAGE)
    command_id = ctx.store.one("SELECT id FROM transition_commands")["id"]
    apply(ctx)
    assert call(ctx, {"action": "command", "command_id": command_id}, session="other")["ok"]
    assert call(ctx, MESSAGE)["command_id"] == command_id
    assert len(messages(ctx)) == 1


def test_a_result_the_session_never_got_does_not_count_as_seen(ctx, no_wait):
    make_task(ctx, type="maintenance")
    assert call(ctx, {"action": "read", "task": "T-1"})["ok"]
    assert call(ctx, MESSAGE)["kind"] == "transition_pending"
    ctx.clock.advance(1)
    edit_as_user(ctx)
    apply(ctx)
    call(ctx, ASSIGN)
    apply(ctx)
    assert ctx.wstore.one("SELECT count(*) FROM task_assignments")[0] == 0
    assert (
        "read it with task.read"
        in ctx.store.q("SELECT result FROM transition_commands")[-1]["result"]
    )


def test_an_accepted_assign_keeps_the_revision_it_was_accepted_with(ctx, no_wait):
    make_task(ctx, type="maintenance")
    assert call(ctx, {"action": "read", "task": "T-1"})["ok"]
    assert call(ctx, ASSIGN)["kind"] == "transition_pending"
    ctx.clock.advance(1)
    edit_as_user(ctx)
    assert call(ctx, {"action": "read", "task": "T-1"})["ok"]
    apply(ctx)
    assert ctx.wstore.one("SELECT count(*) FROM task_assignments")[0] == 0


def test_a_numeric_operation_id_from_a_frozen_schema_is_still_a_string(ctx, no_wait):
    first = call(ctx, {**CREATE, "operation_id": 7})
    assert first["kind"] == "transition_pending"
    assert (
        json.loads(ctx.store.one("SELECT args FROM transition_commands")["args"])["operation_id"]
        == "7"
    )
    refused = call(ctx, {**CREATE, "operation_id": ["a"]})
    assert refused["ok"] is False and "operation_id must be string" in refused["error"]


def test_a_read_of_a_delivered_receipt_does_not_take_the_write_lock(ctx, no_wait):
    pending = call(ctx, CREATE)
    apply(ctx)
    read = {"action": "command", "command_id": pending["command_id"]}
    assert call(ctx, read)["ok"]
    ctx.store.conn.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        assert call(ctx, read)["ok"]
        assert time.monotonic() - started < 2
    finally:
        ctx.store.conn.execute("ROLLBACK")


def test_a_planning_wake_counts_as_having_seen_its_tasks(ctx):
    make_task(ctx, type="maintenance")
    report = coordinator(ctx).plan_assignments(ctx)
    planner = ctx.wstore.one(
        "SELECT session_id FROM client_admissions WHERE native_key=?", (report["planning"],)
    )
    ctx.clock.advance(1)
    edit_as_user(ctx)
    refused = call_applied(ctx, ASSIGN, session=planner["session_id"])
    assert refused["ok"] is False and "read it with task.read" in refused["error"]


def test_a_lost_assign_reply_reserves_once_and_a_second_assign_is_refused(ctx, monkeypatch):
    make_task(ctx, type="maintenance")
    with monkeypatch.context() as patch:
        die_before_the_reply(patch)
        with pytest.raises(ProcessDied):
            call(ctx, ASSIGN)
    apply(ctx)
    retried = call(ctx, ASSIGN)
    assert retried["kind"] == "reserved" and "repeat_of_earlier_call" in retried
    assert ctx.wstore.one("SELECT count(*) FROM task_assignments")[0] == 1
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 1
    second = call_applied(ctx, {**ASSIGN, "rationale": "Reworded"})
    assert second["ok"] is False and "already has an assignment" in second["error"]
    assert ctx.wstore.one("SELECT count(*) FROM client_admissions")[0] == 1


def test_values_from_a_frozen_schema_still_count(ctx):
    task = make_task(ctx, type="maintenance")
    stale = call_applied(
        ctx, {**ASSIGN, "task_updated": "2020-01-01T00:00:00Z", "assignment_id": "named"}
    )
    assert stale["ok"] is False and "read it with task.read, then assign" in stale["error"]
    reserved = call_applied(
        ctx, {**ASSIGN, "task_updated": task["updated"], "assignment_id": "named"}
    )
    assert reserved["kind"] == "reserved"
    row = ctx.wstore.one("SELECT native_key FROM client_admissions")
    assert row["native_key"] == "assignment:named"
