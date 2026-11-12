"""HTTP intake validation and atomic receipts.

OpenTelemetry spans record transaction boundaries without copying request payloads.
"""

import json

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict

from research_core.store import admissions, sessions
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.telemetry import span, trace_carrier


class OpenRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str
    coworld_id: str | None
    session_context: dict | None
    client_capabilities: dict | list | None
    client: dict | None = None


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    run_id: str | None
    replayed: bool


@span("intake.transaction")
def accept(
    store: Store,
    actor: Actor,
    *,
    op_id: str,
    kind: str,
    request: dict,
    session_id: str,
    message: str,
    payload: str,
    registration: OpenRegistration | None = None,
) -> Receipt:
    current = trace.get_current_span()
    current.set_attributes({"op.id": op_id, "session.id": session_id})
    current.add_event("writer.wait")
    with store.tx(actor) as tx:
        current.add_event("writer.acquired")
        existing = admissions.read(store, op_id, kind, request)
        if existing is None:
            if kind == "message":
                payload = json.dumps({**json.loads(payload), "trace_context": trace_carrier()})
            tx.execute(
                "INSERT INTO client_admissions"
                " (op_id,kind,request,session_id,message,payload,status,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    op_id,
                    kind,
                    json.dumps(request, sort_keys=True),
                    session_id,
                    message,
                    payload,
                    "queued",
                    tx.at,
                ),
            )
            tx.event("client.accepted", op_id, {"kind": kind, "session_id": session_id})
        current.add_event("admission.written")
        if registration is not None:
            sessions.register_in_transaction(
                tx,
                session_id=existing["session_id"] if existing is not None else session_id,
                kind="chat",
                **registration.model_dump(),
            )
            current.add_event("session.registered")
    current.add_event("transaction.committed")
    current.set_attribute("replayed", existing is not None)
    return Receipt(
        session_id=existing["session_id"] if existing is not None else session_id,
        run_id=existing["run_id"] if existing is not None else None,
        replayed=existing is not None,
    )
