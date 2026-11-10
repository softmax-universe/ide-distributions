"""Translate Hermes lifecycle IDs into OpenTelemetry spans; the SDK owns trace mechanics.

Native callbacks can arrive on different workers, so parent contexts are passed explicitly.
No prompts, arguments, results or exception messages are recorded. Observed callback bounds
avoid Hermes's cumulative retry clock. Missing terminal hooks never imply completion.

One user message is one trace. A turn's ``invoke_agent`` span is a child of the message's
admission (``bind_origin``, from ``plugin/messages.py``), so every Hermes turn that works on the
message, including one that resumes it, joins the message's trace. The admission is known only at
the turn's first model request, so the span starts then, with the turn's own start time. A turn
with no admission (background work) is a trace of its own, and a delegated child's turn is a child
of its parent's turn.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, StatusCode

from research_core import telemetry
from research_core.telemetry import correlation
from research_core.tools.catalog import Role


@dataclass
class Turn:
    session_id: str
    turn_id: str
    attributes: dict[str, Any]
    started_ns: int
    span: Span | None = None
    complete: bool = True
    model_attempts: int = 0
    attempts: dict[str, Span] = field(default_factory=dict)
    tools: dict[str, Span] = field(default_factory=dict)
    tool_names: dict[str, str] = field(default_factory=dict)
    # Every attempt and tool span the turn had, ended or not, for the records that name them.
    spans: dict[str, Span] = field(default_factory=dict)
    tool_calls: int = 0
    tool_failures: int = 0

    def start(self, parent: Context | None) -> Span:
        """The turn's span, started once: under ``parent``, or as a trace of its own."""
        if self.span is None:
            self.span = telemetry.tracer().start_span(
                "invoke_agent",
                context=parent,
                attributes=self.attributes,
                start_time=self.started_ns,
            )
        return self.span

    def count(self) -> None:
        self.span.set_attributes(
            {
                "gen_ai.turn.model_calls": self.model_attempts,
                "gen_ai.turn.tool_calls": self.tool_calls,
                "gen_ai.turn.tool_failures": self.tool_failures,
            }
        )


def native_error_type(status: str, error_type: str | None, result: Any) -> str | None:
    """The failure class of a native tool call, or ``None`` when it succeeded.

    Hermes names most classes itself (``tool_timeout``, ``invalid_tool_arguments``, an exception's
    class). Its ``tool_error`` covers every failure a tool reports in its result, so this reads
    the result's structured fields for a finer class: a distribution tool's ``kind`` on an
    ``ok: false`` result, a ``timeout`` or ``degraded`` status, or a terminal's exit code. Free
    text such as ``error`` is never read: a file-not-found stays ``tool_error``.
    """
    if status == "ok":
        return None
    if error_type != "tool_error":
        return error_type or status
    if isinstance(result, str) and result.startswith("{") and result.endswith("}"):
        result = json.loads(result)
    if not isinstance(result, dict):
        return error_type
    # Tool results have no shared schema, so each field is optional.
    kind = result.get("kind")
    if result.get("ok") is False and telemetry.is_error_type(kind):
        return kind
    if result.get("status") in ("timeout", "degraded"):
        return result["status"]
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return "timeout" if exit_code == 124 else "exit_code_nonzero"
    return error_type


class Traces:
    """The turns of one profile, by Hermes's turn ID, and the calls a handler and a record need."""

    def __init__(self, role: Role) -> None:
        self.role = role
        self.lock = threading.Lock()
        self.turns: dict[str, Turn] = {}
        # Each Session's latest turn, also after it ended: records of a turn are written after its
        # end too (its committed rows), and an auxiliary call can end after the turn does.
        self.latest: dict[str, Turn] = {}

    def session_turn(self, session_id: str) -> Turn | None:
        """The Session's running turn once its span started. Hermes invokes registered tool
        handlers on isolated workers, and the coordinator permits one active turn per Session."""
        with self.lock:
            return next(
                (
                    turn
                    for turn in self.turns.values()
                    if turn.session_id == session_id and turn.span is not None
                ),
                None,
            )

    def bind_origin(self, session_id: str, origin: Context) -> Span | None:
        """The Session's running turn joins the trace of the message it works on, at its first
        model request. A later request finds the span started and changes nothing."""
        with self.lock:
            turn = next((t for t in self.turns.values() if t.session_id == session_id), None)
            return None if turn is None else turn.start(origin)

    def tool_call(self, session_id: str, tool_name: str) -> tuple[Span, str] | None:
        """The native call a handler of ``tool_name`` serves: the one such call open in the
        Session's turn. Hermes hands a handler no call ID, so two open calls of one tool name say
        nothing about which is whose."""
        with self.lock:
            turn = next((t for t in self.turns.values() if t.session_id == session_id), None)
            if turn is None:
                return None
            open_calls = [call for call, name in turn.tool_names.items() if name == tool_name]
            if len(open_calls) != 1:
                return None
            return turn.tools[open_calls[0]], open_calls[0]

    def correlation(
        self, session_id: str, api_request_id: str | None = None, tool_call_id: str | None = None
    ) -> dict[str, str]:
        """The span a record of the Session belongs to: its model attempt or tool call, else the
        Session's latest turn. None before that turn's span starts."""
        with self.lock:
            turn = self.latest.get(session_id)
            if turn is None or turn.span is None:
                return {}
            span = turn.spans.get(tool_call_id or "") or turn.spans.get(api_request_id or "")
            return correlation(span or turn.span)


