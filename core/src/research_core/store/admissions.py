"""Durable client receipts and queued chat inputs, owned by the workspace.

A message admission is ``queued`` until ``research serve``'s dispatcher starts its run, then
``running``, and ends ``completed``, ``cancelled``, ``failed`` or ``interrupted``. A run that
failed for a budget, rate-limit or provider reason leaves it ``waiting`` instead: it resumes on
its own (``research_core.run_failures``). ``error_class`` names the reason for a ``waiting``
or ``failed`` admission when it is known, and ``resume_after`` is the earliest time a
``waiting`` one resumes. The table stores a waiting admission as ``queued`` with
``resume_after`` set (no run, ``dispatch_started`` 0); :func:`snapshot` reports it as
``waiting``, and ``resumes`` counts its resumes.
"""

import fcntl
import json
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, get_args
from uuid import NAMESPACE_URL, uuid5

from research_core.run_failures import ErrorClass
from research_core.store import settings
from research_core.store.actors import Actor
from research_core.store.db import Store, Tx
from research_core.store.errors import StoreError
from research_core.telemetry import span, trace_carrier

Origin = Literal[
    "client",
    "answer",
    "assignment",
    "continuation",
    "planning",
    "wake",
    "watcher",
    "migration",
    "followup",
]
_held_locks = threading.local()


