"""The capture API: what a callback or a command calls to record something.

Two writers share it. ``TelemetryWriter`` serves a long-lived process, whose callers must never
wait: a capture pickles what it was given, which makes it the writer's own copy, puts that on one
bounded in-memory queue and returns. One background thread unpickles, redacts, hashes and writes
lines to the container's stream, and so empties the queue by itself. Nothing waits for that thread
at exit, so an exit loses what the queue still holds. ``InlineWriter`` serves a short command: it
writes each capture's lines before it returns, so the command exits with nothing left to write.

A capture never raises into its caller. One that cannot be pickled or does not fit in the queue is
counted, and the thread logs the count each time it changes. Captures take plain data: strings,
numbers, lists and mappings. A caller holding a provider object turns it into that where it observes
it. Anything else is dropped, at the latest when it cannot be encoded.
"""

from __future__ import annotations

import logging
import pickle
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from research_core.agent_telemetry.encoder import (
    ContextPart,
    PromptFile,
    PromptSection,
    RecordEncoder,
)
from research_core.agent_telemetry.records import Producer
from research_core.agent_telemetry.redaction import Redactor
from research_core.agent_telemetry.stream import LogStream

logger = logging.getLogger(__name__)

MAX_QUEUED_OBSERVATIONS = 4096
MAX_QUEUED_BYTES = 64 * 1024 * 1024


def _charged_bytes(blob: bytes) -> int:
    """What a capture counts for against the byte limit. One over the limit, which goes out only
    when nothing is queued, counts for nothing: its reply comes microseconds later, often before
    the thread has taken it, and would find the queue full."""
    return 0 if len(blob) > MAX_QUEUED_BYTES else len(blob)


