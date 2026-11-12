"""The workspace owner for assignments, execution attempts and durable handoffs.

Records immutable execution identity before transport, and treats uncertainty as ownership.
Child-result retention and process containment are deliberately not claimed by this boundary.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from opentelemetry.trace import StatusCode

from research_core import brain, inference, layout, run_failures, transitions
from research_core.agent_telemetry import observed
from research_core.store import (
    admissions,
    chat_updates,
    events,
    notices,
    sessions,
    settings,
    tasks,
)
from research_core.store._common import now_iso, parse_iso
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.store.errors import StoreError
from research_core.store.events import Event
from research_core.telemetry import tracer
from research_core.watcher import (
    brain_check,
    champion_sync,
    checks,
    context_panel,
    drain,
    goals,
    heartbeat,
    hermes_api,
    merge,
    store_checks,
    waits,
)
from research_core.watcher.context import TickContext, researcher_profiles, workers
from research_core.watcher.dispatch import drained_sessions, render, start_text
from research_core.watcher.handlers import HANDLERS, Handler
from research_core.watcher.hermes_api import (
    HermesRuns,
    RunsAuthError,
    RunsBusy,
    RunsError,
    RunsNotSent,
    mint_session_id,
)
from research_core.watcher.resume import (
    _pending_message_block,
    emit_event,
    holder_session,
    is_live,
    run_status,
)
from research_core.watcher.store_checks import labelled_stores
from research_core.watcher.wakes import (
    _is_record_ref,
    _subject_ids,
    counted_attempts,
    render_batch,
    render_wake,
)

log = logging.getLogger(__name__)

DEAD_TEXT = "Your previous run ended unexpectedly; continue from your last commit."
HEADER = "The Orchestrator sent you a message; read it below and act on it."
REPEAT_WINDOW = timedelta(hours=6)
PLANNED_STOPS_PREFIX = "wake_planned_stops:"
ERROR_CHARS = 2000
WAKE_INSTRUCTIONS = (
    "You are the Orchestrator in a background wake Session that the watcher started, not a "
    "person: nobody reads this conversation. Act on the event below with the record and "
    "platform tools, read the records before you write, and finish within this run. Never "
    "answer in this conversation; nobody reads it. Use ui.tell for what the user should hear "
    "now, with the category that says why: decision (only they can decide; ask it plainly, they "
    "answer in the chat), outcome (work they asked for finished), submission (a Version "
    "submitted, placed or rejected), failure (something broke that needs them) or merge (a "
    "Version merged to main). Use ui.notify for routine progress they can read later. Settings "
    "bound your own initiative, never a request the user already made."
)

ACTOR = Actor.user("client-dispatch")
RELEASE_NEXT = "Your run will be stopped, then the Task goes back to scheduling; end your run now"
REPLAY_MARGIN_S = 300


@contextmanager
def ownership(ws: layout.Workspace):
    """One owner for the lifetime of the service; contenders fail before executing anything."""
    with (ws.state / "coordinator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Coordinator:
    def __init__(
        self,
        ws: layout.Workspace,
        native: HermesRuns,
        runtime_id: str | None = None,
        *,
        inference_lease_file: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.ws = ws
        self.native = native
        self.runtime_id = runtime_id
        self.instance_id = f"coordinator:{uuid4()}"
        # The inference state beside this file classifies a failed chat run (``run_failures``).
        self.inference_lease_file = inference_lease_file
        self.clock = clock
        # The last report recorded per Coworld: this loop can run twice a second with nothing new.
        self._last_reports: dict[str | None, dict[str, Any]] = {}

    def status(self, run_id: str, *, profile: str):
        return self.native.status(run_id, profile=profile)

    def stop(self, run_id: str, *, profile: str):
        return self.native.stop(run_id, profile=profile)

    def steer(self, run_id: str, text: str | list[dict], *, profile: str):
        return self.native.steer(run_id, text, profile=profile)

    def start(
        self,
        *,
        profile: str,
        session_id: str,
        text: str | list[dict],
        instructions: str | None = None,
        idempotency_key: str,
    ) -> str:
        with Store.open(self.ws.store_path, kind="workspace") as store:
            existing = store.one(
                "SELECT op_id FROM client_admissions WHERE op_id=? OR (profile=? AND native_key=?)",
                (idempotency_key, profile, idempotency_key),
            )
            op_id = (
                existing["op_id"]
                if existing
                else admissions.enqueue(
                    store,
                    ACTOR,
                    key=idempotency_key,
                    session_id=session_id,
                    profile=profile,
                    text=text,
                    instructions=instructions,
                    origin="watcher",
                )
            )
            row = store.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
            payload = json.loads(row["payload"])
            if (row["session_id"], payload["input"], payload["instructions"]) != (
                session_id,
                text,
                instructions,
            ):
                raise RunsError(
                    "Execution payload changed", code="payload_conflict", retryable=False
                )
            if row["resolution_required"]:
                self.close_unreplayable(store, row, row["error"])
                raise RunsError(row["error"], code="execution_interrupted", retryable=False)
            if row["status"] in {"failed", "interrupted", "cancelled"} and not row["run_id"]:
                raise RunsError(
                    row["error"] or row["status"], code="execution_closed", retryable=False
                )
            if row["run_id"]:
                return row["run_id"]
            if row["retry_at"] and parse_iso(store.now()) < parse_iso(row["retry_at"]):
                raise RunsBusy(f"Steering waits until {row['retry_at']}", code="delivery_backoff")
            observed = {}
            capabilities = None
            snapshot = sessions.read(store, session_id)
            if not row["dispatch_started"]:
                self.native.require_inference()
                for previous in store.q(
                    "SELECT * FROM client_admissions"
                    " WHERE session_id=? AND run_id IS NOT NULL AND status IN ('queued','running')",
                    (session_id,),
                ):
                    observed[previous["run_id"]] = self.native.status(
                        previous["run_id"], profile=previous["profile"]
                    )
                    if observed[previous["run_id"]].status == "unknown":
                        self.close_unreplayable(store, previous, "Native outcome is unknown")
                if snapshot["run_id"] not in observed and snapshot["run_id"]:
                    observed[snapshot["run_id"]] = self.native.status(
                        snapshot["run_id"], profile=profile
                    )
                if (
                    row["origin"] in {"client", "answer", "continuation"}
                    and (
                        isinstance(text, str)
                        or isinstance(text[-1]["content"], str)
                        or all(part["type"] == "text" for part in text[-1]["content"])
                    )
                    and snapshot["run_id"]
                    and not observed[snapshot["run_id"]].terminal
                ):
                    with admissions.dispatch_lock(store):
                        current = sessions.read(store, session_id)
                        pending = store.one(
                            "SELECT * FROM client_admissions WHERE op_id=?", (op_id,)
                        )
                        if current["run_id"] != snapshot["run_id"] or pending["dispatch_started"]:
                            raise RunsBusy(
                                "Session changed during steering admission", code="session_busy"
                            )
                        if pending["cancel_requested"] or settings.get(store, "paused"):
                            raise RunsBusy(
                                "Admission is paused or cancelled", code="workspace_paused"
                            )
                        if store.one(
                            "SELECT 1 FROM client_admissions WHERE session_id=? AND op_id!=?"
                            " AND (execution_uncertain=1 OR resolution_required=1 OR"
                            " (status='queued' AND run_id IS NULL AND rowid<(SELECT rowid"
                            " FROM client_admissions WHERE op_id=?)))",
                            (session_id, op_id, op_id),
                        ):
                            raise RunsBusy(
                                "Earlier session input is unsettled", code="session_unresolved"
                            )
                        with store.tx(ACTOR) as tx:
                            tx.execute(
                                "UPDATE client_admissions SET dispatch_started=1,"
                                "delivery_mode='steer',target_run_id=?"
                                " WHERE op_id=?",
                                (snapshot["run_id"], op_id),
                            )
                    steer_text = text if isinstance(text, str) else text[-1]["content"]
                    # Settle definite rejection so durable advancement bounds redelivery.
                    # Generic server errors and missing replies may follow acceptance.
                    # Keep those delivery receipts unconfirmed; their target run stays known.
                    backoff_seconds = int(settings.get(store, "recovery.delivery_backoff_seconds"))
                    call_returned = False
                    try:
                        accepted = self.native.steer(
                            snapshot["run_id"], steer_text, profile=profile
                        )
                        call_returned = True
                    finally:
                        error = sys.exception()
                        rejected = isinstance(error, RunsError) and (
                            isinstance(error, (RunsAuthError, RunsNotSent))
                            or error.status in {400, 401, 403, 404, 409, 422, 429}
                            or (
                                error.status == 503
                                and error.code in {"gateway_draining", "inference_unavailable"}
                            )
                        )
                        if not call_returned and rejected:
                            transient = (
                                isinstance(error, RunsNotSent)
                                or error.status == 429
                                or (
                                    error.status == 503
                                    and error.code in {"gateway_draining", "inference_unavailable"}
                                )
                            )
                            with store.tx(ACTOR) as tx:
                                tx.execute(
                                    "UPDATE client_admissions SET dispatch_started=0,status=CASE"
                                    " WHEN status IN ('queued','running') THEN ? ELSE status END,"
                                    "retry_at=?,error=? WHERE op_id=?",
                                    (
                                        "queued" if transient else "failed",
                                        (
                                            parse_iso(tx.at) + timedelta(seconds=backoff_seconds)
                                        ).isoformat()
                                        if transient
                                        else None,
                                        str(error),
                                        op_id,
                                    ),
                                )
                    with admissions.dispatch_lock(store), store.tx(ACTOR) as tx:
                        if accepted:
                            tx.execute(
                                "UPDATE client_admissions SET run_id=?,status=CASE WHEN "
                                "status='queued' THEN 'running' ELSE status END,retry_at=NULL"
                                " WHERE op_id=?",
                                (snapshot["run_id"], op_id),
                            )
                            tx.event(
                                "client.started",
                                op_id,
                                {"run_id": snapshot["run_id"], "steered": True},
                            )
                        else:
                            tx.execute(
                                "UPDATE client_admissions SET dispatch_started=0,retry_at=?,"
                                "error='Run refused steering; waiting for retry' WHERE op_id=?",
                                (
                                    (
                                        parse_iso(tx.at) + timedelta(seconds=backoff_seconds)
                                    ).isoformat(),
                                    op_id,
                                ),
                            )
                    if not accepted:
                        raise RunsBusy("Run refused steering", code="steer_deferred")
                    return snapshot["run_id"]
                capabilities = self.native.capabilities(profile)
            with admissions.dispatch_lock(store):
                row = store.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
                if row["dispatch_started"] and row["delivery_mode"] == "steer":
                    reason = "Steering delivery unconfirmed; input was not resent"
                    self.close_unreplayable(
                        store, row, reason, execution_unknown=row["target_run_id"] is None
                    )
                    raise RunsError(reason, code="execution_interrupted", retryable=False)
                if not row["dispatch_started"]:
                    if row["cancel_requested"] or row["status"] == "cancelled":
                        raise RunsError(
                            "Execution cancelled before admission",
                            code="cancelled",
                            retryable=False,
                        )
                    session = sessions.read(store, session_id)
                    if session["kind"] == "task":
                        assignment = store.one(
                            "SELECT * FROM task_assignments WHERE session_id=?", (session_id,)
                        )
                        if (
                            assignment is None
                            or assignment["current_attempt_id"] != op_id
                            or assignment["state"] != "held"
                        ):
                            with store.tx(ACTOR) as tx:
                                tx.execute(
                                    "UPDATE client_admissions SET status='cancelled',"
                                    "disposition='target ended before delivery' WHERE op_id=?",
                                    (op_id,),
                                )
                            raise RunsError(
                                "Worker input needs a new execution identity",
                                code="execution_closed",
                                retryable=False,
                            )
                        if assignment["profile"] != profile:
                            raise RunsError(
                                "Worker has no recorded assignment",
                                code="assignment_missing",
                                retryable=False,
                            )
                        if assignment["state"] != "held":
                            raise RunsError(
                                "Worker ownership requires resolution",
                                code="assignment_not_held",
                                retryable=False,
                            )
                        if admissions.reserved_workers(store, session_id) >= int(
                            settings.get(store, "max_researchers")
                        ):
                            raise RunsBusy(
                                "Research capacity is reserved", code="capacity_reserved"
                            )
                    for previous in store.q(
                        "SELECT * FROM client_admissions WHERE session_id=? AND op_id!=?"
                        " AND (status IN ('queued','running') OR resolution_required=1"
                        " OR execution_uncertain=1)"
                        " AND (dispatch_started=1 OR rowid<(SELECT rowid FROM client_admissions"
                        " WHERE op_id=?))"
                        " ORDER BY rowid",
                        (session_id, op_id, op_id),
                    ):
                        if previous["resolution_required"] or previous["execution_uncertain"]:
                            raise RunsBusy(
                                "Session has an unresolved execution", code="session_unresolved"
                            )
                        if previous["run_id"]:
                            if previous["run_id"] not in observed:
                                raise RunsBusy(
                                    "Session changed during observation", code="session_busy"
                                )
                            status = observed[previous["run_id"]]
                            if not status.terminal:
                                raise RunsBusy(
                                    "Session execution has not settled", code="session_busy"
                                )
                            self._record_outcome(store, previous, status)
                            held = store.one(
                                "SELECT resume_after FROM client_admissions WHERE op_id=?",
                                (previous["op_id"],),
                            )
                            if held["resume_after"] is not None:
                                raise RunsBusy(
                                    "Earlier session input is waiting", code="session_busy"
                                )
                        elif previous["dispatch_started"]:
                            raise RunsBusy(
                                "Session has an uncertain start", code="session_unresolved"
                            )
                        else:
                            raise RunsBusy("Earlier session input is queued", code="session_busy")
                    reservation = store.one(
                        "SELECT * FROM task_assignments WHERE current_attempt_id=?", (op_id,)
                    )
                    if reservation is not None:
                        coworld = self.ws.coworld(reservation["coworld_id"])
                        with Store.open(coworld.store_path, kind="coworld") as task_store:
                            task = tasks.read(task_store, reservation["task_id"])
                            same_holder = task["held_by"] == session_id
                            if (
                                not same_holder
                                and not sessions.read(store, session_id)["parent"]
                                and task["updated"] != reservation["task_updated"]
                            ):
                                raise RunsError(
                                    "Assigned task revision changed",
                                    code="assignment_stale",
                                    retryable=False,
                                )
                            worker = sessions.read(store, session_id)
                            if worker["parent"] and not same_holder:
                                predecessor = store.one(
                                    "SELECT * FROM client_admissions WHERE successor_id=?", (op_id,)
                                )
                                if (
                                    predecessor is None
                                    or predecessor["execution_uncertain"]
                                    or predecessor["status"] in {"queued", "running"}
                                ):
                                    raise RunsBusy(
                                        "Predecessor is not excluded", code="session_unresolved"
                                    )
                                transferred = tasks.transfer_attempt(
                                    task_store,
                                    Actor.harness(),
                                    reservation["task_id"],
                                    previous=worker["parent"],
                                    successor=session_id,
                                )
                                if isinstance(transferred, tasks.TransferRefused):
                                    with store.tx(ACTOR) as tx:
                                        tx.execute(
                                            "UPDATE client_admissions SET status='cancelled',"
                                            "disposition='task ownership ended before transfer',"
                                            "advanced_at=?,error=? WHERE op_id=?",
                                            (tx.at, "; ".join(transferred.problems), op_id),
                                        )
                                        tx.execute(
                                            "DELETE FROM task_assignments"
                                            " WHERE current_attempt_id=?",
                                            (op_id,),
                                        )
                                    sessions.end(store, ACTOR, session_id)
                                    raise RunsError(
                                        "; ".join(transferred.problems),
                                        code="assignment_stale",
                                        retryable=False,
                                    )
                            else:
                                claim = tasks.claim_result(
                                    task_store,
                                    Actor.session(session_id),
                                    reservation["task_id"],
                                    run_id=None,
                                    worktree=str(coworld.worktree_for_task(reservation["task_id"])),
                                    branch=f"task/{reservation['task_id']}",
                                )
                                if isinstance(claim, tasks.ClaimRefused):
                                    raise RunsError(
                                        "; ".join(claim.problems),
                                        code="assignment_stale",
                                        retryable=False,
                                    )
                    if not capabilities["supported"] or not capabilities["durable"]:
                        raise RunsError(
                            "Durable native admission unavailable",
                            code="durability_unavailable",
                            retryable=False,
                        )
                    horizon = int(capabilities["retention_seconds"]) - REPLAY_MARGIN_S
                    if horizon <= 0:
                        raise RunsError(
                            "Native replay horizon too short",
                            code="replay_unavailable",
                            retryable=False,
                        )
                    with store.tx(ACTOR) as tx:
                        if settings.get(store, "paused"):
                            raise RunsBusy("Workspace paused", code="workspace_paused")
                        tx.execute(
                            "UPDATE client_admissions SET dispatch_started=1,delivery_mode='start',"
                            "target_run_id=NULL,"
                            " profile=?,native_key=?,"
                            " auth_scope=?,admitted_at=?,retry_until=?,runtime_id=? WHERE op_id=?",
                            (
                                profile,
                                idempotency_key,
                                self.native.auth_scope(profile),
                                tx.at,
                                (parse_iso(tx.at) + timedelta(seconds=horizon)).isoformat(),
                                self.runtime_id,
                                op_id,
                            ),
                        )
                        tx.event(
                            "execution.admitted",
                            op_id,
                            {"profile": profile, "session_id": session_id},
                        )
                    row = store.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
                reason = None
                if self.runtime_id and row["runtime_id"] and self.runtime_id != row["runtime_id"]:
                    reason = "Admitted execution belongs to a retired private runtime"
                elif row["auth_scope"] != self.native.auth_scope(profile):
                    reason = "Native authentication scope changed"
                elif parse_iso(store.now()) >= parse_iso(row["retry_until"]):
                    reason = "Native replay horizon expired"
                if reason:
                    self.close_unreplayable(store, row, reason)
                    raise RunsError(reason, code="execution_interrupted", retryable=False)
            # Transport failures retain this admission and its native key for replay.
            # Callers classify/report errors; the start itself never burns a replacement key.
            run_id = self.native.start(
                profile=profile,
                session_id=session_id,
                text=text,
                instructions=instructions,
                idempotency_key=idempotency_key,
            )
            with admissions.dispatch_lock(store), store.tx(ACTOR) as tx:
                current = tx.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
                recorded = current["run_id"]
                if recorded is not None and recorded != run_id:
                    raise RunsError(
                        "Conflicting native receipt", code="receipt_conflict", retryable=False
                    )
                tx.execute(
                    "UPDATE client_admissions SET run_id=?,status=CASE WHEN status='queued'"
                    " THEN 'running' ELSE status END,owner_instance=?,retry_at=NULL WHERE op_id=?",
                    (run_id, self.instance_id, op_id),
                )
                if current["status"] == "queued":
                    # The run that failed is over. A late receipt on a finished admission keeps
                    # that admission's own error.
                    tx.execute(
                        "UPDATE client_admissions SET error=NULL,error_class=NULL WHERE op_id=?",
                        (op_id,),
                    )
                if recorded is None and current["status"] in {"queued", "running"}:
                    sessions.set_run_in_transaction(tx, session_id, run_id)
                if recorded is None and current["status"] in {"queued", "running"}:
                    tx.event("client.started", op_id, {"run_id": run_id})
            return run_id

    def close_unreplayable(
        self, store: Store, row, reason: str, *, execution_unknown: bool = True
    ) -> None:
        """Close delivery without discarding an untracked execution obligation."""
        if (
            self.runtime_id is not None
            and row["runtime_id"] is not None
            and row["runtime_id"] != self.runtime_id
        ):
            execution_unknown = False  # the private process namespace was replaced
        with store.tx(ACTOR) as tx:
            current = tx.one("SELECT status FROM client_admissions WHERE op_id=?", (row["op_id"],))
            if current["status"] not in {"queued", "running"}:
                # Already closed: nothing is left to resolve, so it must not hold capacity or
                # keep the workspace awake (0011 settles the legacy rows 0006 flagged).
                tx.execute(
                    "UPDATE client_admissions SET resolution_required=0 WHERE op_id=?",
                    (row["op_id"],),
                )
                return
            tx.execute(
                "UPDATE client_admissions SET status='interrupted',resolution_required=0,error=?,"
                "execution_uncertain=?,runtime_id=COALESCE(runtime_id,?) WHERE op_id=?",
                (
                    reason,
                    int(bool(row["dispatch_started"] and execution_unknown)),
                    self.runtime_id,
                    row["op_id"],
                ),
            )
            tx.event("client.delivery_failed", row["op_id"], {"reason": reason, "replayed": False})

    def reconcile_inputs(self, store: Store) -> None:
        retired = []
        if self.runtime_id:
            with store.tx(ACTOR) as tx:
                store.set_meta("runtime_id", self.runtime_id)
                retired = store.q(
                    "SELECT DISTINCT runtime_id FROM client_admissions WHERE execution_uncertain=1"
                    " AND runtime_id IS NOT NULL AND runtime_id!=?",
                    (self.runtime_id,),
                )
                if retired:
                    store.set_meta(
                        "runtime_recovery_count",
                        str(int(store.get_meta("runtime_recovery_count") or "0") + len(retired)),
                    )
                tx.execute(
                    "UPDATE client_admissions SET execution_uncertain=0"
                    " WHERE execution_uncertain=1 AND runtime_id IS NOT NULL AND runtime_id!=?",
                    (self.runtime_id,),
                )
        if retired:
            observed.record(
                "coordinator",
                "runtime_recovered",
                owner_instance=self.instance_id,
                details={"retired_runtimes": len(retired)},
            )
        for row in store.q("SELECT * FROM client_admissions WHERE resolution_required=1"):
            self.close_unreplayable(store, row, row["error"])

    def acknowledge_inputs(self, store: Store, row) -> None:
        """Finish source-message delivery after its native receipt; replay is idempotent."""
        ids = json.loads(row["delivery_ids"])
        session = sessions.read(store, row["session_id"])
        if session["kind"] != "task":
            return
        coworld = self.ws.coworld(session["coworld_id"])
        with Store.open(coworld.store_path, kind="coworld") as task_store:
            task = tasks.read(task_store, session["task_id"])
            if (
                task["status"] == "in-progress"
                and task["held_by"] == session["id"]
                and row["delivery_mode"] == "start"
                and task["run_id"] != row["run_id"]
                and session["run_id"] == row["run_id"]
            ):
                tasks.set_run(task_store, ACTOR, task["id"], row["run_id"])
            if ids:
                tasks.mark_delivered(task_store, ACTOR, ids)
        with store.tx(ACTOR) as tx:
            tx.execute(
                "UPDATE client_admissions SET delivery_ids='[]' WHERE op_id=?", (row["op_id"],)
            )

    @staticmethod
    def _is_experiment_completion(event: Event) -> bool:
        return event.payload.get("kind") == "xp" and "experiment" in event.payload

    def wait_resolved(self, ctx: TickContext, event: Event, store: Store) -> dict[str, Any]:
        """Resume the holder of a Task whose wait the watcher (or the Orchestrator) resolved."""
        task = tasks.read(store, event.subject)
        if task["status"] != "in-progress":
            return {"skipped": f"{event.subject} is {task['status']}, not in progress"}
        waiting = task["waiting_on"]
        if waiting and not self._is_experiment_completion(event):
            return {"skipped": f"{event.subject} waits again on {waiting['kind']}"}
        session = holder_session(ctx, task)
        if session is None:
            return {"skipped": f"{event.subject} has no holder Session to resume"}
        kind = event.payload.get("kind", "something")
        note = event.payload.get("note") or "resolved"
        text = f"Your wait on {kind} resolved: {note}. Continue."
        if waiting:
            text = (
                f"{note} {task['id']} still waits on {waiting['kind']}: {waiting['detail']}. "
                "Handle the experiment result and end your run when no independent work remains. "
                "Respect the outstanding wait and do not proceed with work it blocks."
            )
        return self.continue_session(
            ctx,
            session,
            text,
            key=f"event:{ctx.coworld.coworld_id}:{event.seq}",
        )

    @staticmethod
    def _pending_task_xp(store: Store, task: str) -> bool:
        return (
            store.one(
                "SELECT 1 FROM task_xp_waits WHERE task_id=?", (int(task.removeprefix("T-")),)
            )
            is not None
        )

    def _recoverable(self, store: Store, task: dict[str, Any], session_id: str | None) -> bool:
        return (
            task["status"] == "in-progress"
            and (not task["waiting_on"])
            and not self._pending_task_xp(store, task["id"])
            and (task["held_by"] is not None)
            and (task["held_by"] == session_id)
        )

    def _task_of(self, ctx: TickContext, session: dict[str, Any]) -> dict[str, Any] | None:
        """The Session's Task in the active Coworld's store; None for another Coworld's."""
        if ctx.store is None or ctx.coworld is None or (not session.get("task_id")):
            return None
        if session.get("coworld_id") not in (None, ctx.coworld.coworld_id):
            return None
        try:
            return tasks.read(ctx.store, session["task_id"])
        except StoreError:
            return None

    def deliver_worker_messages(self, ctx: TickContext) -> dict[str, Any]:
        """Deliver pending ``to_researcher`` messages of the active Coworld's in-progress Tasks."""
        if ctx.store is None:
            return {"skipped": "no active Coworld"}
        store = ctx.store
        report: dict[str, Any] = {"steered": [], "resumed": [], "held": [], "deferred": []}
        for task in tasks.list(store, status="in-progress"):
            pending = tasks.pending_messages(store, task["id"], "to_researcher")
            if not pending:
                continue
            session = holder_session(ctx, task)
            if session is None:
                report["held"].append({"task": task["id"], "reason": "no holder Session"})
                continue
            if task["waiting_on"] and (not is_live(run_status(ctx, session))):
                kind = task["waiting_on"]["kind"]
                report["held"].append(
                    {"task": task["id"], "reason": f"delivered when the {kind} wait resolves"}
                )
                continue
            result = self.continue_session(ctx, session, HEADER)
            entry = {"task": task["id"], "messages": len(pending), **result}
            if result.get("delivered") == "steer":
                report["steered"].append(entry)
            elif result.get("delivered") == "queued":
                report["resumed"].append(entry)
            else:
                report["deferred"].append(entry)
        return report

    def deliver_chat_updates(self, ctx: TickContext) -> dict[str, Any]:
        """Hand this pass's Coworld's due ``ui.tell`` updates to its most recent chat as one
        queued follow-up, or to notices when it has no chat (``store/chat_updates.py``). The
        caller skips it while paused, so held updates wait for the resume."""
        coworld_id = ctx.coworld.coworld_id if ctx.coworld is not None else None
        return chat_updates.deliver(ctx.wstore, ctx.actor, coworld_id=coworld_id)

    def abandon_worker(self, ctx: TickContext, event: Event, store: Store) -> dict[str, Any]:
        """``task.abandoned``: stop the previous holder's run (marked as the watcher's stop) and
        end its Session. The branch stays."""
        session_id = event.payload.get("previous_holder")
        if not session_id:
            return {"skipped": f"{event.subject} was not held"}
        session = sessions.read(ctx.wstore, session_id)
        if session is None:
            return {"skipped": f"no Session row for {session_id}"}
        result: dict[str, Any] = {"task": event.subject, "session": session_id}
        with ctx.wstore.tx(ctx.actor) as tx:
            tx.execute(
                "UPDATE task_assignments SET state='release_requested',"
                "recovery_deadline=COALESCE(recovery_deadline,?) WHERE session_id=?",
                (
                    (
                        parse_iso(tx.at)
                        + timedelta(
                            seconds=int(
                                settings.get(ctx.stores, "recovery.receipt_timeout_seconds")
                            )
                        )
                    ).isoformat(),
                    session_id,
                ),
            )
        if is_live(run_status(ctx, session)):
            sessions.set_stopped_by(ctx.wstore, ctx.actor, session_id, "watcher:abandon")
            try:
                status = ctx.hermes.stop(session["run_id"], profile=session["profile"])
                result.update(stopped=session["run_id"], status=status.status)
            except RunsError as error:
                result["stop_error"] = error.detail
        sessions.end(ctx.wstore, ctx.actor, session_id)
        result["ended"] = True
        return result

    def review_worker(self, ctx: TickContext, event: Event, store: Store) -> dict[str, Any]:
        """``task.reviewed``: a rejected review resumes the Researcher with the reason."""
        if event.payload.get("accept"):
            return {"skipped": "accepted; the merge step reads merge == pending"}
        task = tasks.read(store, event.subject)
        if task["status"] != "in-progress":
            return {"skipped": f"{task['id']} is {task['status']}, not reopened"}
        reason = event.payload.get("reason") or "see the review"
        if any(
            m["direction"] == "to_researcher"
            and m["text"] == f"Reopened: {reason}"
            and m["delivered"]
            for m in task["messages"]
        ):
            return {"skipped": "the reopen message was already delivered"}
        session = holder_session(ctx, task)
        if session is None:
            return {"skipped": f"{task['id']} has no holder Session to resume"}
        result = self.continue_session(
            ctx,
            session,
            f"Review reopened {task['id']}: {reason}. Continue.",
            key=f"event:{ctx.coworld.coworld_id}:{event.seq}",
        )
        if result.get("run_id"):
            tasks.set_run(store, ctx.actor, task["id"], result["run_id"])
        return {"task": task["id"], **result}

    def plan_assignments(self, ctx: TickContext) -> dict[str, Any]:
        """Resume drained Sessions, then start runs for dispatchable Tasks while slots remain."""
        if ctx.store is None:
            return {"skipped": "no active Coworld"}
        if settings.get(ctx.stores, "paused"):
            return {"skipped": "paused"}
        report = self._capacity(ctx)
        self._resume_drained(ctx, report)
        if report["deferred"]:
            return report
        candidates = [
            task
            for task in tasks.dispatchable(ctx.store)
            if ctx.wstore.one(
                "SELECT 1 FROM task_assignments WHERE coworld_id=? AND task_id=?",
                (ctx.coworld.coworld_id, int(task["id"].removeprefix("T-"))),
            )
            is None
        ]
        if not candidates or report["slots"] <= 0:
            return report
        if ctx.wstore.one(
            "SELECT 1 FROM client_admissions WHERE origin='planning'"
            " AND (status IN ('queued','running') OR resolution_required=1)"
        ):
            return report
        snapshot = {
            "coworld": ctx.coworld.coworld_id,
            "tasks": candidates,
            "workers": workers(ctx.env),
            "slots": report["slots"],
        }
        text = (
            "Assign research work using task.assign. Read task.workers, "
            "match capabilities to tasks, "
            "respect dependencies and available slots, and provide worker context and rationale. "
            "Do not perform worker tasks yourself.\n" + json.dumps(snapshot, sort_keys=True)
        )
        prefix = "planning:" + hashlib.sha256(text.encode()).hexdigest()
        previous = ctx.wstore.q(
            "SELECT status FROM client_admissions WHERE native_key LIKE ?", (prefix + ":%",)
        )
        attempt = len(previous) + 1
        max_attempts = int(settings.get(ctx.stores, "recovery.max_attempts"))
        if attempt > max_attempts:
            marker = f"planning_failed:{prefix}"
            if ctx.meta(marker) != "1":
                with ctx.wstore.tx(ctx.actor) as tx:
                    tx.event(
                        "recovery.failed",
                        "workspace",
                        {
                            "coworld_id": ctx.coworld.coworld_id,
                            "stage": "planning",
                            "reason": "Planner ended without assigning; retry limit reached",
                        },
                    )
                ctx.set_meta(marker, "1")
            return report
        key = f"{prefix}:{attempt}"
        session = sessions.register(
            ctx.wstore,
            ctx.actor,
            session_id=mint_session_id(),
            profile=ctx.orchestrator_profile,
            kind="wake",
            coworld_id=ctx.coworld.coworld_id,
            event="tasks.assignment_requested",
        )
        # The wake's text shows these revisions, so task.assign is checked against them.
        tasks.note_seen(ctx.store, ctx.actor, session["id"], candidates)
        admissions.enqueue(
            ctx.wstore,
            ctx.actor,
            key=key,
            session_id=session["id"],
            profile=ctx.orchestrator_profile,
            text=text,
            origin="planning",
        )
        report["planning"] = key
        return report

    def workspace_resumed(self, ctx: TickContext, event: Event, store: Store) -> dict[str, Any]:
        """``workspace.resumed`` (a pause ended or a switch came back): drained Sessions first."""
        if ctx.store is None:
            return {"skipped": "no active Coworld"}
        report = self._capacity(ctx)
        self._resume_drained(ctx, report)
        return report

    def _capacity(self, ctx: TickContext) -> dict[str, Any]:
        live = {
            row["session_id"]: None
            for row in ctx.wstore.q(
                "SELECT DISTINCT a.session_id FROM client_admissions a"
                " JOIN sessions s ON s.id=a.session_id"
                " WHERE s.kind='task'"
                " AND (a.status IN ('queued','running') OR a.execution_uncertain=1)"
            )
        }
        return {
            "live": sorted(live),
            "slots": int(settings.get(ctx.stores, "max_researchers")) - len(live),
            "resumed": [],
            "started": [],
            "skipped": [],
            "failed": [],
            "deferred": None,
        }

    def _take_slot(self, report: dict[str, Any], result: dict[str, Any]) -> None:
        if result.get("delivered") == "queued":
            report["slots"] -= 1
        if result.get("deferred"):
            report["deferred"] = result.get("reason")

    def _resume_drained(self, ctx: TickContext, report: dict[str, Any]) -> None:
        ready_xp = {
            event.subject
            for event in events.unhandled(ctx.store, {"task.wait_resolved"})
            if self._is_experiment_completion(event)
        }
        for session in drained_sessions(ctx):
            task = self._held_task(ctx, session)
            if task is None:
                sessions.mark_drained(ctx.wstore, ctx.actor, [session["id"]], False)
                report["skipped"].append(
                    {"session": session["id"], "reason": "no Task to continue"}
                )
                continue
            if (
                task["id"] in ready_xp
                or (task["waiting_on"] or {}).get("kind") == "xp"
                or self._pending_task_xp(ctx.store, task["id"])
            ):
                sessions.mark_drained(ctx.wstore, ctx.actor, [session["id"]], False)
                continue
            if report["slots"] <= 0:
                return
            n = int(task["id"].removeprefix("T-"))
            text = render(
                "task_resume",
                task_id=task["id"],
                title=task["title"],
                worktree=task["worktree"] or ctx.coworld.worktree_for_task(n),
                branch=task["branch"] or f"task/{n}",
            )
            result = self.continue_session(
                ctx, session, text, key=f"resume:{session['id']}:{session['run_id']}"
            )
            self._take_slot(report, result)
            if result.get("deferred"):
                return
            sessions.mark_drained(ctx.wstore, ctx.actor, [session["id"]], False)
            if result.get("run_id"):
                tasks.set_run(ctx.store, ctx.actor, n, result["run_id"])
            report["resumed"].append({"session": session["id"], "task": task["id"], **result})

    def _held_task(self, ctx: TickContext, session: dict[str, Any]) -> dict[str, Any] | None:
        """The in-progress Task this Session holds, or None."""
        if not session.get("task_id"):
            return None
        try:
            task = tasks.read(ctx.store, session["task_id"])
        except StoreError:
            return None
        if task["status"] != "in-progress" or task["held_by"] != session["id"]:
            return None
        return task

    def _dispatch_wake(
        self,
        ctx: TickContext,
        label: str,
        store: Store,
        event: Event,
        handler: Handler,
        max_attempts: int,
        report: dict[str, Any],
    ) -> None:
        ref = f"{label}:{event.seq}"
        row = self._handled_row(store, event.seq)
        if (
            row is not None
            and counted_attempts(ctx, label, event.seq, row["attempts"]) > max_attempts
        ):
            report["gave_up"].append(ref)
            return
        if row is not None and row["session_id"]:
            session = sessions.read(ctx.wstore, row["session_id"])
            if session is not None and session["ended"] is None:
                if session["run_id"]:
                    report["waiting"].append(ref)
                else:
                    self._start_wake(
                        ctx, label, store, [event], handler, session, row["attempts"], report
                    )
                return
        open_session = self._open_wake_session(ctx, store, event)
        if open_session is not None:
            events.claim_handling(store, ctx.actor, event.seq, event.kind, open_session["id"])
            report["joined"].append(f"{ref}->{open_session['id']}")
            return
        if (
            row is not None
            and row["session_id"]
            and sessions.read(ctx.wstore, row["session_id"]) is None
        ):
            session_id, attempts = row["session_id"], row["attempts"]
        else:
            session_id = hermes_api.mint_session_id()
            attempts = events.claim_handling(store, ctx.actor, event.seq, event.kind, session_id)
        if counted_attempts(ctx, label, event.seq, attempts) > max_attempts:
            self._give_up(ctx, label, store, event, handler, row, max_attempts)
            report["gave_up"].append(ref)
            return
        session = sessions.register(
            ctx.wstore,
            ctx.actor,
            session_id=session_id,
            profile=ctx.orchestrator_profile,
            kind="wake",
            coworld_id=ctx.coworld.coworld_id if ctx.coworld is not None else None,
            event=event.kind,
            **_subject_ids(event.subject),
        )
        self._start_wake(ctx, label, store, [event], handler, session, attempts, report)

    def _give_up(
        self,
        ctx: TickContext,
        label: str,
        store: Store,
        event: Event,
        handler: Handler,
        row: Any,
        max_attempts: int,
    ) -> None:
        """Stop retrying: record the failure, emit ``wake.failed`` once and tell the user."""
        last_error = None if row is None else row["last_error"]
        message = f"gave up after {max_attempts} failed wake run(s)"
        if last_error:
            message += f"; last error: {last_error}"
        events.mark_failed(store, ctx.actor, event.seq, message[:ERROR_CHARS])
        if event.kind != "wake.failed":
            with ctx.wstore.tx(ctx.actor) as tx:
                tx.event(
                    "wake.failed",
                    f"{label}:{event.seq}",
                    {
                        "coworld_id": ctx.coworld.coworld_id if ctx.coworld else None,
                        "store": label,
                        "seq": event.seq,
                        "kind": event.kind,
                        "subject": event.subject,
                        "wake": handler.wake,
                        "attempts": max_attempts,
                        "last_error": last_error,
                    },
                )
        refs = [event.subject] if _is_record_ref(event.subject) else []
        notices.add(
            ctx.wstore,
            ctx.actor,
            coworld_id=ctx.coworld.coworld_id if ctx.coworld else None,
            text=(
                f"The coordinator could not complete the {handler.wake} wake "
                f"for {event.kind} ({event.subject}) after {max_attempts} attempt(s)"
            )
            + (f": {last_error}" if last_error else "")
            + ". The event remains visible. workspace.doctor shows runs API health.",
            refs=refs,
            urgency="high",
        )

    def _open_wake_session(self, ctx: TickContext, store: Store, event: Event) -> dict | None:
        """A live wake Session started within the window for another event of the same kind
        and subject, or ``None``."""
        rows = store.q(
            "SELECT DISTINCT h.session_id FROM handled_events h JOIN events e ON e.seq = h.seq"
            " WHERE e.kind = ? AND e.subject = ? AND h.seq != ?"
            " AND h.session_id IS NOT NULL AND h.handled_at IS NULL ORDER BY h.seq DESC",
            (event.kind, event.subject, event.seq),
        )
        for row in rows:
            session = sessions.read(ctx.wstore, row["session_id"])
            if session is None or session["kind"] != "wake" or session["ended"] is not None:
                continue
            if ctx.now - parse_iso(session["started"]) <= REPEAT_WINDOW:
                return session
        return None

    def _handled_row(self, store: Store, seq: int) -> Any:
        return store.one("SELECT * FROM handled_events WHERE seq = ?", (seq,))

    def settle_attempt(self, store: Store, op_id: str, status: hermes_api.RunStatus) -> None:
        """The only native completion transition, for notifications and recovered facts alike.

        A missing native receipt is uncertainty, never evidence authorizing a replacement.
        Domain handoffs are consumed from this durable outcome by advance(), so a crash
        between databases does not lose the transition.
        """
        with admissions.dispatch_lock(store):
            row = store.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
            self._record_outcome(store, row, status)

    def _record_outcome(self, store: Store, row, status: hermes_api.RunStatus) -> None:
        """Caller holds the admission lock; every outcome uses this one transition."""
        if row["run_id"] is not None and row["run_id"] != status.run_id:
            raise StoreError(["native outcome belongs to another execution receipt"])
        if row["status"] not in {"queued", "running"}:
            return
        if row["run_id"] is None and (row["resume_after"] or row["resumes"]):
            return  # A held or resuming admission already settled its run; this report is late.
        if status.status == "unknown":
            self.close_unreplayable(store, row, "Native outcome is unknown")
            return
        if not status.terminal:
            return
        outcome, error = status.status, status.error
        if outcome == "cancelled" and not row["cancel_requested"]:
            # Only a /message-cancel cancels an input. A run stopped without one (a drain, a
            # task abandon, a gateway shutdown) was interrupted under the input.
            outcome, error = "interrupted", error or "The workspace stopped the run"
        error_class = None
        if status.status == "failed" and row["origin"] in {"client", "followup"}:
            error_class = self._classify(row, status)
            if (
                error_class in run_failures.WAITING_CLASSES
                and not row["cancel_requested"]
                and row["delivery_mode"] == "start"
            ):
                self._hold(store, row, status, error_class)
                self.observe_terminal(row, status)
                return
        with store.tx(ACTOR) as tx:
            tx.execute(
                "UPDATE client_admissions SET status=?,error=?,error_class=?,"
                "run_id=COALESCE(run_id,?) WHERE op_id=?",
                (outcome, error, error_class, status.run_id, row["op_id"]),
            )
            tx.event(
                "client.finished",
                row["op_id"],
                {"status": outcome, "error_class": error_class},
            )
        self.observe_terminal(row, status)

    def observe_terminal(self, row, status: hermes_api.RunStatus) -> None:
        """Acceptance to terminal observation includes polling delay, not native completion."""
        try:
            current = tracer().start_span(
                "workspace.run.observed",
                start_time=int(parse_iso(row["created_at"]).timestamp() * 1e9),
                attributes={
                    "session.id": row["session_id"],
                    "op.id": row["op_id"],
                    "run.id": status.run_id,
                    "role": row["profile"],
                    "outcome": status.status,
                    "observation.boundary": "acceptance_to_terminal_observation",
                },
            )
            if row["admitted_at"] is not None:
                current.add_event(
                    "execution.admitted",
                    timestamp=int(parse_iso(row["admitted_at"]).timestamp() * 1e9),
                )
            if status.status != "completed":
                current.set_status(StatusCode.ERROR)
            current.end()
        except Exception:
            # Optional timing annotation must never fail the completed state transition.
            pass

    def _installed_key_id(self) -> str | None:
        lease = inference.read_lease(self.inference_lease_file)
        return lease.key_id if lease else None

    def _classify(self, row, status: hermes_api.RunStatus) -> run_failures.ErrorClass | None:
        """The class of a chat message's failed run (``run_failures``); ``None`` if unknown."""
        error_class = run_failures.classify(
            session_id=row["session_id"],
            raw=status.raw,
            error=status.error,
            state=inference.read_state(self.inference_lease_file),
            installed_key_id=self._installed_key_id(),
            now=self.clock(),
        )
        if error_class == "misconfigured":
            log.error(
                "Chat run %s failed: the workspace is misconfigured (%s)",
                status.run_id,
                status.error,
            )
        return error_class

    def _hold(self, store: Store, row, status: hermes_api.RunStatus, error_class: str) -> None:
        """Hold a chat message whose run failed on budget, rate limit or provider outage.

        The admission goes back to ``queued`` with ``resume_after`` set, which the snapshot
        reports as ``waiting``: its run is over, no new one has started, and it keeps its
        Session's place, so later messages queue behind it. :meth:`resume_if_due` restarts it.
        The steered inputs that rode the same run end ``failed`` with the class: the resume
        continues the whole conversation, theirs included.
        """
        now = self.clock()
        state = inference.read_state(self.inference_lease_file)
        installed_key_id = self._installed_key_id()
        resume_after = now_iso(
            lambda: run_failures.resume_after(
                error_class, state, now, row["resumes"], installed_key_id=installed_key_id
            )
        )
        with store.tx(ACTOR) as tx:
            tx.execute(
                "UPDATE client_admissions SET status='queued',run_id=NULL,dispatch_started=0,"
                "error=?,error_class=?,resume_after=? WHERE op_id=?",
                (status.error, error_class, resume_after, row["op_id"]),
            )
            tx.event(
                "client.waiting",
                row["op_id"],
                {"error_class": error_class, "resume_after": resume_after, "run_id": status.run_id},
            )

    def resume_if_due(self, store: Store, row) -> Any | None:
        """Release a held message whose hold is over, or return ``None`` while it must wait.

        It may resume once ``resume_after`` has passed, the workspace is not paused and the
        inference gate is open (a budget block holds until a new key). Resume ``n`` sends the
        continuation prompt for its class as a user message under the native key
        ``<op id>:resume:<n>``, so a start retried after a crash replays the same run and each
        resume is a new one. The admitted input stays in ``request``; attachments are never
        prepared again. Caller holds the admission lock.
        """
        now = self.clock()
        if (
            parse_iso(row["resume_after"]) > now
            or settings.get(store, "paused")
            or not inference.available(self.inference_lease_file, now)
        ):
            return None
        payload = json.loads(row["payload"])
        payload["input"] = [
            {"role": "user", "content": run_failures.continuation(row["error_class"])}
        ]
        resume = row["resumes"] + 1
        with store.tx(ACTOR) as tx:
            tx.execute(
                "UPDATE client_admissions SET payload=?,native_key=?,resumes=?,resume_after=NULL"
                " WHERE op_id=?",
                (json.dumps(payload), f"{row['op_id']}:resume:{resume}", resume, row["op_id"]),
            )
            tx.event(
                "client.resumed",
                row["op_id"],
                {"error_class": row["error_class"], "resume": resume},
            )
        return store.one("SELECT * FROM client_admissions WHERE op_id=?", (row["op_id"],))

    def reconcile(self) -> None:
        """Recover missed native outcomes. This pass never schedules or retries work."""
        with Store.open(self.ws.store_path, kind="workspace") as store:
            self.reconcile_inputs(store)
            for row in store.q(
                "SELECT * FROM client_admissions WHERE status IN ('queued','running')"
                " AND run_id IS NOT NULL"
            ):
                self.settle_attempt(
                    store, row["op_id"], self.native.status(row["run_id"], profile=row["profile"])
                )

    def advance_attempts(self, ctx: TickContext) -> None:
        """Resume interrupted durable handoffs, using immutable predecessor identities."""
        for row in ctx.wstore.q(
            "SELECT * FROM client_admissions WHERE kind='message' AND status IN"
            " ('completed','failed','interrupted','cancelled') AND advanced_at IS NULL"
            " AND execution_uncertain=0 ORDER BY rowid"
        ):
            snapshot = sessions.read(ctx.wstore, row["session_id"])
            if snapshot["coworld_id"] is not None and (
                ctx.coworld is None or snapshot["coworld_id"] != ctx.coworld.coworld_id
            ):
                continue
            history = []
            if (
                snapshot["kind"] == "task"
                and row["delivery_mode"] == "start"
                and not snapshot["stopped_by"]
                and not settings.get(ctx.stores, "paused")
                and ctx.wstore.one(
                    "SELECT 1 FROM task_assignments WHERE current_attempt_id=? AND state='held'",
                    (row["op_id"],),
                )
            ):
                task = self._task_of(ctx, snapshot)
                failures = ctx.wstore.one(
                    "SELECT count(*) FROM client_admissions a JOIN sessions s ON s.id=a.session_id"
                    " WHERE s.coworld_id=? AND s.task_id=? AND a.failure_counted=1",
                    (snapshot["coworld_id"], snapshot["task_id"]),
                )[0] + int(not row["failure_counted"])
                if (
                    task is not None
                    and self._recoverable(ctx.store, task, snapshot["id"])
                    and failures < int(settings.get(ctx.stores, "recovery.max_attempts"))
                ):
                    history = self.native.session_messages(
                        snapshot["id"], profile=snapshot["profile"]
                    )
            with admissions.dispatch_lock(ctx.wstore):
                row = ctx.wstore.one(
                    "SELECT * FROM client_admissions WHERE op_id=?", (row["op_id"],)
                )
                if row["advanced_at"] is not None or row["execution_uncertain"]:
                    continue
                if ctx.store is not None and ctx.store.one(
                    "SELECT 1 FROM transition_commands WHERE issuer_attempt_id=?"
                    " AND result IS NULL",
                    (row["op_id"],),
                ):
                    continue
                session = sessions.read(ctx.wstore, row["session_id"])
                if session["coworld_id"] is not None and (
                    ctx.coworld is None or session["coworld_id"] != ctx.coworld.coworld_id
                ):
                    continue
                reroute = row["disposition"] == "target ended before delivery"
                rejected = (
                    row["delivery_mode"] == "steer"
                    and row["status"] in {"failed", "interrupted", "cancelled"}
                    and not row["dispatch_started"]
                    and not row["cancel_requested"]
                )
                if reroute or rejected:
                    assignment = ctx.wstore.one(
                        "SELECT * FROM task_assignments WHERE coworld_id=? AND task_id=?",
                        (session["coworld_id"], session["task_id"]),
                    )
                    task = self._task_of(ctx, session)
                    if assignment is not None and task["status"] == "in-progress":
                        target = sessions.read(ctx.wstore, assignment["session_id"])
                        result = self._queue_continuation(
                            ctx,
                            target,
                            json.loads(row["payload"])["input"],
                            key=f"reroute:{row['op_id']}" if reroute else row["native_key"],
                            delivery_failures=row["delivery_failures"],
                            with_pending_messages=False,
                            delivery_ids=tuple(json.loads(row["delivery_ids"])),
                        )
                        if result.get("deferred"):
                            continue
                        successor = ctx.wstore.one(
                            "SELECT op_id FROM client_admissions WHERE profile=? AND native_key=?",
                            (row["profile"], result["key"]),
                        )
                        with ctx.wstore.tx(ctx.actor) as tx:
                            tx.execute(
                                "UPDATE client_admissions SET disposition=?,"
                                "successor_id=?,advanced_at=? WHERE op_id=?",
                                (
                                    "delivery rerouted" if reroute else "delivery retry queued",
                                    successor["op_id"],
                                    tx.at,
                                    row["op_id"],
                                ),
                            )
                        observed.record(
                            "execution",
                            "successor",
                            admission_id=row["op_id"],
                            execution_attempt_id=successor["op_id"],
                            session_id=target["id"],
                            parent_session_id=row["session_id"],
                            reason="rerouted" if reroute else "delivery_retry",
                        )
                        continue
                # A steer is a delivery to an existing attempt, not a new worker attempt.
                if row["delivery_mode"] == "start":
                    if session["run_id"] == row["run_id"]:
                        sessions.end(ctx.wstore, ctx.actor, session["id"])
                    if session["kind"] == "wake":
                        for label, store in labelled_stores(ctx):
                            for event in store.q(
                                "SELECT seq FROM handled_events WHERE session_id=?"
                                " AND handled_at IS NULL",
                                (session["id"],),
                            ):
                                if row["status"] == "completed":
                                    events.mark_handled(
                                        store, ctx.actor, event["seq"], row["run_id"]
                                    )
                                else:
                                    events.mark_failed(
                                        store,
                                        ctx.actor,
                                        event["seq"],
                                        row["error"] or row["status"],
                                    )
                                    if session["stopped_by"] == sessions.SLEEP_STOP:
                                        marker = f"wake_stop:{row['op_id']}:{label}:{event['seq']}"
                                        counter = f"{PLANNED_STOPS_PREFIX}{label}:{event['seq']}"
                                        with ctx.wstore.tx(ctx.actor):
                                            if ctx.meta(marker) is None:
                                                ctx.set_meta(
                                                    counter, str(int(ctx.meta(counter) or 0) + 1)
                                                )
                                                ctx.set_meta(marker, "1")
                    elif (
                        session["kind"] == "task"
                        and not session["stopped_by"]
                        and ctx.wstore.one(
                            "SELECT 1 FROM task_assignments WHERE current_attempt_id=?"
                            " AND state='held'",
                            (row["op_id"],),
                        )
                    ):
                        task = self._task_of(ctx, session)
                        if task is not None and self._recoverable(ctx.store, task, session["id"]):
                            if settings.get(ctx.stores, "paused"):
                                continue
                            self.retry_worker(
                                ctx,
                                ctx.store,
                                task,
                                session,
                                row["error"] or row["status"],
                                predecessor=row,
                                history=history,
                            )
                with ctx.wstore.tx(ctx.actor) as tx:
                    tx.execute(
                        "UPDATE client_admissions SET advanced_at=?,"
                        "disposition=COALESCE(disposition,?)"
                        " WHERE op_id=?",
                        (
                            tx.at,
                            "delivery settled"
                            if row["delivery_mode"] == "steer"
                            else "domain outcome recorded",
                            row["op_id"],
                        ),
                    )

    def continue_session(
        self,
        ctx: TickContext,
        session: dict[str, Any],
        text: str,
        *,
        key: str | None = None,
        attempt_note: str = "",
        with_pending_messages: bool = True,
    ) -> dict[str, Any]:
        history = []
        if session["kind"] == "task":
            current = ctx.wstore.one(
                "SELECT a.*,t.state AS assignment_state FROM task_assignments t"
                " JOIN client_admissions a"
                " ON a.op_id=t.current_attempt_id WHERE t.session_id=?",
                (session["id"],),
            )
            if (
                current is not None
                and current["run_id"]
                and current["status"] in {"queued", "running"}
            ):
                observed = self.native.status(current["run_id"], profile=session["profile"])
                self.settle_attempt(ctx.wstore, current["op_id"], observed)
            current = ctx.wstore.one(
                "SELECT a.*,t.state AS assignment_state FROM task_assignments t"
                " JOIN client_admissions a"
                " ON a.op_id=t.current_attempt_id WHERE t.session_id=?",
                (session["id"],),
            )
            if (
                current is not None
                and current["status"] not in {"queued", "running"}
                and not current["execution_uncertain"]
                and not current["resolution_required"]
                and current["assignment_state"] == "held"
                and not settings.get(ctx.stores, "paused")
                and ctx.coworld is not None
                and session["coworld_id"] == ctx.coworld.coworld_id
            ):
                history = self.native.session_messages(session["id"], profile=session["profile"])
        with admissions.dispatch_lock(ctx.wstore):
            return self._queue_continuation(
                ctx,
                session,
                text,
                history=history,
                key=key,
                attempt_note=attempt_note,
                with_pending_messages=with_pending_messages,
            )

    def _queue_continuation(
        self,
        ctx: TickContext,
        session: dict[str, Any],
        text: str,
        *,
        attempt_note: str = "",
        with_pending_messages: bool = True,
        key: str | None = None,
        delivery_ids: tuple[int, ...] = (),
        delivery_failures: int = 0,
        history: tuple[dict, ...] | list[dict] = (),
    ) -> dict[str, Any]:
        """Persist continuation intent; the one admission dispatcher performs delivery.

        No polling, raw steering, native start or attempt mutation occurs here. Pending
        message IDs and predecessor IDs give retries stable identities after a crash.
        """
        if settings.get(ctx.stores, "paused"):
            return {"session": session["id"], "deferred": True, "reason": "paused"}
        message_ids = list(delivery_ids)
        if with_pending_messages:
            block, message_ids = _pending_message_block(ctx, session)
            if block:
                text += "\n\n" + block
        if attempt_note:
            text += "\n\n" + attempt_note
        if key is None and message_ids:
            key = f"messages:{session['id']}:{','.join(map(str, message_ids))}"
        if key is None:
            return {"session": session["id"], "deferred": True, "reason": "messages already queued"}
        previous = ctx.wstore.one(
            "SELECT * FROM client_admissions WHERE profile=? AND native_key=?",
            (session["profile"], key),
        )
        # Definitively undelivered attempts retain their identity; retries use new keys.
        while previous is not None and (
            previous["disposition"] == "target ended before delivery"
            or (
                previous["status"] in {"failed", "interrupted", "cancelled"}
                and not previous["dispatch_started"]
                and not previous["cancel_requested"]
            )
        ):
            delivery_failures = previous["delivery_failures"] + int(
                previous["disposition"] != "target ended before delivery"
            )
            if previous[
                "disposition"
            ] != "target ended before delivery" and delivery_failures >= int(
                settings.get(ctx.stores, "recovery.max_attempts")
            ):
                with ctx.wstore.tx(ctx.actor) as tx:
                    tx.execute(
                        "UPDATE client_admissions SET disposition='delivery retries exhausted',"
                        "advanced_at=COALESCE(advanced_at,?) WHERE op_id=?",
                        (tx.at, previous["op_id"]),
                    )
                return {
                    "session": previous["session_id"],
                    "deferred": True,
                    "reason": "delivery retries exhausted",
                }
            key = (
                key + ":after-end"
                if previous["disposition"] == "target ended before delivery"
                else f"redelivery:{previous['op_id']}"
            )
            previous = ctx.wstore.one(
                "SELECT * FROM client_admissions WHERE profile=? AND native_key=?",
                (session["profile"], key),
            )
        if previous is not None:
            return {"session": previous["session_id"], "delivered": "queued", "key": key}
        predecessor = None
        assignment = None
        if session["kind"] == "task":
            assignment = ctx.wstore.one(
                "SELECT * FROM task_assignments WHERE coworld_id=? AND task_id=?",
                (session["coworld_id"], session["task_id"]),
            )
            if assignment is None:
                raise StoreError(["worker continuation has no authoritative assignment"])
            if assignment["state"] != "held":
                return {"session": session["id"], "deferred": True, "reason": "release pending"}
            if assignment["session_id"] != session["id"]:
                return {
                    "session": assignment["session_id"],
                    "deferred": True,
                    "reason": "ownership handoff pending",
                }
            predecessor = ctx.wstore.one(
                "SELECT * FROM client_admissions WHERE op_id=?", (assignment["current_attempt_id"],)
            )
            if ctx.store.one(
                "SELECT 1 FROM transition_commands WHERE issuer_attempt_id=? AND result IS NULL",
                (predecessor["op_id"],),
            ):
                return {
                    "session": session["id"],
                    "deferred": True,
                    "reason": "accepted worker report pending",
                }
            if predecessor["execution_uncertain"] or predecessor["resolution_required"]:
                return {
                    "session": session["id"],
                    "deferred": True,
                    "reason": "predecessor execution uncertain",
                }
            if predecessor["status"] in {"queued", "running"}:
                predecessor = None  # A live run receives a steer, retaining its execution identity.
        target = session["id"]
        if predecessor is not None:
            target = "api_" + uuid5(NAMESPACE_URL, f"worker:{session['profile']}:{key}").hex
            task = tasks.read(ctx.store, session["task_id"])
            # Keep whole messages so the bounded transcript remains valid JSON.
            recent_history = list(history[-16:])
            while recent_history and len(json.dumps(recent_history, sort_keys=True)) > 32000:
                recent_history.pop(0)
            # New execution sessions inherit durable task progress, not mutable runtime identity.
            text = (
                "Continue this task from its durable handoff. Inspect the recorded worktree "
                "and pending work before acting.\n"
                + json.dumps(task, sort_keys=True)
                + "\nPrevious execution outcome: "
                + predecessor["status"]
                + "\nRecent conversation (last 16 messages, 32000 characters):\n"
                + json.dumps(recent_history, sort_keys=True)
                + "\n\n"
                + text
            )
        with ctx.wstore.tx(ctx.actor) as tx:
            if predecessor is not None:
                sessions.register_in_transaction(
                    tx,
                    session_id=target,
                    profile=session["profile"],
                    kind="task",
                    coworld_id=session["coworld_id"],
                    task_id=session["task_id"],
                    goal_id=session["goal_id"],
                    parent=session["id"],
                )
            successor_id = admissions.enqueue_in_transaction(
                tx,
                key=key,
                session_id=target,
                profile=session["profile"],
                text=text,
                origin="continuation",
                delivery_ids=tuple(message_ids),
            )
            tx.execute(
                "UPDATE client_admissions SET delivery_failures=? WHERE op_id=?",
                (delivery_failures, successor_id),
            )
            if predecessor is not None:
                tx.execute(
                    "UPDATE task_assignments SET current_attempt_id=?,session_id=?"
                    " WHERE op_id=? AND current_attempt_id=?",
                    (successor_id, target, assignment["op_id"], predecessor["op_id"]),
                )
                tx.execute(
                    "UPDATE client_admissions SET successor_id=?,disposition='successor "
                    "queued',advanced_at=?"
                    " WHERE op_id=?",
                    (successor_id, tx.at, predecessor["op_id"]),
                )
        if predecessor is not None:
            observed.record(
                "execution",
                "successor",
                admission_id=predecessor["op_id"],
                execution_attempt_id=successor_id,
                assignment_id=assignment["op_id"],
                session_id=target,
                parent_session_id=session["id"],
                task_id=f"T-{session['task_id']}",
                profile=session["profile"],
                reason="new_session",
            )
        return {"session": target, "delivered": "queued", "key": key}

    def retry_worker(
        self,
        ctx: TickContext,
        store: Store,
        task: dict[str, Any],
        session: dict,
        why: str,
        *,
        predecessor,
        history: tuple[dict, ...] | list[dict] = (),
    ) -> dict[str, Any]:
        """A terminal attempt stays terminal; its successor has a new immutable identity."""
        key = f"retry:{predecessor['op_id']}"
        if ctx.wstore.one(
            "SELECT 1 FROM client_admissions WHERE native_key=? AND profile=?",
            (key, session["profile"]),
        ):
            return {"session": session["id"], "delivered": "queued", "key": key}
        with ctx.wstore.tx(ctx.actor) as tx:
            tx.execute(
                "UPDATE client_admissions SET failure_counted=1 WHERE op_id=?",
                (predecessor["op_id"],),
            )
        count = ctx.wstore.one(
            "SELECT count(*) FROM client_admissions a JOIN sessions s ON s.id=a.session_id"
            " WHERE s.coworld_id=? AND s.task_id=? AND a.delivery_mode='start'"
            " AND a.failure_counted=1",
            (session["coworld_id"], session["task_id"]),
        )[0]
        max_attempts = int(settings.get(ctx.stores, "recovery.max_attempts"))
        if count < max_attempts:
            key = f"retry:{predecessor['op_id']}"
            return self._queue_continuation(
                ctx,
                session,
                DEAD_TEXT,
                attempt_note=f"(Attempt {count + 1} of {max_attempts} for this Session.)",
                key=key,
                with_pending_messages=False,
                history=history,
            )
        failure = {
            "what_failed": f"Worker {task['held_by']} exhausted {max_attempts} attempts: {why}",
            "next_step": "Review the failure and create a new Task with revised instructions",
        }
        tasks.fail(store, Actor.session(task["held_by"]), task["id"], failure=failure)
        emit_event(
            ctx.wstore,
            ctx.actor,
            "recovery.failed",
            task["id"],
            {
                "coworld_id": session["coworld_id"],
                "task": task["id"],
                "session": session["id"],
                "reason": failure["what_failed"],
                "stage": "recovery",
            },
        )
        return {"action": "failed", "task": task["id"], "reason": failure["what_failed"]}

    def advance(self, ctx: TickContext) -> dict:
        """One explicit workflow owner. Observation only supplies facts to this state machine."""
        transitions.apply(ctx, self.instance_id)
        report = {"store_checks": store_checks.run(ctx)}
        paused = store_checks.pause_state(ctx)
        self.reconcile_inputs(ctx.wstore)
        self.adopt_existing_workers(self.ws, ctx.wstore, self.runtime_id)
        self.advance_attempts(ctx)
        self.finish_assignments(ctx.wstore)
        if paused:
            if ctx.meta("paused_drained") != "1":
                report["drain"] = drain.run(ctx)
                if report["drain"]["quiet"] and not report["drain"]["errors"]:
                    ctx.set_meta("paused_drained", "1")
        else:
            ctx.set_meta("paused_drained", "0")
            # These are explicit domain operations, not an import-by-name event bus.
            # Their polling cadence is independent from missed-native-outcome recovery.
            workflow_key = (
                f"coordinator_workflow_at:{ctx.coworld.coworld_id if ctx.coworld else 'workspace'}"
            )
            due = ctx.meta(workflow_key)
            if due is None or ctx.now - parse_iso(due) >= timedelta(seconds=60):
                report["champion_sync"] = champion_sync.run(ctx)
                report["waits"] = waits.run(ctx)
                report["merge"] = merge.run(ctx)
                report["goals"] = goals.run(ctx)
                report["checks"] = checks.run(ctx)
                if brain.enabled(ctx.env):
                    report["brain"] = brain_check.run(ctx)
                ctx.set_meta(workflow_key, ctx.now.isoformat())
            report["messages"] = self.deliver_worker_messages(ctx)
            report["chat_updates"] = self.deliver_chat_updates(ctx)
            report["dispatch"] = self.plan_assignments(ctx)
            report["wakes"] = self.advance_events(ctx)
        report["heartbeat"] = heartbeat.run(ctx)
        report["context_panel"] = context_panel.run(ctx)
        self._record_report(ctx, report)
        return report

    def _record_report(self, ctx: TickContext, report: dict[str, Any]) -> None:
        """The report as numbers. An unchanged one says nothing."""
        details = observed.report_details(report)
        coworld_id = ctx.coworld.coworld_id if ctx.coworld is not None else None
        if self._last_reports.get(coworld_id) != details:
            self._last_reports[coworld_id] = details
            observed.record(
                "coordinator",
                "report",
                coworld_id=coworld_id,
                owner_instance=self.instance_id,
                details=details,
            )

    def advance_events(self, ctx: TickContext) -> dict[str, Any]:
        if settings.get(ctx.stores, "paused"):
            return {"skipped": "paused"}
        report = {
            name: []
            for name in ("handled", "started", "joined", "waiting", "busy", "failed", "gave_up")
        }
        max_attempts = int(settings.get(ctx.wstore, "recovery.max_attempts"))
        for label, store in labelled_stores(ctx):
            batches: dict[str, list[Event]] = {}
            for event in events.unhandled(store, set(HANDLERS)):
                if store is ctx.wstore:
                    event_coworld = event.payload.get("coworld_id")
                    if event.kind == "coworld.setup_needed":
                        event_coworld = event.subject
                    if event_coworld is not None and event_coworld != (
                        ctx.coworld.coworld_id if ctx.coworld else None
                    ):
                        continue
                handler = HANDLERS[event.kind]
                ref = f"{label}:{event.seq}"
                if handler.batch_by_kind:
                    batches.setdefault(event.kind, []).append(event)
                    continue
                if handler.model:
                    if event.kind == "task.completed":
                        task = tasks.read(store, event.subject)
                        holder = holder_session(ctx, task)
                        if holder is not None and ctx.wstore.one(
                            "SELECT 1 FROM client_admissions WHERE session_id=? AND"
                            " (status IN ('queued','running') OR execution_uncertain=1)",
                            (holder["id"],),
                        ):
                            report["waiting"].append(ref)
                            continue
                    self._dispatch_wake(ctx, label, store, event, handler, max_attempts, report)
                    continue
                match event.kind:
                    case "task.reviewed":
                        result = self.review_worker(ctx, event, store)
                    case "task.wait_resolved":
                        result = self.wait_resolved(ctx, event, store)
                    case "workspace.resumed":
                        result = self.workspace_resumed(ctx, event, store)
                    case "task.abandoned":
                        result = self.abandon_worker(ctx, event, store)
                    case "capability.failed":
                        result = checks.handle_capability_failed(ctx, event, store)
                    case "repair.failed_twice":
                        result = checks.handle_repair_failed_twice(ctx, event, store)
                    case "task.failed":
                        result = checks.handle_task_failed(ctx, event, store)
                if not result.get("deferred"):
                    events.mark_handled(store, ctx.actor, event.seq, None)
                    report["handled"].append(ref)
            for kind, pending in batches.items():
                self._dispatch_batch(
                    ctx, label, store, pending, HANDLERS[kind], max_attempts, report
                )
        return report

    def _start_wake(
        self,
        ctx: TickContext,
        label: str,
        store: Store,
        batch: list[Event],
        handler: Handler,
        session: dict,
        attempts: int,
        report: dict,
    ) -> None:
        event = batch[0]
        key = f"event:{label}:{event.seq}:{attempts}"
        pending = ctx.wstore.one(
            "SELECT * FROM client_admissions WHERE native_key=? AND profile=?",
            (key, ctx.orchestrator_profile),
        )
        if pending is None:
            op_id = admissions.enqueue(
                ctx.wstore,
                ctx.actor,
                key=key,
                session_id=session["id"],
                profile=ctx.orchestrator_profile,
                text=render_batch(handler.wake, batch, ctx)
                if handler.batch_by_kind
                else render_wake(handler.wake, event, ctx),
                instructions=WAKE_INSTRUCTIONS,
                origin="wake",
            )
            observed.record(
                "wake",
                "started",
                admission_id=op_id,
                session_id=session["id"],
                profile=ctx.orchestrator_profile,
                coworld_id=ctx.coworld.coworld_id if ctx.coworld is not None else None,
                native_event_type=event.kind,
                reason=handler.wake,
                details={
                    "batch_size": len(batch),
                    "first_seq": batch[0].seq,
                    "last_seq": batch[-1].seq,
                    "attempts": attempts,
                },
            )
        report["started"].append(f"{label}:{event.seq}->{session['id']}")

    @staticmethod
    def reserve(
        ctx,
        *,
        task_id: int,
        task_updated: str,
        profile: str,
        assignment_id: str,
        context: str,
        rationale: str,
    ) -> dict:
        if profile not in researcher_profiles(ctx.env):
            raise StoreError(["profile is not an available research worker"])
        coworld = ctx.coworld()
        with admissions.dispatch_lock(ctx.wstore):
            previous = ctx.wstore.one(
                "SELECT * FROM task_assignments WHERE coworld_id=? AND task_id=?",
                (coworld.coworld_id, task_id),
            )
            key = f"assignment:{assignment_id}"
            op_id = str(uuid5(NAMESPACE_URL, f"research:{profile}:{key}"))
            if previous is not None:
                if (
                    previous["op_id"],
                    previous["task_updated"],
                    previous["rationale"],
                    previous["context"],
                ) != (op_id, task_updated, rationale, context):
                    raise StoreError(
                        [
                            "this Task already has an assignment with other details, and an "
                            "assignment cannot be changed; send task.message to tell its worker, "
                            "or wait until the worker has finished or released it"
                        ]
                    )
                return {
                    "kind": "reserved",
                    "op_id": op_id,
                    "session_id": previous["session_id"],
                    "profile": profile,
                }
            task = tasks.read(ctx.store, task_id)
            if task["updated"] != task_updated:
                raise StoreError(
                    ["the Task changed after you last read it; read it with task.read, then assign"]
                )
            dispatchable = {task["id"] for task in tasks.dispatchable(ctx.store)}
            if task["id"] not in dispatchable:
                raise StoreError(
                    [
                        "only an accepted Task whose dependencies are done can be assigned; "
                        "read it with task.read"
                    ]
                )
            occupied = admissions.reserved_workers(ctx.wstore)
            if occupied >= int(settings.get([ctx.wstore, ctx.store], "max_researchers")):
                raise StoreError(
                    [
                        "every Researcher slot is taken (max_researchers); assign this when a "
                        "running Task ends"
                    ]
                )
            session_id = mint_session_id()
            # The prompt is rendered from the exact task revision the session saw.
            text = (
                start_text(ctx.store, coworld, task)
                + f"\n\nAssignment context:\n{context}\nReason:\n{rationale}"
            )
            with ctx.wstore.tx(ctx.actor) as tx:
                sessions.register_in_transaction(
                    tx,
                    session_id=session_id,
                    profile=profile,
                    kind="task",
                    coworld_id=coworld.coworld_id,
                    task_id=task_id,
                    goal_id=task["goal_id"],
                )
                admissions.enqueue_in_transaction(
                    tx,
                    key=key,
                    session_id=session_id,
                    profile=profile,
                    text=text,
                    origin="assignment",
                )
                tx.execute(
                    "INSERT INTO task_assignments "
                    "(coworld_id,task_id,op_id,session_id,profile,task_updated,rationale,context)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        coworld.coworld_id,
                        task_id,
                        op_id,
                        session_id,
                        profile,
                        task_updated,
                        rationale,
                        context,
                    ),
                )
                transitions.record_effect(
                    tx,
                    ctx.env,
                    {
                        "kind": "reserved",
                        "op_id": op_id,
                        "session_id": session_id,
                        "profile": profile,
                    },
                )
                tx.event(
                    "task.assigned",
                    task["id"],
                    {
                        "op_id": op_id,
                        "session_id": session_id,
                        "profile": profile,
                        "rationale": rationale,
                    },
                )
            return {
                "kind": "reserved",
                "op_id": op_id,
                "session_id": session_id,
                "profile": profile,
            }

    @staticmethod
    def request_release(ctx, task_id: int, reason: str) -> dict:
        """Persist release intent; parent completion alone cannot certify stopped descendants."""
        coworld = ctx.coworld()
        with admissions.dispatch_lock(ctx.wstore):
            tasks.require_holder(ctx.store, task_id, ctx.actor)
            assignment = ctx.wstore.one(
                "SELECT * FROM task_assignments WHERE coworld_id=? AND task_id=?",
                (coworld.coworld_id, task_id),
            )
            if assignment is None or assignment["session_id"] != ctx.hermes_session_id():
                raise StoreError(["only the assigned worker can request release"])
            with ctx.wstore.tx(ctx.actor) as tx:
                tx.execute(
                    "UPDATE task_assignments SET state='release_requested',"
                    "recovery_deadline=COALESCE(recovery_deadline,?) WHERE op_id=?",
                    (
                        (
                            parse_iso(tx.at)
                            + timedelta(
                                seconds=int(
                                    settings.get(ctx.wstore, "recovery.receipt_timeout_seconds")
                                )
                            )
                        ).isoformat(),
                        assignment["op_id"],
                    ),
                )
                transitions.record_effect(
                    tx,
                    ctx.env,
                    {
                        "kind": "release_pending",
                        "record": tasks.read(ctx.store, task_id),
                        "reason": RELEASE_NEXT,
                    },
                )
                tx.event(
                    "task.release_requested",
                    f"T-{task_id}",
                    {"op_id": assignment["op_id"], "reason": reason},
                )
        return {
            "kind": "release_pending",
            "record": tasks.read(ctx.store, task_id),
            "reason": RELEASE_NEXT,
        }

    def finish_assignments(self, store: Store) -> None:
        """Finish release requests and failed legacy reservations without operator intervention."""
        for assignment in store.q(
            "SELECT t.*,a.status,a.error,a.runtime_id,a.run_id AS attempt_run_id,"
            "a.dispatch_started,a.op_id AS attempt_id FROM task_assignments t"
            " JOIN client_admissions a ON a.op_id=t.current_attempt_id"
            " WHERE NOT EXISTS (SELECT 1 FROM client_admissions obligation"
            " JOIN sessions s ON s.id=obligation.session_id"
            " WHERE s.coworld_id=t.coworld_id AND s.task_id=t.task_id"
            " AND (obligation.execution_uncertain=1 OR obligation.resolution_required=1))"
            " AND (t.state!='held' OR"
            " (a.status IN ('failed','interrupted','cancelled')"
            " AND a.run_id IS NULL))"
        ):
            excluded = (
                self.runtime_id is not None
                and assignment["runtime_id"] is not None
                and self.runtime_id != assignment["runtime_id"]
            )
            status = None
            if (
                assignment["dispatch_started"]
                and not excluded
                and assignment["status"] in {"queued", "running"}
            ):
                if (
                    assignment["attempt_run_id"] is None
                    and assignment["recovery_deadline"]
                    and parse_iso(store.now()) >= parse_iso(assignment["recovery_deadline"])
                ):
                    self.close_unreplayable(
                        store,
                        store.one(
                            "SELECT * FROM client_admissions WHERE op_id=?",
                            (assignment["attempt_id"],),
                        ),
                        "Native stop deadline expired; private runtime exclusion required",
                    )
                    continue
                if assignment["attempt_run_id"] is None:
                    continue
                status = self.native.status(
                    assignment["attempt_run_id"], profile=assignment["profile"]
                )
                if status.status == "unknown":
                    self.close_unreplayable(
                        store,
                        store.one(
                            "SELECT * FROM client_admissions WHERE op_id=?",
                            (assignment["attempt_id"],),
                        ),
                        "Release outcome is unknown",
                    )
                    continue
                if not status.terminal:
                    self.native.stop(assignment["attempt_run_id"], profile=assignment["profile"])
                    continue
            coworld = self.ws.coworld(assignment["coworld_id"])
            with Store.open(coworld.store_path, kind="coworld") as task_store:
                with admissions.dispatch_lock(store):
                    latest = store.one(
                        "SELECT t.*,a.status,a.dispatch_started,a.run_id,a.runtime_id"
                        " FROM task_assignments t JOIN client_admissions a"
                        " ON a.op_id=t.current_attempt_id WHERE t.op_id=?",
                        (assignment["op_id"],),
                    )
                    if latest is None or latest["current_attempt_id"] != assignment["attempt_id"]:
                        continue
                    if store.one(
                        "SELECT 1 FROM client_admissions a JOIN sessions s ON s.id=a.session_id"
                        " WHERE s.coworld_id=? AND s.task_id=?"
                        " AND (a.execution_uncertain=1 OR a.resolution_required=1)",
                        (latest["coworld_id"], latest["task_id"]),
                    ):
                        continue
                    excluded = (
                        self.runtime_id is not None
                        and latest["runtime_id"] is not None
                        and self.runtime_id != latest["runtime_id"]
                    )
                    if (
                        latest["dispatch_started"]
                        and latest["status"] in {"queued", "running"}
                        and not excluded
                        and not (
                            status is not None
                            and status.terminal
                            and status.run_id == latest["run_id"]
                        )
                    ):
                        continue
                    if task_store.one(
                        "SELECT 1 FROM transition_commands WHERE issuer_attempt_id=?"
                        " AND result IS NULL",
                        (latest["current_attempt_id"],),
                    ):
                        continue
                    assignment = dict(assignment) | dict(latest)
                    task = tasks.read(task_store, assignment["task_id"])
                    if (
                        task["held_by"] == assignment["session_id"]
                        and task["status"] == "in-progress"
                    ):
                        actor = Actor.session(assignment["session_id"])
                        if assignment["state"] == "release_requested":
                            tasks.release(
                                task_store,
                                actor,
                                assignment["task_id"],
                                reason="Worker requested release",
                            )
                        else:
                            tasks.abandon(
                                task_store,
                                Actor.harness(),
                                assignment["task_id"],
                                failure={
                                    "what_failed": assignment["error"]
                                    or "Execution could not be recovered",
                                    "next_step": "Review recorded effects and plan a follow-up",
                                },
                            )
                    with store.tx(Actor.harness()) as tx:
                        tx.execute(
                            "DELETE FROM task_assignments WHERE op_id=?", (assignment["op_id"],)
                        )
                        tx.execute(
                            "UPDATE client_admissions SET status='cancelled',cancel_requested=1,"
                            "resolution_required=0 WHERE session_id=? AND status='queued'"
                            " AND run_id IS NULL",
                            (assignment["session_id"],),
                        )
                    sessions.end(store, Actor.harness(), assignment["session_id"])

    @staticmethod
    def adopt_existing_workers(
        ws: layout.Workspace, store: Store, runtime_id: str | None = None
    ) -> None:
        """Migrate existing held workers into the ledger without replaying their old input."""
        for session in store.q(
            "SELECT s.* FROM sessions s LEFT JOIN task_assignments t ON "
            "t.coworld_id=s.coworld_id AND t.task_id=s.task_id"
            " WHERE s.kind='task' AND t.session_id IS NULL"
        ):
            coworld = ws.coworld(session["coworld_id"])
            with Store.open(coworld.store_path, kind="coworld") as task_store:
                with admissions.dispatch_lock(store):
                    task = tasks.read(task_store, session["task_id"])
                    if task["held_by"] != session["id"]:
                        continue
                    with store.tx(Actor.harness()) as tx:
                        op_id = admissions.enqueue_in_transaction(
                            tx,
                            key=f"adopt:{session['id']}:{session['run_id']}",
                            session_id=session["id"],
                            profile=session["profile"],
                            text="Existing worker receipt adopted without input replay",
                            origin="migration",
                        )
                        tx.execute(
                            "UPDATE client_admissions SET run_id=?,status=?,"
                            "dispatch_started=1,execution_uncertain=?,runtime_id=?"
                            " WHERE op_id=?",
                            (
                                session["run_id"],
                                "running" if session["run_id"] else "interrupted",
                                int(session["run_id"] is None),
                                runtime_id,
                                op_id,
                            ),
                        )
                        tx.execute(
                            "INSERT INTO task_assignments"
                            " (coworld_id,task_id,op_id,session_id,profile,task_updated,"
                            "rationale,context)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (
                                session["coworld_id"],
                                session["task_id"],
                                op_id,
                                session["id"],
                                session["profile"],
                                task["updated"],
                                "Preserve existing worker ownership",
                                "Adopted native receipt; no historical input replay",
                            ),
                        )

    def _dispatch_batch(
        self,
        ctx: TickContext,
        label: str,
        store: Store,
        pending: list[Event],
        handler: Handler,
        max_attempts: int,
        report: dict[str, Any],
    ) -> None:
        """One wake Session for every pending event of a ``batch_by_kind`` kind in ``store``.

        Events a live Session already carries wait for it (or retry its start after a busy
        gateway). While such a Session is live, every other event of the kind waits for the next
        batch. Otherwise the events not given up on are claimed for one new Session and started
        together.
        """
        fresh: list[Event] = []
        live: dict[str, tuple[dict[str, Any], list[Event], int]] = {}
        for event in pending:
            ref = f"{label}:{event.seq}"
            row = self._handled_row(store, event.seq)
            if (
                row is not None
                and counted_attempts(ctx, label, event.seq, row["attempts"]) > max_attempts
            ):
                report["gave_up"].append(ref)
                continue
            if row is not None and row["session_id"]:
                session = sessions.read(ctx.wstore, row["session_id"])
                if session is not None and session["ended"] is None:
                    _, carried, _ = live.setdefault(session["id"], (session, [], row["attempts"]))
                    carried.append(event)
                    continue
            fresh.append(event)
        for session, carried, attempts in live.values():
            if session["run_id"]:
                report["waiting"].extend(f"{label}:{event.seq}" for event in carried)
            else:
                self._start_wake(ctx, label, store, carried, handler, session, attempts, report)
        if live:
            report["waiting"].extend(f"{label}:{event.seq}" for event in fresh)
            return
        # Recover a claim committed before its workspace Session was registered.
        session_id = next(
            (
                row["session_id"]
                for event in fresh
                if (row := self._handled_row(store, event.seq)) is not None
                and row["session_id"]
                and sessions.read(ctx.wstore, row["session_id"]) is None
            ),
            None,
        )
        session_id = session_id or hermes_api.mint_session_id()
        batch: list[Event] = []
        first_attempts = 0
        for event in fresh:
            row = self._handled_row(store, event.seq)
            recovering = row is not None and row["session_id"] == session_id
            next_attempt = (
                row["attempts"] if recovering else (row["attempts"] if row is not None else 0) + 1
            )
            if counted_attempts(ctx, label, event.seq, next_attempt) > max_attempts:
                events.claim_handling(
                    store, ctx.actor, event.seq, event.kind, hermes_api.mint_session_id()
                )
                self._give_up(ctx, label, store, event, handler, row, max_attempts)
                report["gave_up"].append(f"{label}:{event.seq}")
                continue
            attempts = (
                row["attempts"]
                if recovering
                else events.claim_handling(store, ctx.actor, event.seq, event.kind, session_id)
            )
            if not batch:
                first_attempts = attempts
            batch.append(event)
        if not batch:
            return
        session = sessions.register(
            ctx.wstore,
            ctx.actor,
            session_id=session_id,
            profile=ctx.orchestrator_profile,
            kind="wake",
            coworld_id=ctx.coworld.coworld_id if ctx.coworld is not None else None,
            event=handler.kind,
        )
        self._start_wake(ctx, label, store, batch, handler, session, first_attempts, report)
