"""Durable requests to the coordinator; tools carry intent, never scheduler authority.

The local function call applies one domain command and acknowledges it in the same transaction.
Cross-process delivery uses SQLite and bounded polling: no native runtime hook is required.
Stdlib only, because submit runs inside the pinned native interpreter.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from research_core.store import admissions
from research_core.store._common import parse_iso
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.store.errors import StoreError
from research_core.tools.base import ToolCall, ToolContext, ToolError, ToolSpec, tool

NOT_THE_HOLDER = (
    "This run no longer holds the Task: it was stopped, released or given to another run. "
    "Nothing was changed; end your run."
)
REQUEST_WAIT_SECONDS = 15
# How long Hermes waits for a tool thread after an interrupt before it abandons the call and,
# later, clears the interrupt (agent/tool_executor.py).
HERMES_INTERRUPT_GRACE_SECONDS = 3
PENDING_NEXT_STEP = (
    "Accepted, not done yet. Read the result with task.command and this command_id, or call again "
    "with the same fields: that will not do it twice."
)
REPEAT_NOTE = (
    "This call did nothing: an earlier identical call already did it, once. If you are retrying, "
    "you are done. If you want it done a second time, make this same call again and it will run."
)

CommandState = Literal["pending", "applied", "refused"]


@dataclass(frozen=True)
class CommandReceipt:
    state: CommandState
    payload: dict

    @classmethod
    def parse(cls, payload: dict) -> CommandReceipt:
        state = payload["transition_state"]
        if state not in {"pending", "applied", "refused"}:
            raise ValueError("unknown command receipt state")
        return cls(state, payload)


@dataclass
class CommandApplication:
    """Convert expected domain refusals after rollback into durable terminal results.

    Unexpected exceptions propagate. The inner atomic transaction must exit first.
    """

    store: Store
    command_id: str

    def __enter__(self) -> CommandApplication:
        return self

    def __exit__(self, exc_type, error, tb) -> bool:
        if isinstance(error, Exception) and not isinstance(error, (ToolError, StoreError)):
            row = self.store.one(
                "SELECT attempts FROM transition_commands WHERE id=?", (self.command_id,)
            )
            with self.store.tx(Actor.harness()) as tx:
                retry_at = (
                    parse_iso(tx.at) + timedelta(seconds=min(60, 2 ** min(row["attempts"], 6)))
                ).isoformat()
                detail = f"{type(error).__name__}: {error}"
                tx.execute(
                    "UPDATE transition_commands SET last_error=?,retry_at=? WHERE id=?",
                    (detail, retry_at, self.command_id),
                )
                tx.event(
                    "transition.retry_waiting",
                    self.command_id,
                    {"error": detail, "retry_at": retry_at},
                )
            return False
        if not isinstance(error, (ToolError, StoreError)):
            return False
        with self.store.tx(Actor.harness()) as tx:
            tx.execute(
                "UPDATE transition_commands SET result=?,completed_at=? WHERE id=?",
                (
                    json.dumps(
                        {
                            "ok": False,
                            "kind": "transition_refused",
                            "problems": error.problems,
                            "error": str(error),
                        }
                    ),
                    tx.at,
                    self.command_id,
                ),
            )
            tx.event("transition.refused", self.command_id, {"problems": error.problems})
        return True


def command_identity(ctx: ToolContext, tool: str, operation_id: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"transition:{ctx.coworld().coworld_id}:{ctx.actor}:{tool}:{operation_id}",
        )
    )


def receipt(store: Store, command_id: str) -> dict:
    row = store.one("SELECT * FROM transition_commands WHERE id=?", (command_id,))
    if row is None:
        raise StoreError(["unknown transition command"])
    if row["result"] is not None:
        result = json.loads(row["result"])
        state = "applied" if result["ok"] else {"transition_refused": "refused"}[result["kind"]]
        return {**result, "command_id": command_id, "transition_state": state}
    return {
        "transition_state": "pending",
        "kind": "transition_pending",
        "command_id": command_id,
        "owner": row["owner_instance"] or "workspace coordinator",
        "reason": row["last_error"] or "awaiting coordinator application",
        "trigger": "coordinator retries after backoff"
        if row["retry_at"]
        else "coordinator processes this durable command",
        "retry_at": row["retry_at"],
        "attempts": row["attempts"],
    }


def submit(call: ToolCall) -> dict:
    """Hand a tool call to the coordinator under the operation id it names, or a generated one."""
    operation_id = call.get("operation_id")
    if operation_id:  # a chat whose frozen schema still lists the field
        result = request(call.ctx, call.spec.name, call.args, operation_id)
        if CommandReceipt.parse(result).state != "pending":
            note_delivery(call.ctx, result)
        return result
    return request_generated(call.ctx, call.spec.name, call.args)


def request(ctx: ToolContext, tool: str, args: dict, operation_id: str) -> dict:
    command_id = command_identity(ctx, tool, operation_id)
    payload = json.dumps(args, sort_keys=True)
    # Acceptance and execution retirement share the admission gate. A persisted report
    # retains its acceptance authority if its worker exits before application.
    with admissions.dispatch_lock(ctx.wstore), ctx.store.tx(ctx.actor) as tx:
        prior = tx.one("SELECT * FROM transition_commands WHERE id=?", (command_id,))
        if prior is not None:
            if (
                prior["tool"],
                prior["args"],
                prior["role"],
                prior["actor"],
                prior["actor_epoch"],
            ) != (
                tool,
                payload,
                ctx.role,
                str(ctx.actor),
                ctx.actor.epoch,
            ):
                raise StoreError(["operation_id already belongs to a different transition intent"])
        else:
            _accept(tx, ctx, tool, args, command_id, payload)
    return _await_result(ctx, command_id)


def request_generated(ctx: ToolContext, tool: str, args: dict) -> dict:
    """Apply one intent once without an id from the model.

    A tool-call id cannot do it: a call the model repeats after a lost or pending reply has a
    new one. Arguments alone cannot either: the same message may be meant twice, and the same
    call must run again after a refusal. What tells them apart is whether the session has been
    given the first call's finished result. Until then an identical call is a retry and joins
    that command; afterwards it is a new intent. A retry that finds the work already done says
    so, in case it was in fact meant as a second one.
    """
    fields = {name: value for name, value in args.items() if value not in (None, "")}
    fields.pop("operation_id", None)
    payload = json.dumps(fields, sort_keys=True)
    started = time.monotonic()
    with admissions.dispatch_lock(ctx.wstore), ctx.store.tx(ctx.actor) as tx:
        earlier = tx.one(
            "SELECT id FROM transition_commands WHERE reply_seen_at IS NULL AND tool=?"
            " AND args=? AND role=? AND actor=? AND actor_epoch IS ? ORDER BY rowid DESC LIMIT 1",
            (tool, payload, ctx.role, str(ctx.actor), ctx.actor.epoch),
        )
        if earlier is not None:
            command_id = earlier["id"]
        else:
            command_id = command_identity(ctx, tool, f"generated:{uuid4()}")
            _accept(tx, ctx, tool, fields, command_id, payload)
    # A lock cannot be waited on and an interrupt watched at once, and by the end of a long wait
    # Hermes may have abandoned this call and cleared its interrupt. Then the reply may be lost.
    maybe_abandoned = time.monotonic() - started >= HERMES_INTERRUPT_GRACE_SECONDS
    result = _await_result(ctx, command_id)
    state = CommandReceipt.parse(result).state
    if state == "pending":
        return {**result, "next": PENDING_NEXT_STEP}
    if state == "applied" and earlier is not None:
        result["repeat_of_earlier_call"] = REPEAT_NOTE
    if not maybe_abandoned:
        note_delivery(ctx, result)
    return result


def read_command(ctx: ToolContext, command_id: str) -> dict:
    """``task.command``: a finished result read here reaches the session."""
    result = receipt(ctx.store, command_id)
    if CommandReceipt.parse(result).state != "pending":
        note_delivery(ctx, result)
    return result


def note_delivery(ctx: ToolContext, result: dict) -> None:
    """Record that the session that issued this finished command is being given its result: an
    identical call is a new intent from now on, and the Task revisions it shows count as seen.
    Both commit together, so neither can fail after the other. Another session's read changes
    nothing, and neither does repeating a read of a delivered command. Under an interrupt Hermes
    may discard this reply, so it stays unmarked: at worst a deliberate repeat gets REPEAT_NOTE."""
    command_id = result["command_id"]
    command = ctx.store.one(
        "SELECT tool,reply_seen_at FROM transition_commands WHERE id=? AND actor=?",
        (command_id, str(ctx.actor)),
    )
    if command is None or command["reply_seen_at"] is not None or _hermes_interrupted():
        return
    from research_core.tools import task

    with ctx.store.atomic(ctx.actor) as tx:
        tx.execute("UPDATE transition_commands SET reply_seen_at=? WHERE id=?", (tx.at, command_id))
        if command["tool"] == "task":
            task.remember_seen_revisions(ctx, result)


def _accept(tx, ctx: ToolContext, tool: str, args: dict, command_id: str, payload: str) -> None:
    from research_core.store import tasks
    from research_core.tools import _args as arguments
    from research_core.watcher.wakes import question_for_session

    if (
        ctx.role == "orchestrator"
        and ctx.actor.is_session
        and (tool != "task" or args["action"] != "answer")
        and question_for_session(ctx.store, ctx.actor.id) is not None
    ):
        raise ToolError("This wake is scoped to answering or escalating its bound question.")

    issuer_attempt_id = None
    task_updated = None
    if tool == "task" and args["action"] == "assign" and not args.get("task_updated"):
        # Fixed now: what the session sees while this waits must not change what it chose from.
        task_updated = tasks.revision_for_assignment(
            ctx.store,
            ctx.hermes_session_id(),
            arguments.record_number(args["task"], "task", "task"),
        )
    if (
        ctx.role == "researcher"
        and tool == "task"
        and args["action"] in {"complete", "wait", "ask", "release", "propose_submission"}
    ):
        task_id = arguments.record_number(args["task"], "task", "task")
        tasks.require_holder(ctx.store, task_id, ctx.actor)
        assignment = ctx.wstore.one(
            "SELECT a.current_attempt_id,a.state,execution.status FROM task_assignments a"
            " JOIN client_admissions execution ON execution.op_id=a.current_attempt_id"
            " WHERE a.session_id=? AND a.task_id=?",
            (ctx.actor.id, task_id),
        )
        if (
            assignment is None
            or assignment["state"] != "held"
            or assignment["status"] not in {"queued", "running"}
        ):
            raise ToolError(NOT_THE_HOLDER)
        issuer_attempt_id = assignment["current_attempt_id"]
    tx.execute(
        "INSERT INTO transition_commands(id,tool,args,role,actor,actor_epoch,"
        "issuer_attempt_id,created_at,task_updated) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            command_id,
            tool,
            payload,
            ctx.role,
            str(ctx.actor),
            ctx.actor.epoch,
            issuer_attempt_id,
            tx.at,
            task_updated,
        ),
    )
    tx.event("transition.requested", command_id, {"tool": tool})


def _hermes_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted
    except ImportError:  # outside Hermes: tests, the CLI and workspace-executor subprocesses
        return False
    return is_interrupted()


def _await_result(ctx: ToolContext, command_id: str) -> dict:
    """The command's result, or its pending receipt after a short wait.

    An interrupt ends the wait at once: Hermes abandons a thread still running after a short grace
    period and tells the model the call was cancelled, and it clears the interrupt later, so this
    thread must not stay to record a finished result nobody will get. A pending reply marks nothing.
    """
    deadline = time.monotonic() + REQUEST_WAIT_SECONDS
    wait = threading.Event()
    while time.monotonic() < deadline and not _hermes_interrupted():
        result = receipt(ctx.store, command_id)
        if CommandReceipt.parse(result).state != "pending":
            return result
        wait.wait(0.1)
    return receipt(ctx.store, command_id)


def apply(ctx, instance_id: str) -> None:
    """Only the lifetime coordinator calls this. Recovery reuses the exact command identity."""
    if ctx.store is None:
        return
    from research_core.tools import task

    store = ctx.store
    for command in store.q("SELECT * FROM transition_commands WHERE result IS NULL ORDER BY rowid"):
        if command["retry_at"] is not None and parse_iso(store.now()) < parse_iso(
            command["retry_at"]
        ):
            continue
        with store.tx(Actor.harness()) as tx:
            tx.execute(
                "UPDATE transition_commands SET attempts=attempts+1,owner_instance=?,"
                "last_error=NULL,retry_at=NULL WHERE id=?",
                (instance_id, command["id"]),
            )
        actor = replace(Actor.parse(command["actor"]), epoch=command["actor_epoch"])
        tool_ctx = ToolContext(
            role=command["role"],
            actor=actor,
            workspace=ctx.ws,
            env={
                **ctx.env,
                "IDE_SESSION_ID": actor.id if actor.is_session else "",
                "IDE_ACTOR": str(actor),
                "IDE_COWORLD_ID": ctx.coworld.coworld_id,
                "IDE_TRANSITION_ID": command["id"],
                "IDE_TRANSITION_ATTEMPT_ID": command["issuer_attempt_id"] or "",
            },
            coordinator_instance=instance_id,
        )
        tool_ctx.wstore = ctx.wstore
        tool_ctx.store = store
        session = ctx.wstore.one("SELECT profile FROM sessions WHERE id=?", (actor.id,))
        if session is not None:
            tool_ctx.env = {**tool_ctx.env, "HERMES_PROFILE_NAME": session["profile"]}
        args = json.loads(command["args"])
        if command["task_updated"]:
            args["task_updated"] = command["task_updated"]
        with CommandApplication(store, command["id"]):
            # Lock order is admission lock, then Coworld writer, then workspace writer, everywhere;
            # ``store.cli.lock_order`` follows it.
            with admissions.dispatch_lock(ctx.wstore), store.atomic(actor) as tx:
                effect = ctx.wstore.one(
                    "SELECT result FROM transition_effects WHERE command_id=?", (command["id"],)
                )
                if effect is not None:
                    result = json.loads(effect["result"])
                elif command["tool"] == "task":
                    call = ToolCall(spec=task.SPEC, args=args, ctx=tool_ctx, action=args["action"])
                    result = task.execute(call)
                elif command["tool"] == "workspace":
                    from research_core.tools import workspace

                    result = workspace.handle(
                        ToolCall(
                            spec=workspace.SPEC, args=args, ctx=tool_ctx, action=args["action"]
                        )
                    )
                elif command["tool"] == "capability":
                    from research_core.store import capabilities

                    result = capabilities.report_failure(store, actor, **args)
                elif command["tool"] == "store":
                    from research_core.store import cli

                    if args["op"] not in cli.ALLOWED or actor.is_session:
                        raise StoreError(
                            ["store commands are restricted to user and harness edits"]
                        )
                    stores = cli.Stores(ctx.ws, ctx.wstore, ctx.coworld.coworld_id)
                    stores._store = store
                    result = {
                        "record": cli._call(
                            args["op"],
                            "coworld",
                            stores,
                            actor,
                            args["args"],
                            coordinator_instance=instance_id,
                        )
                    }
                else:
                    raise StoreError(["unknown durable transition tool"])
                result.setdefault("ok", True)
                tx.execute(
                    "UPDATE transition_commands SET result=?,completed_at=? WHERE id=?",
                    (json.dumps(result, default=str), tx.at, command["id"]),
                )
                tx.event("transition.applied", command["id"], {"owner": instance_id})


def record_effect(tx, env, result: dict) -> None:
    """Commit a cross-store command's authoritative workspace effect and recovery receipt."""
    if "IDE_TRANSITION_ID" in env:
        tx.execute(
            "INSERT INTO transition_effects(command_id,result,committed_at) VALUES (?,?,?)",
            (env["IDE_TRANSITION_ID"], json.dumps(result), tx.at),
        )