@contextmanager
def dispatch_lock(store: Store):
    """Serialize the start/receipt boundary with the separate sleep command."""
    path = store.path.parent / "client-dispatch.lock"
    if not hasattr(_held_locks, "paths"):
        _held_locks.paths = set()
    if path in _held_locks.paths:
        yield
        return
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _held_locks.paths.add(path)
        try:
            yield
        finally:
            _held_locks.paths.remove(path)
            fcntl.flock(lock, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Admission:
    op_id: str
    session_id: str
    message: str
    input: dict
    status: Literal[
        "queued", "running", "waiting", "completed", "cancelled", "failed", "interrupted"
    ]
    run_id: str | None
    error: str | None
    error_class: ErrorClass | None
    resume_after: str | None
    created_at: str
    client_id: str | None


def read(store: Store, op_id: str, kind: str, request: dict) -> dict | None:
    row = store.one("SELECT * FROM client_admissions WHERE op_id = ?", (op_id,))
    if row is None:
        return None
    if row["kind"] != kind or json.loads(row["request"]) != request:
        raise StoreError(["op_id was already used for different input"])
    return dict(row)


def snapshot(store: Store, session_id: str | None = None) -> list[dict]:
    return [
        asdict(Admission(**{**dict(row), "input": json.loads(row["input"])}))
        for row in store.q(
            "SELECT op_id,session_id,message,"
            " CASE WHEN origin='answer' THEN json_object('role','user','content',message)"
            " ELSE json_extract(request, '$.message') END AS input,"
            " CASE WHEN status='queued' AND resume_after IS NOT NULL THEN 'waiting'"
            " ELSE status END AS status,run_id,error,error_class,resume_after,created_at,"
            " json_extract(payload, '$.context.client.id') AS client_id"
            " FROM client_admissions WHERE kind = 'message'"
            " AND origin IN ('client','answer')"
            " AND (? IS NULL OR session_id=?) ORDER BY rowid",
            (session_id, session_id),
        )
    ]


def requests(store: Store, session_ids: list[str]) -> list[dict]:
    """Every message admission of these sessions as AIDE's request records read it.

    Beside :func:`snapshot`'s fields: ``origin``; ``resumes``; ``revision`` and ``updated_at``,
    the seq and time of the latest event about it; ``runs``, each run it started with its
    ``attempt`` (the resumes before that run, from ``client.resumed``) and whether it was
    ``steered`` (another admission's live run this one's message joined); and ``logged``, the last
    row of its event log (:func:`append_event`), once it has one. ``input`` is the user's
    message, or ``None`` for runtime work.
    """
    rows = store.q(
        "SELECT op_id,session_id,origin,resumes,created_at,error,error_class,resume_after,"
        " CASE WHEN origin='answer' THEN json_object('role','user','content',message)"
        " WHEN origin='client' THEN json_extract(request, '$.message') END AS input,"
        " CASE WHEN status='queued' AND resume_after IS NOT NULL THEN 'waiting'"
        " ELSE status END AS status,"
        " (SELECT seq FROM events WHERE subject=op_id ORDER BY seq DESC LIMIT 1) AS revision,"
        " (SELECT at FROM events WHERE subject=op_id ORDER BY seq DESC LIMIT 1) AS updated_at,"
        " (SELECT json_object('attempt',attempt,'seq',seq,'run_id',run_id,"
        "'source_seq',source_seq,'event',json(event)) FROM request_events"
        " WHERE request_id=op_id ORDER BY attempt DESC, seq DESC LIMIT 1) AS logged"
        " FROM client_admissions WHERE kind='message'"
        " AND session_id IN (SELECT value FROM json_each(?)) ORDER BY created_at, rowid",
        (json.dumps(session_ids),),
    )
    runs: dict[str, list[dict]] = {row["op_id"]: [] for row in rows}
    attempts = dict.fromkeys(runs, 0)
    for event in store.q(
        "SELECT subject,kind,payload FROM events"
        " WHERE kind IN ('client.started','client.resumed')"
        " AND subject IN (SELECT op_id FROM client_admissions WHERE kind='message'"
        " AND session_id IN (SELECT value FROM json_each(?))) ORDER BY seq",
        (json.dumps(session_ids),),
    ):
        payload = json.loads(event["payload"])
        if event["kind"] == "client.resumed":
            attempts[event["subject"]] = payload["resume"]
        elif payload["run_id"] not in {run["run_id"] for run in runs[event["subject"]]}:
            runs[event["subject"]].append(
                {
                    "attempt": attempts[event["subject"]],
                    "run_id": payload["run_id"],
                    "steered": "steered" in payload,
                }
            )
    return [
        {
            **dict(row),
            "input": None if row["input"] is None else json.loads(row["input"]),
            "runs": runs[row["op_id"]],
            "logged": None if row["logged"] is None else json.loads(row["logged"]),
        }
        for row in rows
    ]


def append_event(
    tx: Tx, op_id: str, event: dict, run_id: str | None, source_seq: int | None
) -> None:
    """Append one AIDE ``ChatEvent`` to its request's log at the cursor it carries.

    The log's triggers keep each attempt's seqs contiguous and ``finished`` last; only a
    finished admission has an outcome to append. ``run_id`` and ``source_seq`` name the last
    gateway event folded into this one.
    """
    if event["kind"] == "finished":
        status = tx.one(
            "SELECT status FROM client_admissions WHERE op_id=? AND kind='message'", (op_id,)
        )
        if status is None or status["status"] not in {
            "completed",
            "cancelled",
            "failed",
            "interrupted",
        }:
            raise StoreError(["only a finished message admission has an outcome to append"])
    tx.execute(
        "INSERT INTO request_events(request_id,attempt,seq,kind,event,run_id,source_seq)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            op_id,
            event["attempt"],
            event["seq"],
            event["kind"],
            json.dumps(event),
            run_id,
            source_seq,
        ),
    )


def events_after(store: Store, op_id: str, attempt: int, seq: int, limit: int) -> dict:
    """Up to ``limit`` logged events after ``(attempt, seq)``, in cursor order; ``more`` when
    further rows follow them and ``closed`` once the log holds its ``finished`` event."""
    with store.read_tx():
        rows = store.q(
            "SELECT event FROM request_events WHERE request_id=?"
            " AND (attempt>? OR (attempt=? AND seq>?)) ORDER BY attempt, seq LIMIT ?",
            (op_id, attempt, attempt, seq, limit + 1),
        )
        closed = store.one(
            "SELECT 1 FROM request_events WHERE request_id=? AND kind='finished'", (op_id,)
        )
    return {
        "events": [json.loads(row["event"]) for row in rows[:limit]],
        "more": len(rows) > limit,
        "closed": closed is not None,
    }


