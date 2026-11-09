"""What the gateway records about the agent's work, as telemetry lines. Capture is always on.

Each callback here only renders what Hermes hands it into the plain data the writer takes. Hermes
isolates plugin hooks, so a failure here reaches no agent. ``image/hermes-patches/README.md`` says
when each new hook fires.

One writer serves the whole gateway process, because a multiplexed gateway registers this plugin
once per profile. Each profile has its own ``GatewayCapture``, for its name and per-session state.

``on_model_attempt`` is the one source of request and response bodies. It holds the exact arguments
of each physical call, which request middleware does not: the stream options come later, and retries
run below it. So this registers no middleware, which also saves a copy of each request.

What Hermes runs for a tool (``post_tool_call``) is captured as metadata and arguments only. The
result comes from ``on_tool_results_final``, in the form the model got, not the original a spill
moved to a file that nobody read.

This writer is also the process's ``agent_telemetry.observed`` writer, so a store-only tool's
commits reach it with the run and tool call they happened in.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from research_core.agent_telemetry import (
    ContextPart,
    Producer,
    PromptSection,
    TelemetryWriter,
    hermes_redactor,
    observed,
    stream_path,
)
from research_core.agent_telemetry.encoder import message_text
from research_core.plugin.executor import hermes_call_ids
from research_core.plugin.hermes_prompt import drop_mandatory_tool_use
from research_core.plugin.repeats import refusal_of

GATEWAY_WRITER = "gateway"
# ``image/processes.py`` gives the gateway ten seconds to exit, and this runs once per profile.
EXIT_FLUSH_S = 2.0
# Auxiliary calls outside a turn belong to no Session, and a manifest must name one.
NO_SESSION = "no-session"
_NOT_A_NAME = re.compile(r"[^a-z0-9_.-]+")
_WIRE_ONLY_FIELDS = ("extra_body", "extra_headers", "extra_query")
_ROW_TEXT_FIELDS = ("role", "content", "tool_calls", "reasoning_content")
# What aiohttp logs each HTTP request under, as Hermes's ``web.AppRunner(app)`` leaves it.
ACCESS_LOGGER = "aiohttp.access"
# The whole of a successful poll by the seam's urllib client, in aiohttp's default format.
_SEAM_POLL = re.compile(
    r'\S+ \[[^\]]*\] "GET /p/[^/\s]+/v1/runs/[^/\s?]+ HTTP/[\d.]+" 200 \S+ "[^"]*" '
    r'"Python-urllib/[\d.]+"'
)

_writer_lock = threading.Lock()
_writer: TelemetryWriter | None = None


def _name(value: Any) -> str | None:
    """A value as the record format's lowercase name, or ``None`` when there is none."""
    if value is None or value == "":
        return None
    name = _NOT_A_NAME.sub("_", str(value).lower())[:63]
    return name if name[0].isalpha() else f"x{name}"


def _numbers(**values: Any) -> dict[str, Any] | None:
    """The ``details`` of a record, which hold only numbers and booleans."""
    kept = {name: value for name, value in values.items() if isinstance(value, int | float)}
    return kept or None


def _plain(value: Any) -> Any:
    """A provider object as plain data: ``SimpleNamespace`` from a stream, otherwise pydantic."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return str(value)


def _identity(message: Mapping[str, Any]) -> tuple[Any, ...]:
    """What tells a message apart: its role, and the tool calls it makes or answers."""
    calls = tuple(call.get("id") for call in message.get("tool_calls") or ())
    return message.get("role"), message.get("tool_call_id"), calls


def row_keys(conversation_history: Sequence[Mapping[str, Any]]) -> list[tuple[Any, Any]]:
    """The identity and native row ID of each message of ``pre_api_request``'s history."""
    return [(_identity(message), message.get("_row_id")) for message in conversation_history]


def with_row_ids(
    wire_messages: Sequence[Mapping[str, Any]], keys: Sequence[tuple[Any, Any]]
) -> list[Mapping[str, Any]]:
    """The messages a provider gets, each with ``_row_id`` added where its history row is known.

    Hermes and the refusal middleware can drop history rows on the way to the wire, and Hermes adds
    the system message, so a message is matched forward through the history by its identity, never
    by position alone.
    """
    cursor = 0
    bound = []
    for message in wire_messages:
        for index in range(cursor, len(keys)):
            identity, row_id = keys[index]
            if identity == _identity(message):
                cursor = index + 1
                if row_id is not None:
                    message = {**message, "_row_id": row_id}
                break
        bound.append(message)
    return bound


