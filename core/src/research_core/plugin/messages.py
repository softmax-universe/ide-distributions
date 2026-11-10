"""Associate client context with the native message row, without rewriting history."""

import json
from collections.abc import Callable

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span

from research_core.telemetry import propagator


def record_native_message(
    *,
    session_id: str,
    conversation_history: list,
    span_for_session: Callable[[str], Span],
    user_message: object = None,
    run_id: str | None = None,
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

    native_id = next(
        message["_row_id"]
        for message in reversed(conversation_history)
        if message["role"] == "user" and "_row_id" in message
    )
    workspace = layout.from_env(profile_env())
    store = Store.open(workspace.store_path, kind="workspace")
    try:
        admission = store.one(
            "SELECT op_id,run_id,payload,resumes FROM client_admissions"
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
            # The one place that sees an admission beside its native row and the run Hermes bound.
            observed.record(
                "admission",
                "bound",
                admission_id=admission["op_id"],
                session_id=session_id,
                run_id=run_id,
                hermes_message_id=str(native_id),
            )
    finally:
        store.close()

    # Reuse the required application binding read; tracing never opens another store.
    # An optional trace annotation must not change the binding's functional outcome.
    try:
        current_span = span_for_session(session_id)
        if isinstance(current_span, ReadableSpan) and "op.id" in current_span.attributes:
            return  # Later model iterations cannot relink the turn to an older resume.
        current_span.set_attribute("op.id", admission["op_id"])
        if admission["run_id"] is not None:
            current_span.set_attribute("run.id", admission["run_id"])
        origin = trace.get_current_span(
            propagator.extract(json.loads(admission["payload"])["trace_context"])
        ).get_span_context()
        if origin.is_valid:
            current_span.add_link(origin)
    except Exception:
        pass
