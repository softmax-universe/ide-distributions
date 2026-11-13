"""Payload-free OpenTelemetry for both workspace and native Hermes interpreters.

The SDK owns clocks, IDs, context and export. Bounded standard batch processors keep both
console and remote delivery off functional threads; process exit does not flush telemetry.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import OTELResourceDetector, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)
from opentelemetry.trace import Span, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from research_core.agent_telemetry.redaction import mask_softmax_tokens

_provider: TracerProvider | trace.NoOpTracerProvider | None = None
_lock = threading.Lock()
_logging_started = False
propagator = TraceContextTextMapPropagator()


class JsonLines(logging.Formatter):
    """One record as one JSON line, for Datadog, which reads the container's output.

    ``status`` becomes the log's status, so a warning on stderr is no longer an error, and a
    traceback stays inside its record's ``message`` instead of arriving as one log per line.
    Fields a caller passes as ``extra={"attributes": {...}}`` become top-level fields. The
    message and each string field go through ``redact``, because a field can hold what the model
    chose, such as a tool name or call ID.
    """

    def __init__(self, redact: Callable[[str], str]) -> None:
        super().__init__()
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        created = datetime.fromtimestamp(record.created, UTC)
        attributes = getattr(record, "attributes", {})
        return json.dumps(
            {
                # First, so a caller's field cannot replace the redacted message or the status.
                **{k: self.redact(v) if isinstance(v, str) else v for k, v in attributes.items()},
                "timestamp": created.isoformat(timespec="milliseconds"),
                "status": record.levelname.lower(),
                "logger.name": record.name,
                # The message with any traceback, which the queue in front has already joined.
                "message": self.redact(super().format(record)),
            },
            default=str,
        )


def json_console(handlers: Iterable[logging.Handler], redact: Callable[[str], str]) -> None:
    """Give each console handler JSON lines. A file handler keeps its format."""
    for handler in handlers:
        if (
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
            and not isinstance(handler.formatter, JsonLines)
        ):
            handler.setFormatter(JsonLines(redact))


LOG_QUEUE_SIZE = 2048


class DroppingQueueHandler(QueueHandler):
    """Never waits for a destination: a record that does not fit in the queue is counted instead."""

    def __init__(self) -> None:
        super().__init__(queue.Queue(maxsize=LOG_QUEUE_SIZE))
        self.dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        # ``Handler.handle`` holds this handler's lock, and only the listener takes records out.
        if self.queue.full():
            self.dropped += 1
        else:
            self.queue.put_nowait(record)


class DropReportingListener(QueueListener):
    """Tells the destinations how many records the queue dropped, before the next one it passes."""

    def __init__(self, source: DroppingQueueHandler, *handlers: logging.Handler) -> None:
        super().__init__(source.queue, *handlers, respect_handler_level=True)
        self.source = source
        self.reported = 0

    def handle(self, record: logging.LogRecord) -> None:
        if self.source.dropped != self.reported:
            self.reported = self.source.dropped
            notice = logging.makeLogRecord(
                {
                    "name": __name__,
                    "levelno": logging.WARNING,
                    "levelname": "WARNING",
                    "msg": f"logging: {self.reported} records dropped so far",
                }
            )
            super().handle(notice)
        super().handle(record)


def configure_logging() -> None:
    """Standard bounded logging queue; slow handlers never run on functional threads.

    Console handlers write JSON lines (``JsonLines``). The gateway plugin gives Hermes's own
    console handler Hermes's scanner first; any other gets the Softmax token masks.
    """
    global _logging_started
    if _logging_started:
        return
    # Python logging otherwise flushes/locks every handler at interpreter exit.
    # Telemetry is best effort; exit must not wait for a blocked destination.
    atexit.unregister(logging.shutdown)
    logging.raiseExceptions = False
    root = logging.getLogger()
    # Agent telemetry's handler only enqueues, so it cannot block a caller, and behind this queue a
    # stuck handler holds its records and an exit can lose them.
    direct = [h for h in root.handlers if getattr(h, "only_enqueues", False)]
    destinations = [h for h in root.handlers if h not in direct] or [
        logging.StreamHandler(sys.stderr)
    ]
    json_console(destinations, mask_softmax_tokens)
    handler = DroppingQueueHandler()
    root.handlers = [handler, *direct]
    DropReportingListener(handler, *destinations).start()
    _logging_started = True


def tracer() -> trace.Tracer:
    global _provider
    if _provider is not None:
        return _provider.get_tracer("research_core")
    with _lock:
        if _provider is None:
            if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
                # The long-lived native caller records the complete subprocess tool call.
                # A one-shot command must not start exporters or flush them at exit.
                provider: TracerProvider | trace.NoOpTracerProvider = trace.NoOpTracerProvider()
            else:
                try:
                    configure_logging()
                    provider = TracerProvider(
                        shutdown_on_exit=False,
                        resource=OTELResourceDetector()
                        .detect()
                        .merge(
                            Resource(
                                {
                                    "service.name": os.environ.get(
                                        "OTEL_SERVICE_NAME", "ide-distributions"
                                    ),
                                    **{
                                        key: os.environ[env]
                                        for key, env in (
                                            ("workspace.id", "IDE_WORKSPACE_ID"),
                                            ("service.instance.id", "IDE_RUNTIME_ID"),
                                        )
                                        if env in os.environ
                                    },
                                }
                            )
                        ),
                    )
                    provider.add_span_processor(
                        BatchSpanProcessor(
                            ConsoleSpanExporter(
                                out=sys.stderr,
                                formatter=lambda span: span.to_json(indent=None) + "\n",
                            )
                        )
                    )
                except Exception:
                    # Configuration/SDK failure disables tracing, never application work.
                    # Do not report through a synchronous log handler on this path.
                    provider = trace.NoOpTracerProvider()
                if "OTEL_EXPORTER_OTLP_ENDPOINT" in os.environ or (
                    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in os.environ
                ):
                    try:
                        if isinstance(provider, TracerProvider):
                            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
                    except Exception:
                        # Keep console diagnostics when only the remote exporter is invalid.
                        pass
                trace.set_tracer_provider(provider)
            # Published only once configured, so a caller may read it without the lock.
            _provider = provider
        return _provider.get_tracer("research_core")


def correlation(span: Span) -> dict[str, str]:
    """The IDs a record carries to join its span, none for no span: ``trace_id`` (32 lowercase
    hex digits) and ``span_id`` (16), as Datadog links a log to its trace, and the trace again as
    ``otel_trace_id``, because Logs search never returns Datadog's two reserved names."""
    context = span.get_span_context()
    if not context.is_valid:
        return {}
    trace_id = f"{context.trace_id:032x}"
    return {"trace_id": trace_id, "span_id": f"{context.span_id:016x}", "otel_trace_id": trace_id}