def durable_tool(spec: ToolSpec, *, mutations: set[str] | None = None) -> ToolSpec:
    """Accept an explicit ``operation_id`` on every command-producing tool action.

    ``request_generated`` gives a call its identity, so no schema offers the field; chats whose
    frozen schema still lists it may send one, and it keeps its meaning.
    """
    actions = tuple(
        replace(
            action,
            hidden_params={**action.hidden_params, "operation_id": {"type": "string"}},
        )
        if (
            action.name in mutations
            if mutations is not None
            else action.name not in {"read", "list", "workers", "command"}
        )
        else action
        for action in spec.actions
    )
    return tool(replace(spec, actions=actions))


def report_capability(ctx: ToolContext, name: str, **details) -> dict:
    """A tool reports a failure; the coordinator creates or joins its one repair task."""
    if ctx.coordinator_instance is not None:
        from research_core.store import capabilities

        return capabilities.report_failure(ctx.store, ctx.actor, name, **details)
    repaired = ctx.store.one(
        "SELECT COALESCE(MAX(seq),0) FROM events WHERE"
        " (kind='capability.repaired' AND subject=?) OR"
        " (kind IN ('task.completed','task.failed','task.abandoned','task.rejected')"
        " AND subject IN (SELECT 'T-' || id FROM tasks WHERE capability=?))",
        (name, name),
    )[0]
    operation_id = f"failure:{name}:{repaired}"
    prior = ctx.store.one(
        "SELECT args FROM transition_commands WHERE id=?",
        (command_identity(ctx, "capability", operation_id),),
    )
    intent = json.loads(prior["args"]) if prior is not None else {"name": name, **details}
    result = request(ctx, "capability", intent, operation_id)
    state = CommandReceipt.parse(result).state
    if state == "pending":
        return {"name": name, "maintenance_task": None, "transition": result}
    if state == "refused":
        raise StoreError(result["problems"])
    return result