def wire_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON body the SDK makes of these keyword arguments: ``extra_body`` joins the top level.

    Headers and query parameters are not part of the body, so a rebuild would not have them.
    """
    wire = {name: value for name, value in request.items() if name not in _WIRE_ONLY_FIELDS}
    wire.update(request.get("extra_body") or {})
    return wire


def _assistant_message(response: Any) -> Any:
    """The reply as a JSON object. A 200 of plain text makes the SDK return a string."""
    choices = getattr(response, "choices", None)
    message = _plain(choices[0].message if choices else response)
    if message is None or isinstance(message, Mapping):
        return message
    return {"unparsed_reply": message}


def _message_from_parts(parts: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a stream delivered before it ended, as an assistant message, or ``None`` if nothing.

    Reasoning is ``reasoning_content`` here as on the message Hermes assembles from a whole stream.
    """
    message: dict[str, Any] = {
        wire_name: parts[name]
        for name, wire_name in (
            ("content", "content"),
            ("reasoning", "reasoning_content"),
            ("refusal", "refusal"),
            ("reasoning_details", "reasoning_details"),
        )
        if parts.get(name)
    }
    if parts.get("tool_calls"):
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in parts["tool_calls"]
        ]
    return {"role": "assistant", **message} if message else None


def _usage_fields(response: Any) -> dict[str, Any]:
    """``usage`` and ``cost`` from a provider's raw usage, in the record format's names."""
    raw = _plain(getattr(response, "usage", None))
    if not isinstance(raw, Mapping):
        return {}
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    usage = {
        "input_tokens": raw.get("prompt_tokens"),
        "output_tokens": raw.get("completion_tokens"),
        "cache_read_input_tokens": prompt_details.get("cached_tokens"),
        "cache_creation_input_tokens": prompt_details.get("cache_write_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
    }
    fields: dict[str, Any] = {
        "usage": {name: count for name, count in usage.items() if isinstance(count, int)} or None
    }
    if isinstance(raw.get("cost"), int | float):
        fields["cost"] = {"usd": raw["cost"], "source": "provider_billed"}
    return fields


def _milliseconds(start: Any, end: Any) -> float | None:
    if isinstance(start, int | float) and isinstance(end, int | float) and end >= start:
        return round((end - start) * 1000, 3)
    return None


class GatewayCapture:
    """The hooks of one profile, writing through the gateway's one writer."""

    def __init__(self, writer: TelemetryWriter, profile: str) -> None:
        self._writer = writer
        self._profile = profile
        # All by live Hermes Session. One dict operation at a time needs no lock here.
        self._row_keys: dict[str, list[Any]] = {}
        self._sections: dict[str, list[PromptSection]] = {}
        self._turn_parts: dict[str, list[ContextPart]] = {}
        # Compaction moves a Session to a new live ID; records keep naming the one it started with.
        self._first_session: dict[str, str] = {}

    def hooks(self) -> dict[str, Callable[..., None]]:
        return {
            "on_system_prompt_built": self.on_system_prompt_built,
            "pre_api_request": self.pre_api_request,
            "on_model_attempt": self.on_model_attempt,
            "api_request_error": self.api_request_error,
            "pre_auxiliary_call": self.pre_auxiliary_call,
            "post_auxiliary_call": self.post_auxiliary_call,
            "post_tool_call": self.post_tool_call,
            "on_tool_results_final": self.on_tool_results_final,
            "on_messages_committed": self.on_messages_committed,
            "pre_approval_request": self.pre_approval_request,
            "post_approval_response": self.post_approval_response,
            "post_compaction": self.post_compaction,
            "on_session_start": self.on_session_start,
            "on_session_end": self.on_session_end,
            "subagent_start": self.subagent_start,
            "subagent_stop": self.subagent_stop,
            "on_gateway_exit": self.on_gateway_exit,
        }

    def _ids(self, session_id: str | None, run_id: str | None, **fields: Any) -> dict[str, Any]:
        """The fields every record of a Session shares. ``None`` and empty ones are left out."""
        live = session_id or None
        known = {
            "profile": self._profile,
            "session_id": self._first_session.get(live, live) if live else None,
            "hermes_session_id": live,
            "run_id": run_id,
            **fields,
        }
        return {name: value for name, value in known.items() if value not in (None, "")}

    # Rendered context.

    def on_system_prompt_built(
        self,
        session_id: str = "",
        system_prompt: str = "",
        sections: Sequence[Mapping[str, Any]] = (),
        model: str | None = None,
        provider: str | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        # As sent: ``hermes_prompt``'s middleware drops a block from the request, not from this
        # prompt, and a section is found by its text in the request. Dropped text has no source.
        self._sections[session_id] = [
            PromptSection(
                section["source"],
                drop_mandatory_tool_use(system_prompt[section["start"] : section["end"]]),
            )
            for section in sections
        ]
        self._writer.capture_operation(
            "system_prompt",
            "built",
            model=model,
            provider=provider,
            size_bytes=len(system_prompt.encode("utf-8", "replace")),
            details=_numbers(sections=len(sections)),
            **self._ids(session_id, run_id),
        )

    def turn_context(
        self, session_id: str, user_message: Any, parts: Sequence[tuple[str, str]]
    ) -> None:
        """What ``plugin/turn.py`` adds to a user message, placed when the message is next sent."""
        typed = (
            [ContextPart("user_text", user_message)]
            if isinstance(user_message, str) and user_message
            else []
        )
        self._turn_parts[session_id] = [
            *typed,
            *(ContextPart(source, text) for source, text in parts),
        ]

    # Model requests and replies.

    def pre_api_request(
        self,
        session_id: str = "",
        conversation_history: Sequence[Mapping[str, Any]] = (),
        turn_id: str | None = None,
        task_id: str | None = None,
        api_request_id: str | None = None,
        api_call_count: int | None = None,
        retry_count: int | None = None,
        model: str | None = None,
        provider: str | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        """Keeps the native message IDs for the attempt, and says a call is on its way: a process
        killed during the call leaves this record and not the attempt's."""
        self._row_keys[session_id] = row_keys(conversation_history)
        self._writer.capture_operation(
            "model_request",
            "started",
            model=model,
            provider=provider,
            details=_numbers(
                api_call_count=api_call_count,
                retry_count=retry_count,
                history_messages=len(conversation_history),
            ),
            **self._ids(
                session_id,
                run_id,
                turn_id=turn_id,
                hermes_task_id=task_id,
                api_request_id=api_request_id,
            ),
        )

    def _capture_context_parts(
        self, session_id: str, messages: Sequence[Any], ids: Mapping[str, Any]
    ) -> None:
        parts = self._turn_parts.pop(session_id, None)
        if not parts:
            return
        for message in reversed(messages):
            if message.get("role") == "user":
                text = message_text(message)
                if text is not None:
                    self._writer.capture_context_parts(text, parts, **ids)
                return

    def on_model_attempt(
        self,
        session_id: str = "",
        request: Mapping[str, Any] | None = None,
        response: Any = None,
        stream_parts: Mapping[str, Any] | None = None,
        outcome: str = "",
        kind: str | None = None,
        attempt: int | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        first_chunk_at: float | None = None,
        error: str | None = None,
        error_type: str | None = None,
        status_code: int | None = None,
        provider: str | None = None,
        api_request_id: str | None = None,
        call_role: str | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        attempt_id = str(uuid.uuid4())
        wire = wire_request(request or {})
        wire["messages"] = with_row_ids(
            wire.get("messages", ()), self._row_keys.get(session_id, ())
        )
        ids = self._ids(
            session_id,
            run_id,
            api_request_id=api_request_id,
            delegated=True if call_role == "delegated" else None,
        )
        session = ids.get("session_id", NO_SESSION)
        identity = {key: value for key, value in ids.items() if key != "session_id"}
        self._capture_context_parts(
            session_id, wire["messages"], {**ids, "model_attempt_id": attempt_id}
        )
        self._writer.capture_request(
            wire,
            session_id=session,
            model_attempt_id=attempt_id,
            provider=provider or "unknown",
            prompt_sections=self._sections.get(session_id, ()),
            occurred_at=started_at,
            native_event_type=kind,
            **identity,
        )

        message = (
            _assistant_message(response)
            if outcome == "completed"
            else _message_from_parts(stream_parts or {})
        )
        result = {
            **ids,
            "model_attempt_id": attempt_id,
            "native_event_type": kind,
            "outcome": _name(outcome),
            "reason": _name(error_type),
            "duration_ms": _milliseconds(started_at, ended_at),
            "time_to_first_token_ms": _milliseconds(started_at, first_chunk_at),
            "details": _numbers(attempt=attempt, status_code=status_code),
        }
        if message is not None:
            fields = {
                k: v for k, v in result.items() if k not in ("session_id", "model_attempt_id")
            }
            choices = getattr(response, "choices", None)
            self._writer.capture_response(
                message,
                session_id=session,
                model_attempt_id=attempt_id,
                partial=outcome != "completed",
                occurred_at=ended_at,
                finish_reason=getattr(choices[0], "finish_reason", None) if choices else None,
                responded_model=getattr(response, "model", None),
                **_usage_fields(response),
                **fields,
            )
        if outcome == "failed":
            self._writer.capture_operation(
                "model_attempt",
                "failed",
                occurred_at=ended_at,
                content={"error": error} if error else None,
                **result,
            )
        if outcome == "completed":
            self._capture_proposals(stream_parts, message, {**ids, "model_attempt_id": attempt_id})

    def _capture_proposals(
        self, stream_parts: Mapping[str, Any] | None, message: Any, ids: Mapping[str, Any]
    ) -> None:
        """Each tool call the model asked for. A stream's arguments come before Hermes's repair."""
        calls = (stream_parts or {}).get("tool_calls")
        if not calls:
            listed = message.get("tool_calls") if isinstance(message, Mapping) else None
            calls = [
                {"id": call.get("id"), **(call.get("function") or {})} for call in listed or ()
            ]
        for call in calls:
            self._writer.capture_operation(
                "tool",
                "proposed",
                content={"arguments": call.get("arguments") or ""},
                tool_call_id=call.get("id"),
                tool_name=call.get("name"),
                **ids,
            )

    def api_request_error(
        self,
        session_id: str = "",
        reason: Any = None,
        status_code: int | None = None,
        api_request_id: str | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        """Hermes's own verdict on a failed call. Its text is on the attempt's failed record."""
        self._writer.capture_operation(
            "model_request",
            "error",
            reason=_name(reason),
            details=_numbers(status_code=status_code),
            **self._ids(session_id, run_id, api_request_id=api_request_id),
        )

    # Auxiliary calls: titles, compaction summaries and so on. One pair of hooks per attempt.

    def _auxiliary_ids(
        self, aux_task: str, started_at: Any, session_id: str, run_id: str | None
    ) -> tuple[dict[str, Any], str, str]:
        """The pair shares a start time and nothing else that identifies one attempt."""
        task = _name(aux_task) or "unknown"
        attempt_id = f"aux-{task}-{int((started_at or 0) * 1_000_000)}"
        ids = self._ids(session_id, run_id, native_event_type=task)
        return ids, attempt_id, ids.get("session_id", NO_SESSION)

    def pre_auxiliary_call(
        self,
        aux_task: str = "",
        session_id: str = "",
        request_body: Mapping[str, Any] | None = None,
        provider: str | None = None,
        started_at: float | None = None,
        api_request_id: str | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        ids, attempt_id, session = self._auxiliary_ids(aux_task, started_at, session_id, run_id)
        identity = {key: value for key, value in ids.items() if key != "session_id"}
        self._writer.capture_request(
            wire_request(request_body or {}),
            session_id=session,
            model_attempt_id=attempt_id,
            provider=provider or "unknown",
            history_line=f"auxiliary:{aux_task}",
            occurred_at=started_at,
            api_request_id=api_request_id or None,
            **identity,
        )

    def post_auxiliary_call(
        self,
        aux_task: str = "",
        session_id: str = "",
        raw_response: Any = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        error: str | None = None,
        error_type: str | None = None,
        finish_reason: str | None = None,
        response_model: str | None = None,
        streaming: bool = False,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        ids, attempt_id, session = self._auxiliary_ids(aux_task, started_at, session_id, run_id)
        identity = {key: value for key, value in ids.items() if key != "session_id"}
        duration_ms = _milliseconds(started_at, ended_at)
        if raw_response is not None:
            self._writer.capture_response(
                _assistant_message(raw_response),
                session_id=session,
                model_attempt_id=attempt_id,
                occurred_at=ended_at,
                outcome="completed",
                finish_reason=finish_reason,
                responded_model=response_model,
                duration_ms=duration_ms,
                **_usage_fields(raw_response),
                **identity,
            )
            return
        self._writer.capture_operation(
            "model_attempt",
            "failed" if error else "unread",
            occurred_at=ended_at,
            content={"error": error} if error else None,
            reason=_name(error_type),
            outcome=None if error else "streamed",
            model_attempt_id=attempt_id,
            duration_ms=duration_ms,
            **ids,
        )

    # Tools.

    def post_tool_call(
        self,
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        session_id: str = "",
        task_id: str | None = None,
        tool_call_id: str | None = None,
        turn_id: str | None = None,
        api_request_id: str | None = None,
        duration_ms: float | None = None,
        status: str | None = None,
        error_type: str | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        refused = status == "blocked" or (status == "error" and refusal_of(result) is not None)
        self._writer.capture_operation(
            "tool",
            "refused" if refused else "completed",
            content={"arguments": _plain(args)},
            tool_name=tool_name,
            outcome=_name(status),
            reason=_name(error_type),
            duration_ms=duration_ms,
            **self._ids(
                session_id,
                run_id,
                tool_call_id=tool_call_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                hermes_task_id=task_id,
            ),
        )

    def on_tool_results_final(
        self,
        session_id: str = "",
        task_id: str | None = None,
        results: Sequence[Mapping[str, Any]] = (),
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        for result in results:
            row_id = result.get("row_id")
            self._writer.capture_operation(
                "tool",
                "result",
                content={"result": result.get("content") or ""},
                tool_name=result.get("tool_name"),
                **self._ids(
                    session_id,
                    run_id,
                    tool_call_id=result.get("tool_call_id"),
                    hermes_task_id=task_id,
                    hermes_message_id=None if row_id is None else str(row_id),
                ),
            )

    def on_messages_committed(
        self,
        session_id: str = "",
        rows: Sequence[Mapping[str, Any]] = (),
        committed_at: float | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        """Metadata, and each row's own text. The last rows of a turn reach no later request,
        Hermes's tool guard writes its text with no model call at all, and a user's message is on no
        finished request if the process dies during that turn's first call. A tool row's text is on
        its ``tool`` / ``result`` record. This runs with the session's persistence lock held, and
        only enqueues."""
        for row in rows:
            row_id = row.get("_row_id")
            message = {name: row[name] for name in _ROW_TEXT_FIELDS if row.get(name)}
            self._writer.capture_operation(
                "message",
                "committed",
                occurred_at=committed_at,
                content={"message": message} if row.get("role") != "tool" else None,
                kind=row.get("role"),
                tool_name=row.get("tool_name"),
                finish_reason=row.get("finish_reason"),
                **self._ids(
                    session_id,
                    run_id,
                    tool_call_id=row.get("tool_call_id"),
                    hermes_message_id=None if row_id is None else str(row_id),
                ),
            )

    def pre_approval_request(
        self,
        command: str = "",
        description: str = "",
        surface: str | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        self._writer.capture_operation(
            "approval",
            "requested",
            content={"command": command, "description": description},
            native_event_type=_name(surface),
            **self._ids(None, run_id),
        )

    def post_approval_response(
        self,
        command: str = "",
        choice: str | None = None,
        decided_by: str | None = None,
        surface: str | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        self._writer.capture_operation(
            "approval",
            "decided",
            content={"command": command},
            outcome=_name(choice),
            reason=_name(decided_by),
            native_event_type=_name(surface),
            **self._ids(None, run_id),
        )

    # Lifecycle.

    def post_compaction(
        self,
        session_id: str = "",
        old_session_id: str = "",
        kind: str | None = None,
        reason: str | None = None,
        before_tokens: int | None = None,
        after_tokens: int | None = None,
        messages_removed: int | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        if old_session_id:
            self._first_session[session_id] = self._first_session.get(
                old_session_id, old_session_id
            )
            # Hermes rebuilds the prompt under the old ID and then moves to the new one.
            if old_session_id in self._sections:
                self._sections[session_id] = self._sections.pop(old_session_id)
        self._writer.capture_operation(
            "compaction",
            "committed",
            reason=_name(reason),
            details=_numbers(
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                messages_removed=messages_removed,
            ),
            **self._ids(session_id, run_id, kind=kind, parent_session_id=old_session_id),
        )

    def on_session_start(
        self, session_id: str = "", model: str | None = None, run_id: str | None = None, **_: Any
    ) -> None:
        self._writer.capture_operation(
            "session", "started", model=model, **self._ids(session_id, run_id)
        )

    def on_session_end(
        self,
        session_id: str = "",
        task_id: str | None = None,
        turn_id: str | None = None,
        turn_exit_reason: str | None = None,
        completed: bool | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        """In Hermes this ends a turn, not a Session."""
        self._row_keys.pop(session_id, None)
        self._turn_parts.pop(session_id, None)
        self._writer.capture_operation(
            "turn",
            "ended",
            reason=_name(turn_exit_reason),
            details={"completed": completed} if isinstance(completed, bool) else None,
            **self._ids(session_id, run_id, turn_id=turn_id, hermes_task_id=task_id),
        )

    def subagent_start(
        self,
        parent_session_id: str | None = None,
        child_session_id: str | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        self._writer.capture_operation(
            "subagent",
            "started",
            **self._ids(child_session_id, run_id, parent_session_id=parent_session_id),
        )

    def subagent_stop(
        self,
        parent_session_id: str | None = None,
        child_session_id: str | None = None,
        child_status: str | None = None,
        duration_ms: float | None = None,
        run_id: str | None = None,
        **_: Any,
    ) -> None:
        self._writer.capture_operation(
            "subagent",
            "stopped",
            outcome=_name(child_status),
            duration_ms=duration_ms,
            **self._ids(child_session_id, run_id, parent_session_id=parent_session_id),
        )

    def on_gateway_exit(self, exit_code: int | None = None, **_: Any) -> None:
        """Hermes calls this once per profile, with no profile scope, just before ``os._exit``."""
        self._writer.capture_operation(
            "gateway", "exit", details=_numbers(exit_code=exit_code), **self._ids(None, None)
        )
        self._writer.flush(EXIT_FLUSH_S)


class DiagnosticsHandler(logging.Handler):
    """Each log record of the gateway, through the writer and so through its one scanner.

    Hermes stamps the profile home and the session on every record, because a gateway serves several
    profiles. This only formats and enqueues, on the logging thread. Records from the writer itself
    stay out, because a failing writer would otherwise feed itself. So does the access line of a
    successful seam poll: it asks once a second per live run, and records what those polls find.
    """

    only_enqueues = True  # ``telemetry.configure_logging`` leaves this handler on the root logger.

    def __init__(self, writer: TelemetryWriter) -> None:
        super().__init__(logging.INFO)
        self._writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.threadName == "telemetry-writer"
            or record.name.startswith("research_core.agent_telemetry")
            or (record.name == ACCESS_LOGGER and _SEAM_POLL.fullmatch(record.getMessage()))
        ):
            return
        try:
            home = getattr(record, "hermes_home", "")
            session = getattr(record, "session_tag", "").strip(" []")
            self._writer.capture_operation(
                "diagnostic",
                _name(record.levelname) or "unknown",
                occurred_at=record.created,
                content={"message": self.format(record)},
                native_event_type=record.name,
                profile=Path(home).name if home else None,
                session_id=session or None,
                details=_numbers(line=record.lineno),
            )
        except Exception:  # The logging module's own way to report a handler's failure.
            self.handleError(record)


def _gateway_writer() -> TelemetryWriter:
    """The process's one writer. Only the first profile's call creates it."""
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = TelemetryWriter(
                stream_path(),
                Producer.for_process(GATEWAY_WRITER),
                redactor=hermes_redactor(),
            )
            observed.bind(_writer, hermes_call_ids)
            logging.getLogger().addHandler(DiagnosticsHandler(_writer))
        return _writer


def register_capture(ctx: Any) -> Callable[[str, Any, Sequence[tuple[str, str]]], None] | None:
    """Register every hook, and return where ``plugin/turn.py`` reports what it adds to a turn.

    Outside Hermes (tests with a stand-in context, the CLI) there is no scanner, so nothing is
    captured.
    """
    try:
        writer = _gateway_writer()
    except ImportError:
        return None
    capture = GatewayCapture(writer, ctx.profile_name)
    for hook_name, callback in capture.hooks().items():
        ctx.register_hook(hook_name, callback)
    return capture.turn_context