def make_instrumentation(
    role: Role,
) -> tuple[dict[str, Callable[..., None]], Traces]:
    traces = Traces(role)
    turns, lock = traces.turns, traces.lock

    def incomplete(turn: Turn, reason: str) -> None:
        turn.start(None)
        turn.count()
        for current in (*turn.attempts.values(), *turn.tools.values(), turn.span):
            current.set_attributes({"observation.complete": False, "observation.reason": reason})
            current.set_status(StatusCode.ERROR)
            current.end()  # Observation end, explicitly not native completion.

    def discard_session(*, session_id: str, reason: str, **_: Any) -> None:
        with lock:
            traces.latest.pop(session_id, None)
            for turn_id, turn in list(turns.items()):
                if turn.session_id == session_id:
                    incomplete(turn, reason)
                    del turns[turn_id]

    def pre_llm_call(
        *,
        session_id: str,
        task_id: str,
        turn_id: str,
        parent_session_id: str = "",
        conversation_history: list | None = None,
        **_: Any,
    ) -> None:
        with lock:
            for previous, turn in list(turns.items()):
                if turn.session_id == session_id:
                    incomplete(turn, "next_turn_in_session")
                    del turns[previous]
            if len(turns) >= 256:
                oldest = next(iter(turns))
                incomplete(turns.pop(oldest), "tracking_capacity")
            turn = Turn(
                session_id,
                turn_id,
                {
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": role,
                    "session.id": session_id,
                    "task.id": task_id,
                    "turn.id": turn_id,
                    "observation.boundary": "pre_llm_call_to_session_end",
                    "gen_ai.turn.model_calls": 0,
                },
                time.time_ns(),
            )
            parent = next(
                (t for t in turns.values() if t.session_id == parent_session_id and t.span), None
            )
            if parent is not None:
                # A delegated child works inside its parent's turn, so it is in that trace.
                turn.start(trace.set_span_in_context(parent.span))
            turns[turn_id] = turn
            traces.latest.pop(session_id, None)
            traces.latest[session_id] = turn
            if len(traces.latest) > 1024:
                del traces.latest[next(iter(traces.latest))]

    def pre_api_request(
        *,
        turn_id: str,
        api_request_id: str,
        retry_count: int,
        conversation_history: list | None = None,
        **_: Any,
    ) -> None:
        with lock:
            if turn_id not in turns:
                return
            turn = turns[turn_id]
            # ``bind_origin`` ran first when the turn works on a client's message.
            span = turn.start(None)
            turn.model_attempts += 1
            span.set_attribute("gen_ai.turn.model_calls", turn.model_attempts)
            turn.attempts[api_request_id] = turn.spans[api_request_id] = (
                telemetry.tracer().start_span(
                    "chat",
                    kind=SpanKind.CLIENT,
                    context=trace.set_span_in_context(span),
                    attributes={
                        "gen_ai.operation.name": "chat",
                        "gen_ai.agent.name": role,
                        "request.id": api_request_id,
                        "retry.count": retry_count,
                    },
                )
            )

    def model_done(
        *,
        turn_id: str,
        api_request_id: str,
        model: str,
        provider: str,
        failed: bool = False,
        usage: dict | None = None,
        status_code: int | None = None,
        first_chunk_at: float | None = None,
        reason: str | None = None,
        error: dict | None = None,
        **_: Any,
    ) -> None:
        with lock:
            if turn_id not in turns:
                return
            turn = turns[turn_id]
            current = turn.attempts.pop(api_request_id, None)
            if current is None:
                turn.complete = False
                turn.start(None).add_event("observation.missing_start", {"kind": "model"})
                return
            current.set_attributes(
                {"gen_ai.request.model": model, "gen_ai.provider.name": provider}
            )
            if status_code is not None:
                current.set_attribute("http.response.status_code", status_code)
            current.set_attribute("gen_ai.usage.reported", usage is not None)
            if usage is not None:
                for source, target in (
                    ("prompt_tokens", "input_tokens"),
                    ("output_tokens", "output_tokens"),
                    ("cache_read_tokens", "cache_read.input_tokens"),
                    ("cache_write_tokens", "cache_write.input_tokens"),
                ):
                    current.set_attribute(f"gen_ai.usage.{target}", usage[source])
            if first_chunk_at is not None:
                current.add_event("response.first_chunk", timestamp=int(first_chunk_at * 1e9))
            current.set_attribute("outcome", "failed" if failed else "succeeded")
            current.set_attribute("operation.success", 0 if failed else 1)
            if failed:
                # Hermes's classification (``rate_limit``, ``server_error``, ``timeout``...) and the
                # exception's class. A failure inside a stream that answered 200, such as
                # OpenRouter's "Provider returned error" event, has no status code.
                current.set_attributes({"error.type": reason, "exception.type": error["type"]})
                current.set_status(StatusCode.ERROR)
            current.end()

    def pre_tool_call(*, turn_id: str, tool_call_id: str, tool_name: str = "", **_: Any) -> None:
        with lock:
            if turn_id in turns:
                turn = turns[turn_id]
                turn.tool_calls += 1
                turn.tool_names[tool_call_id] = tool_name
                turn.tools[tool_call_id] = turn.spans[tool_call_id] = telemetry.tracer().start_span(
                    "execute_tool",
                    context=trace.set_span_in_context(turn.start(None)),
                    attributes={
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.agent.name": role,
                        "gen_ai.tool.call.id": tool_call_id,
                        "tool.boundary": "native",
                        "session.id": turn.session_id,
                        "turn.id": turn_id,
                    },
                )

    def post_tool_call(
        *,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        status: str,
        session_id: str = "",
        error_type: str | None = None,
        result: Any = None,
        duration_ms: float = 0,
        **_: Any,
    ) -> None:
        failure = native_error_type(status, error_type, result)
        current = None
        with lock:
            if turn_id in turns:
                turn = turns[turn_id]
                if failure is not None:
                    turn.tool_failures += 1
                current = turn.tools.pop(tool_call_id, None)
                turn.tool_names.pop(tool_call_id, None)
                if current is None:
                    turn.complete = False
                    turn.start(None).add_event("observation.missing_start", {"kind": "tool"})
        # Logged even with no span to end: this line replaces Hermes's own, which held the output.
        telemetry.finish_tool_call(
            current or trace.INVALID_SPAN,
            boundary="native",
            role=role,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            outcome=status,
            error_type=failure,
            duration_s=duration_ms / 1000,
        )
        if current is not None:
            current.end()

    def on_session_end(
        *,
        turn_id: str,
        completed: bool,
        failed: bool,
        interrupted: bool,
        **_: Any,
    ) -> None:
        with lock:
            if turn_id not in turns:
                return
            turn = turns.pop(turn_id)
            if turn.attempts or turn.tools or not turn.complete:
                incomplete(turn, "missing_callbacks")
                return
            span = turn.start(None)  # A turn with no model request, such as a vetoed one.
            turn.count()
            span.set_attribute("observation.complete", True)
            span.set_attribute(
                "outcome",
                "interrupted"
                if interrupted
                else "succeeded"
                if completed and not failed
                else "failed",
            )
            if failed or interrupted or not completed:
                span.set_status(StatusCode.ERROR)
            span.end()

    hooks = {
        "pre_llm_call": pre_llm_call,
        "pre_api_request": pre_api_request,
        "post_api_request": model_done,
        "api_request_error": partial(model_done, failed=True),
        "pre_tool_call": pre_tool_call,
        "post_tool_call": post_tool_call,
        "on_session_end": on_session_end,
        **{
            name: partial(discard_session, reason=name)
            for name in ("on_session_finalize", "on_session_reset", "on_session_delete")
        },
    }
    return hooks, traces