def enqueue(
    store: Store,
    actor: Actor,
    *,
    key: str,
    session_id: str,
    profile: str,
    text: str,
    origin: Origin,
    instructions: str | None = None,
    delivery_ids: tuple[int, ...] = (),
) -> str:
    """Persist one immutable native input; replay cannot change its assignment or body."""
    with store.tx(actor) as tx:
        return enqueue_in_transaction(
            tx,
            key=key,
            session_id=session_id,
            profile=profile,
            text=text,
            origin=origin,
            instructions=instructions,
            delivery_ids=delivery_ids,
        )


def enqueue_in_transaction(
    tx: Tx,
    *,
    key: str,
    session_id: str,
    profile: str,
    text: str,
    origin: Origin,
    instructions: str | None = None,
    delivery_ids: tuple[int, ...] = (),
) -> str:
    if origin not in get_args(Origin):
        raise StoreError([f"origin must be one of {', '.join(get_args(Origin))}"])
    op_id = str(uuid5(NAMESPACE_URL, f"research:{profile}:{key}"))
    request = {
        "key": key,
        "session_id": session_id,
        "profile": profile,
        "origin": origin,
        "text": text,
        "instructions": instructions,
        "delivery_ids": list(delivery_ids),
    }
    row = tx.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
    if row is not None:
        if row["request"] != json.dumps(request, sort_keys=True):
            raise StoreError(["execution identity already belongs to a different payload"])
        return op_id
    # The producer's span, as for a message put on a queue: the root of what this input causes,
    # or a child of the span that admitted it, such as a tool call assigning a Task.
    with span("workspace.admission", **{"op.id": op_id, "session.id": session_id}):
        carrier = trace_carrier()
    tx.execute(
        "INSERT INTO client_admissions"
        " (op_id,kind,request,session_id,message,payload,status,created_at,"
        "profile,origin,native_key,delivery_ids)"
        " VALUES (?,'message',?,?,?,?,'queued',?,?,?,?,?)",
        (
            op_id,
            json.dumps(request, sort_keys=True),
            session_id,
            text,
            json.dumps(
                {
                    "input": text,
                    "instructions": instructions,
                    "context": None,
                    "trace_context": carrier,
                }
            ),
            tx.at,
            profile,
            origin,
            key,
            json.dumps(list(delivery_ids)),
        ),
    )
    tx.event(
        "execution.queued", op_id, {"session_id": session_id, "profile": profile, "origin": origin}
    )
    return op_id


def reserved_workers(store: Store, exclude_session: str | None = None) -> int:
    return store.one(
        "SELECT COUNT(DISTINCT a.session_id) FROM client_admissions a"
        " JOIN sessions s ON s.id=a.session_id WHERE s.kind='task'"
        " AND (? IS NULL OR a.session_id!=?)"
        " AND (a.status IN ('queued','running')"
        " OR a.resolution_required=1 OR a.execution_uncertain=1)",
        (exclude_session, exclude_session),
    )[0]


def problems(store: Store) -> list[dict]:
    return [
        dict(row)
        for row in store.q(
            "SELECT op_id,profile,error AS reason FROM client_admissions"
            " WHERE resolution_required=1"
        )
    ]


