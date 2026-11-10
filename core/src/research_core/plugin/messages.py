"""Associate client context with the native message row, without rewriting history."""

import json
from collections.abc import Callable

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span

from research_core.telemetry import propagator


def record_native_message(
    *,
    session_id: str,
    conversation_history: list,
    bind_origin: Callable[[str, Context], Span | None],
    **_,
) -> None:
    """Bind application metadata to the native row ID supplied by the pinned observer hook.

    The turn's user message is the last one Hermes committed, which has a row ID. A later user
    message is one Hermes added inside the turn and never commits, such as the nudge that
    continues a truncated stream.
    """
    from research_core import layout
    from research_core.agent_telemetry import observed
    from research_core.plugin.executor import profile_env
    from research_core.store.actors import Actor
    from research_core.store.db import Store
    from research_core.telemetry import correlation

    native_id = next(
        message["_row_id"]
        for message in reversed(conversation_history)
        if message["role"] == "user" and "_row_id" in message
    )
    workspace = layout.from_env(profile_env())
    store = Store.open(workspace.store_path, kind="workspace")
    try:
        admission = store.one(
            "SELECT op_id,payload,resumes FROM client_admissions"
            " WHERE session_id=? AND kind='message'"
            " AND ((native_message_id IS NULL AND resumes=0) OR resumes>0)"
            " AND dispatch_started=1"
            " AND status IN ('queued','running') ORDER BY (resumes>0),rowid LIMIT 1",
            (session_id,),
        )
        if admission is None:
            # Background turns have no client admission.
            return
        # Original binding candidates retain precedence over any resumed admission.
        # A continuation is never substituted for the original user message.
        if admission["resumes"] == 0:
            with store.tx(Actor.harness()) as tx:
                tx.execute(
                    "UPDATE client_admissions SET native_message_id=? WHERE op_id=?"
                    " AND native_message_id IS NULL",
                    (native_id, admission["op_id"]),
                )
    finally:
        store.close()

    # Reuse the required application binding read; tracing never opens another store.
    # The turn joins the message's trace: the admission carries the context it was admitted in.
    # An admission stored before every admission carried one has none (``stored formats``).
    payload = json.loads(admission["payload"])
    turn_span = bind_origin(session_id, propagator.extract(payload.get("trace_context", {})))
    if turn_span is None:
        return
    if isinstance(turn_span, ReadableSpan) and "op.id" not in turn_span.attributes:
        turn_span.set_attribute("op.id", admission["op_id"])
    if admission["resumes"] == 0:
        # The one place that sees an admission beside its native row and the turn serving it.
        observed.record(
            "admission",
            "bound",
            admission_id=admission["op_id"],
            session_id=session_id,
            hermes_message_id=str(native_id),
            **correlation(turn_span),
        )