class Writer:
    """The capture API. With a ``redactor`` this is the gateway's writer and it takes text too.
    Without one, text captures are dropped and any other string goes through the Softmax token
    patterns alone."""

    def __init__(
        self,
        stream_path: str,
        producer: Producer,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        """Opens nothing here: Hermes gives a plugin's registration only a short time."""
        self._encoder = RecordEncoder(LogStream(stream_path), producer, redactor)

    def _capture(self, kind: str, occurred_at: float | None, **payload: Any) -> None:
        raise NotImplementedError

    def _write(self, kind: str, occurred_at: float, payload: Mapping[str, Any]) -> None:
        try:
            self._encoder.write(kind, occurred_at, payload)
        except Exception as error:
            logger.warning("telemetry: dropped a %s record (%s)", kind, type(error).__name__)

    def capture_operation(
        self,
        action: str,
        stage: str,
        *,
        occurred_at: float | None = None,
        content: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        """Record what happened. ``fields`` are the record's fields in the contract, and each event
        lists its own. ``details`` holds numbers and booleans only. ``content`` maps a reference
        name to text or a JSON value: each is stored as an object and named in ``content_refs``.
        """
        self._capture(
            "operation",
            occurred_at,
            action=action,
            stage=stage,
            content=content or {},
            fields=fields,
        )

    def capture_context_parts(
        self,
        target: str,
        parts: Sequence[ContextPart],
        *,
        occurred_at: float | None = None,
        **fields: Any,
    ) -> None:
        """Record where each part sits in a message's text, in bytes of the redacted text.

        A part that cannot be found is reported as ``unattributed``, with no place.
        """
        self._capture("context_parts", occurred_at, target=target, parts=parts, fields=fields)

    def capture_request(
        self,
        request: Mapping[str, Any],
        *,
        session_id: str,
        model_attempt_id: str,
        provider: str,
        prompt_sections: Sequence[PromptSection | PromptFile] = (),
        history_line: str = "main",
        occurred_at: float | None = None,
        **fields: Any,
    ) -> None:
        """Record one model request by reference: its keyword arguments, as the provider gets them.

        This also writes the ``model_attempt`` / ``request`` operation that names the manifest, and
        ``fields`` are that operation's. ``request["messages"]`` must be a list. A message's
        ``_row_id``, if present, becomes its ``hermes_message_ids`` entry. ``history_line``
        separates calls that do not share one history, such as auxiliary calls in a Session.
        """
        self._capture(
            "request",
            occurred_at,
            request=request,
            session_id=session_id,
            model_attempt_id=model_attempt_id,
            provider=provider,
            prompt_sections=prompt_sections,
            history_line=history_line,
            fields=fields,
        )

    def capture_response(
        self,
        message: Mapping[str, Any],
        *,
        session_id: str,
        model_attempt_id: str,
        partial: bool = False,
        occurred_at: float | None = None,
        **fields: Any,
    ) -> None:
        """Record one assistant message. ``partial``: a stream failed part way through.

        This also writes the ``model_attempt`` / ``response`` operation that names the message, and
        ``fields`` are that operation's, for example ``outcome``, ``usage`` and ``finish_reason``.
        """
        self.capture_operation(
            "model_attempt",
            "response",
            occurred_at=occurred_at,
            content={"message": message},
            session_id=session_id,
            model_attempt_id=model_attempt_id,
            partial=partial,
            **fields,
        )


class InlineWriter(Writer):
    """A short command's writer: each capture is written to the stream before it returns, as a
    process logs to stdout. The container runtime drains that stream into the pod's log file
    whether or not the Datadog Agent is reading it, so a write waits only if that runtime stops."""

    def __init__(self, stream_path: str, producer: Producer) -> None:
        super().__init__(stream_path, producer)
        self._lock = threading.Lock()

    def _capture(self, kind: str, occurred_at: float | None, **payload: Any) -> None:
        with self._lock:
            self._write(kind, time.time() if occurred_at is None else occurred_at, payload)


# A queue item: a capture's pickled payload, or a job to run on the thread.
_Item = tuple[str, float, bytes] | Callable[[], None]


class TelemetryWriter(Writer):
    """A long-lived process's writer: one bounded queue and one background thread."""

    def __init__(
        self,
        stream_path: str,
        producer: Producer,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        super().__init__(stream_path, producer, redactor=redactor)
        self._ready = threading.Condition()
        self._queue: deque[_Item] = deque()
        self._queued_bytes = 0
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, name="telemetry-writer", daemon=True)
        self._thread.start()

    def _capture(self, kind: str, occurred_at: float | None, **payload: Any) -> None:
        occurred = time.time() if occurred_at is None else occurred_at
        if threading.current_thread() is self._thread:
            # A deferred job's own captures go out in its place, before what was queued after it.
            self._write(kind, occurred, payload)
            return
        try:
            blob = pickle.dumps(payload, 5)
        except Exception:  # A capture must never break the agent's work.
            blob = None
        with self._ready:
            if (
                blob is None
                or len(self._queue) >= MAX_QUEUED_OBSERVATIONS
                or (self._queue and self._queued_bytes + len(blob) > MAX_QUEUED_BYTES)
            ):
                self._dropped += 1
            else:
                self._queue.append((kind, occurred, blob))
                self._queued_bytes += _charged_bytes(blob)
            self._ready.notify()

    def defer(self, job: Callable[[], None]) -> None:
        """Run ``job`` on the writer's thread, after what is captured so far: work a caller must
        not wait for, such as a read whose result is captured. It counts as a capture."""
        with self._ready:
            if len(self._queue) >= MAX_QUEUED_OBSERVATIONS:
                self._dropped += 1
            else:
                self._queue.append(job)
            self._ready.notify()

    def _run(self) -> None:
        reported = 0
        while True:
            with self._ready:
                while not (self._queue or self._dropped != reported):
                    self._ready.wait()
                dropped = self._dropped
                item = self._queue.popleft() if self._queue else None
                if isinstance(item, tuple):
                    self._queued_bytes -= _charged_bytes(item[2])
            if dropped != reported:
                reported = dropped
                logger.warning("telemetry: %d records dropped so far", dropped)
            if isinstance(item, tuple):
                kind, occurred_at, blob = item
                try:
                    payload = pickle.loads(blob)
                except Exception as error:  # Something can pickle and still fail to load.
                    logger.warning(
                        "telemetry: dropped a %s record (%s)", kind, type(error).__name__
                    )
                else:
                    self._write(kind, occurred_at, payload)
            elif item is not None:
                try:
                    item()
                except Exception as error:  # The writer's thread outlives any one job.
                    logger.warning("telemetry: a deferred job failed (%s)", type(error).__name__)
            # Let go of a request of many megabytes now, not after the next wait.
            item = blob = None