def work_reasons(store: Store) -> list[str]:
    """The admissions that keep the workspace awake, as ``admission:<op id>:<status>``.

    Only work that can progress while the workspace is awake counts. A held (``waiting``)
    message, a refused start waiting out its ``retry_at`` backoff, the messages queued behind
    either in the same Session, and an admission that needs resolution do not: each waits on
    time, a refill or the user, and the dispatcher picks it up again once the workspace wakes.
    """
    return [
        f"admission:{row['op_id']}:{row['status']}"
        for row in store.q(
            "SELECT op_id,status FROM client_admissions a WHERE a.resolution_required=0"
            " AND (a.status='running' OR (a.status='queued' AND a.resume_after IS NULL"
            " AND a.retry_at IS NULL AND NOT EXISTS (SELECT 1 FROM client_admissions held"
            " WHERE held.session_id=a.session_id AND held.status='queued'"
            " AND (held.resume_after IS NOT NULL OR held.retry_at IS NOT NULL)"
            " AND held.rowid<a.rowid))) ORDER BY a.rowid"
        )
    ]


def restart_required(store: Store) -> bool:
    return (
        store.one("SELECT 1 FROM client_admissions WHERE execution_uncertain=1 LIMIT 1") is not None
    )


def work_status(store: Store, lease_file: Path | None = None) -> list[dict]:
    """Visible execution obligations, including terminal deliveries with uncertain execution."""
    from research_core import inference

    owner = store.get_meta("coordinator_instance") or "workspace coordinator awaiting startup"
    paused = settings.get(store, "paused")
    result = []
    for row in store.q(
        "SELECT a.*,t.state AS assignment_state FROM client_admissions a"
        " LEFT JOIN task_assignments t ON t.current_attempt_id=a.op_id"
        " WHERE a.kind='message' AND (a.status IN ('queued','running')"
        " OR a.execution_uncertain=1 OR a.resolution_required=1 OR a.advanced_at IS NULL"
        " OR a.disposition='delivery retries exhausted') ORDER BY a.rowid"
    ):
        phase = row["status"]
        responsible = row["owner_instance"] or owner
        reason = row["error"]
        trigger = None
        if row["execution_uncertain"] or row["resolution_required"]:
            phase = "waiting"
            responsible = f"runtime:{row['runtime_id']}"
            reason = reason or "execution exclusion required"
            trigger = "private runtime namespace replaced; coordinator reconciles evidence"
        elif phase == "queued":
            responsible = owner
            if row["resume_after"]:
                phase, reason = "waiting", row["error_class"]
                trigger = f"coordinator resumes this message after {row['resume_after']}"
            elif row["retry_at"]:
                phase, reason = "waiting", "transient steering rejection"
                trigger = f"coordinator retries this delivery at {row['retry_at']}"
            elif paused and not row["dispatch_started"]:
                phase, reason, trigger = "waiting", "workspace paused", "workspace resume"
            elif not row["dispatch_started"] and not inference.available(lease_file):
                blocked = inference.blocking_state(lease_file)
                phase, responsible = "waiting", f"runtime:{store.get_meta('runtime_id')}"
                reason = "inference lease unavailable" if blocked is None else blocked.blocked
                trigger = "new inference lease/key, or provider backoff deadline"
            else:
                reason, trigger = "durable admission queued", "coordinator dispatch"
        elif phase == "running":
            responsible = f"native:{row['profile']}:{row['run_id']}"
            if row["assignment_state"] == "release_requested":
                phase = "waiting"
                reason = "recorded run is stopping; task ownership retained"
                trigger = "coordinator polls this run until terminal outcome"
            else:
                reason, trigger = (
                    "execution active",
                    "native outcome; reconciliation recovers missed facts",
                )
        elif row["advanced_at"] is None:
            responsible = owner
            phase, reason, trigger = (
                "advancing",
                "outcome committed",
                "coordinator applies durable handoff",
            )
        result.append(
            {
                "attempt_id": row["op_id"],
                "session_id": row["session_id"],
                "state": phase,
                "authority": owner,
                "owner": responsible,
                "reason": reason,
                "trigger": trigger,
                "outcome": row["status"],
                "disposition": row["disposition"],
                "successor_id": row["successor_id"],
            }
        )
    return result