def trace_carrier() -> dict[str, str]:
    """The current trace context as W3C ``traceparent`` and ``tracestate``, to store or send."""
    carrier: dict[str, str] = {}
    propagator.inject(carrier)
    return carrier


@contextmanager
def span(name: str, *, context: Context | None = None, **attributes) -> Iterator[Span]:
    """Record safe, explicitly selected attributes; never capture exception text or payloads."""
    with tracer().start_as_current_span(
        name,
        context=context,
        attributes={key: value for key, value in attributes.items() if value is not None},
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        completed = False
        try:
            yield current
            completed = True
        finally:
            if not completed:
                current.set_status(StatusCode.ERROR)


def process_env(env: Mapping[str, str]) -> dict[str, str]:
    return {**env, **{key.upper(): value for key, value in trace_carrier().items()}}


def process_context(env: Mapping[str, str]) -> Context:
    return propagator.extract(
        {key.lower(): value for key, value in env.items() if key in {"TRACEPARENT", "TRACESTATE"}}
    )


tool_log = logging.getLogger("research_core.tool_call")
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def is_error_type(value: object) -> bool:
    """Whether a ``kind`` read from a tool's result can be an ``error.type``: a short identifier
    such as ``transition_refused`` or an exception's class, never a sentence or a path."""
    return isinstance(value, str) and _ERROR_TYPE.fullmatch(value) is not None


def finish_tool_call(
    current: Span,
    *,
    boundary: str,
    role: str,
    tool_name: str,
    tool_call_id: str | None,
    session_id: str | None,
    turn_id: str | None,
    outcome: str,
    error_type: str | None,
    duration_s: float,
    action: str | None = None,
) -> None:
    """Name one ``execute_tool`` span's call and result, and log it: the same fields at both
    boundaries, ``native`` (Hermes's hooks) and ``distribution`` (``tools.base.run_tool``).

    A call failed exactly when it has an ``error_type``: a short, bounded class such as
    ``exit_code_nonzero`` or ``transition_refused``, never text from the call. The log line holds
    no arguments or results either, because those can hold file contents, paths and secrets.
    The caller ends the span.
    """
    attributes = {
        "gen_ai.operation.name": "execute_tool",
        "tool.boundary": boundary,
        "gen_ai.agent.name": role,
        "gen_ai.tool.name": tool_name,
        "gen_ai.tool.call.id": tool_call_id,
        "session.id": session_id,
        "turn.id": turn_id,
        "tool.action": action,
        "outcome": outcome,
        "error.type": error_type,
    }
    current.set_attribute("operation.success", 0 if error_type else 1)
    known = {key: value for key, value in attributes.items() if value}
    current.set_attributes(known)
    if error_type is not None:
        current.set_status(StatusCode.ERROR)
    tool_log.log(
        logging.INFO if error_type is None else logging.WARNING,
        "%s tool %s %s%s in %.2fs",
        boundary,
        tool_name,
        outcome,
        f" ({error_type})" if error_type not in (None, outcome) else "",
        duration_s,
        extra={"attributes": {**known, "duration_s": round(duration_s, 3)}},
    )
