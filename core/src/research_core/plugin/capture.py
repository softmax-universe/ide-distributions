"""What the gateway records about the agent's work, as telemetry lines, from Hermes's own hooks.

Hermes is not patched. Each callback renders what a native hook or middleware hands it into the
plain data the writer takes. Hermes isolates plugin hooks and middleware, so a failure here reaches
no agent; Hermes logs it.

- A model request: the ``llm_request`` middleware sees the whole request, after every other plugin
  middleware, and keeps it; ``pre_api_request`` (fired next, on the same thread) records it with the
  native row IDs of its history. Hermes's own ``request`` field on the hooks is capped, so it is not
  used. One record per outer attempt: a stream that Hermes retries inside one attempt is one record.
- A reply: ``post_api_request`` hands over the assistant message object itself. A failure is
  ``api_request_error``; what a broken stream had shown is ``on_stream_end``'s ``final_text``.
- A tool result: as the model got it, from the next request that carries it. A tool row's own
  text is on its commit too, for a turn that ends before a request carries it (a guard halt).
- Committed rows: read from ``state.db`` at the end of each turn (``on_session_end``).
- The run: ``plugin/runs.py``, from the Session's started admission.
  An auxiliary call keeps the run its Session's turn was in when the call began.

Telemetry never waits on the agent's path: a hook only enqueues. The writer's thread is a daemon,
and Hermes ends the gateway with ``os._exit``, so what it still has queued at exit is lost unless
flushed first. Only while the gateway stops does a turn's end (``on_session_end``) or a Session's
finalization (``on_session_finalize``) wait, up to ``FLUSH_S``, for the writer: a graceful stop
interrupts each running turn and the turn's end keeps its records. A hard exit loses what the
writer still had queued. One writer serves the whole gateway process,
because a multiplexed gateway registers this plugin once per profile. Each profile has its own
``GatewayCapture``, for its name and per-session state.

This writer is also the process's ``agent_telemetry.observed`` writer, so a store-only tool's
commits reach it with the run and tool call they happened in.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
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
from research_core.plugin.repeats import refusal_of
from research_core.plugin.runs import parents, run_of

GATEWAY_WRITER = "gateway"
# How long a turn's end or a Session's finalization waits for the writer while the gateway stops.
# Hermes abandons an ``on_session_end`` callback after 30 seconds and a finalization after 10.
FLUSH_S = 5.0
# Auxiliary calls outside a turn belong to no Session, and a manifest must name one.
NO_SESSION = "no-session"
_NOT_A_NAME = re.compile(r"[^a-z0-9_.-]+")
# SDK keyword arguments that are not part of the JSON body.
_WIRE_ONLY_FIELDS = ("extra_body", "extra_headers", "extra_query", "timeout", "http_client")
_ROW_TEXT_FIELDS = ("role", "content", "tool_call_id", "tool_calls", "reasoning_content")
# What aiohttp logs each HTTP request under, as Hermes's ``web.AppRunner(app)`` leaves it.
ACCESS_LOGGER = "aiohttp.access"
# The whole of a successful poll by the seam's urllib client, in aiohttp's default format.
_SEAM_POLL = re.compile(
    r'\S+ \[[^\]]*\] "GET /p/[^/\s]+/v1/runs/[^/\s?]+ HTTP/[\d.]+" 200 \S+ "[^"]*" '
    r'"Python-urllib/[\d.]+"'
)

_writer_lock = threading.Lock()
_writer: TelemetryWriter | None = None
# What each plugin prompt section last rendered, by Session and section name: ``remember_section``.
_plugin_sections: dict[str, dict[str, str]] = defaultdict(dict)


def remember_section(name: str, render: Callable[..., str]) -> Callable[..., str]:
    """``render`` for ``register_system_prompt_section``, keeping what it gave each Session, so
    that its stretch of the system prompt can be attributed to it."""

    def section(info: Mapping[str, Any], **kwargs: Any) -> str:
        text = render(info, **kwargs)
        _plugin_sections[info["session_id"]][name] = text
        return text

    return section


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

    Headers and query parameters are not part of the body, so a rebuild would not have them. Hermes
    adds ``stream`` and ``stream_options`` after the middleware, so they are not here either.
    """
    wire = {name: value for name, value in request.items() if name not in _WIRE_ONLY_FIELDS}
    wire.update(request.get("extra_body") or {})
    return wire


def to_native_message(message: Any) -> dict[str, Any] | None:
    """Hermes's assistant message object as the record format's native message."""
    if message is None:
        return None
    reasoning = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None)
    plain = {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "reasoning_content": reasoning,
        "tool_calls": [
            _wire_tool_call(call) for call in _plain(getattr(message, "tool_calls", None)) or ()
        ]
        or None,
    }
    return {name: value for name, value in plain.items() if value is not None}


def _wire_tool_call(call: Mapping[str, Any]) -> dict[str, Any]:
    """A tool call in the provider's shape. Hermes's assembled message keeps its own flat one."""
    if "function" in call:
        return dict(call)
    function = {"name": call.get("name"), "arguments": call.get("arguments") or ""}
    return {"id": call.get("id"), "type": "function", "function": function}


def usage_fields(summary: Any) -> dict[str, Any]:
    """``usage`` from ``post_api_request``'s summary, in the record format's names. Hermes keeps the
    provider's raw usage out of it, so the billed cost is not here."""
    if not isinstance(summary, Mapping):
        return {}
    usage = {
        "input_tokens": summary.get("prompt_tokens"),
        "output_tokens": summary.get("output_tokens"),
        "cache_read_input_tokens": summary.get("cache_read_tokens"),
        "cache_creation_input_tokens": summary.get("cache_write_tokens"),
        "reasoning_tokens": summary.get("reasoning_tokens"),
    }
    return {"usage": {name: n for name, n in usage.items() if isinstance(n, int)} or None}


def prompt_sections(system: str, sources: Sequence[tuple[str, str]]) -> list[PromptSection]:
    """Each source whose text is in the system prompt as sent. Others are left out."""
    return [PromptSection(name, text.strip()) for name, text in sources if text.strip() in system]


def _milliseconds(start: Any, end: Any) -> float | None:
    if isinstance(start, int | float) and isinstance(end, int | float) and end >= start:
        return round((end - start) * 1000, 3)
    return None


class GatewayCapture:
    """The hooks of one profile, writing through the gateway's one writer."""

    def __init__(self, writer: TelemetryWriter, profile: str, stopping: Callable[[], bool]) -> None:
        """``stopping`` says, without waiting, whether the gateway is shutting down."""
        self._writer = writer
        self._profile = profile
        self._stopping = stopping
        self._started = time.time()
        # All by live Hermes Session. One dict operation at a time needs no lock here.
        self._requests: dict[str, Mapping[str, Any]] = {}
        self._attempts: dict[str, int] = defaultdict(int)
        self._attempt: dict[str, str] = {}
        # Each call's attempt in flight, by ``api_request_id``, until Hermes reports it failed.
        # Hermes also reports a call whose request could not be built, before it has an attempt.
        self._open_attempt: dict[str, str] = {}
        # The calls of each Session's current turn, whose state its end clears.
        self._turn_calls: dict[str, list[str]] = defaultdict(list)
        self._results_seen: dict[str, set[str]] = defaultdict(set)
        self._turn_parts: dict[str, list[ContextPart]] = {}
        self._reasoning: dict[str, list[str]] = defaultdict(list)
        self._committed: dict[str, int] = {}
        # The run a Session's current turn belongs to, as its requests found it: the admission can
        # leave ``started`` before ``on_session_end`` reads the turn's committed rows.
        self._turn_run: dict[str, str | None] = {}
        # The run each Session's latest turn belongs to, bound when the turn starts: the title call
        # Hermes starts then can end after the run does. And the run each auxiliary call is in.
        self._spawn_run: dict[str, str | None] = {}
        self._call_run: dict[str, str | None] = {}
        # Compaction moves a Session to a new live ID; records keep naming the one it started with.
        self._first_session: dict[str, str] = {}

    def hooks(self) -> dict[str, Callable[..., None]]:
        return {
            "pre_llm_call": self.pre_llm_call,
            "pre_api_request": self.pre_api_request,
            "post_api_request": self.post_api_request,
            "api_request_error": self.api_request_error,
            "on_stream_start": self.on_stream_start,
            "on_stream_delta": self.on_stream_delta,
            "on_stream_end": self.on_stream_end,
            "pre_auxiliary_call": self.pre_auxiliary_call,
            "post_auxiliary_call": self.post_auxiliary_call,
            "post_tool_call": self.post_tool_call,
            "pre_approval_request": self.pre_approval_request,
            "post_approval_response": self.post_approval_response,
            "post_compaction": self.post_compaction,
            "on_session_start": self.on_session_start,
            "on_session_end": self.on_session_end,
            "on_session_finalize": self.on_session_finalize,
            "subagent_start": self.subagent_start,
            "subagent_stop": self.subagent_stop,
        }

    def _ids(self, session_id: str | None, **fields: Any) -> dict[str, Any]:
        """The fields every record of a Session shares. ``None`` and empty ones are left out."""
        live = session_id or None
        known = {
            "profile": self._profile,
            "session_id": self._first_session.get(live, live) if live else None,
            "hermes_session_id": live,
            "run_id": run_of(live),
            **fields,
        }
        return {name: value for name, value in known.items() if value not in (None, "")}

    # Rendered context.

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

    def _sections(self, session_id: str, messages: Sequence[Mapping[str, Any]]) -> list:
        """SOUL and this plugin's sections, which are text the distribution ships. The rest of the
        prompt (Hermes's guidance, the project's rules, the skill index) is in no section."""
        if not messages or messages[0].get("role") != "system":
            return []
        from hermes_constants import get_hermes_home

        soul = Path(get_hermes_home()) / "SOUL.md"
        sources = [
            ("soul", soul.read_text(encoding="utf-8") if soul.exists() else ""),
            *(("plugin_sections", text) for text in _plugin_sections[session_id].values()),
        ]
        return prompt_sections(message_text(messages[0]) or "", sources)

    # Model requests and replies.

    def keep_request(self, request: Mapping[str, Any], session_id: str = "", **_: Any) -> None:
        """``llm_request`` middleware that changes nothing: the whole request, for the hook."""
        self._requests[session_id] = request

    def pre_llm_call(self, session_id: str = "", **_: Any) -> None:
        """A turn starts, before Hermes starts the calls (title, compression) that belong to it."""
        self._spawn_run[session_id] = run_of(session_id or None)

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
        started_at: float | None = None,
        **_: Any,
    ) -> None:
        """One attempt is on its way: the request as sent, with its history's native row IDs."""
        ids = self._ids(
            session_id, turn_id=turn_id, hermes_task_id=task_id, api_request_id=api_request_id
        )
        self._turn_run[session_id] = ids.get("run_id")
        self._spawn_run[session_id] = ids.get("run_id")
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
            **ids,
        )
        request = self._requests.pop(session_id, None)
        if request is None:
            return
        call = api_request_id or str(uuid.uuid4())
        self._attempts[call] += 1
        self._turn_calls[session_id].append(call)
        attempt_id = f"{call}#{self._attempts[call]}"
        self._attempt[session_id] = attempt_id
        self._open_attempt[call] = attempt_id
        wire = wire_request(request)
        wire["messages"] = with_row_ids(wire.get("messages", ()), row_keys(conversation_history))
        identity = {key: value for key, value in ids.items() if key != "session_id"}
        self._capture_context_parts(
            session_id, wire["messages"], {**ids, "model_attempt_id": attempt_id}
        )
        self._capture_results(session_id, wire["messages"], ids)
        self._writer.capture_request(
            wire,
            session_id=ids.get("session_id", NO_SESSION),
            model_attempt_id=attempt_id,
            provider=provider or "unknown",
            prompt_sections=self._sections(session_id, wire["messages"]),
            occurred_at=started_at,
            native_event_type="request",
            details=_numbers(attempt=self._attempts[call]),
            **identity,
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

    def _capture_results(
        self, session_id: str, messages: Sequence[Mapping[str, Any]], ids: Mapping[str, Any]
    ) -> None:
        """Each tool result of the current turn the first time a request carries it: the form the
        model got, after Hermes's transforms, spill, turn budget and ``/steer`` text. Results of
        earlier turns, which every request sends again, are not this turn's, even after a
        restart."""
        seen = self._results_seen[session_id]
        users = [index for index, message in enumerate(messages) if message.get("role") == "user"]
        for message in messages[users[-1] + 1 if users else 0 :]:
            call = message.get("tool_call_id")
            if message.get("role") != "tool" or not call or call in seen:
                continue
            seen.add(call)
            row_id = message.get("_row_id")
            self._writer.capture_operation(
                "tool",
                "result",
                content={"result": message_text(message) or ""},
                tool_name=message.get("name"),
                tool_call_id=call,
                hermes_message_id=None if row_id is None else str(row_id),
                **{key: value for key, value in ids.items() if key != "api_request_id"},
            )

    def post_api_request(
        self,
        session_id: str = "",
        assistant_message: Any = None,
        finish_reason: str | None = None,
        response_model: str | None = None,
        usage: Any = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        first_chunk_at: float | None = None,
        api_request_id: str | None = None,
        **_: Any,
    ) -> None:
        attempt_id = self._attempt.get(session_id)
        self._reasoning.pop(session_id, None)
        message = to_native_message(assistant_message)
        if attempt_id is None or message is None:
            return
        ids = self._ids(session_id, api_request_id=api_request_id)
        fields = {key: value for key, value in ids.items() if key != "session_id"}
        self._writer.capture_response(
            message,
            session_id=ids.get("session_id", NO_SESSION),
            model_attempt_id=attempt_id,
            occurred_at=ended_at,
            outcome="completed",
            finish_reason=finish_reason,
            responded_model=response_model,
            duration_ms=_milliseconds(started_at, ended_at),
            time_to_first_token_ms=_milliseconds(started_at, first_chunk_at),
            native_event_type="request",
            **usage_fields(usage),
            **fields,
        )
        for call in message.get("tool_calls") or ():
            function = call.get("function") or {}
            self._writer.capture_operation(
                "tool",
                "proposed",
                content={"arguments": function.get("arguments") or ""},
                tool_call_id=call.get("id"),
                tool_name=function.get("name"),
                model_attempt_id=attempt_id,
                **ids,
            )

    def api_request_error(
        self,
        session_id: str = "",
        reason: Any = None,
        status_code: int | None = None,
        api_request_id: str | None = None,
        error: Mapping[str, Any] | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        **_: Any,
    ) -> None:
        """Hermes's verdict on a failed attempt, with its error text. A call that failed before its
        request was recorded, such as one whose request could not be built, has no attempt: its
        record has no ``model_attempt_id``."""
        ids = self._ids(session_id, api_request_id=api_request_id)
        error = error or {}
        self._writer.capture_operation(
            "model_attempt",
            "failed",
            occurred_at=ended_at,
            content={"error": error.get("message")} if error.get("message") else None,
            model_attempt_id=self._open_attempt.pop(api_request_id or "", None),
            reason=_name(reason) or _name(error.get("type")),
            duration_ms=_milliseconds(started_at, ended_at),
            details=_numbers(status_code=status_code),
            **ids,
        )

    def on_stream_start(self, session_id: str = "", **_: Any) -> None:
        self._reasoning.pop(session_id, None)

    def on_stream_delta(
        self, session_id: str = "", delta: str = "", kind: str = "text", **_: Any
    ) -> None:
        """Reasoning as it streams, kept for a stream that breaks. It reaches plugins only with
        ``plugins.stream_reasoning_deltas``. Text comes whole at the stream's end."""
        if kind == "reasoning":
            self._reasoning[session_id].append(delta)

    def on_stream_end(
        self,
        session_id: str = "",
        final_text: str = "",
        finished: bool = True,
        error: str | None = None,
        **_: Any,
    ) -> None:
        """A stream that broke: what it had shown, as a partial reply of the current attempt.
        Partial tool-call arguments are not handed to plugins."""
        reasoning = "".join(self._reasoning.pop(session_id, ()))
        attempt_id = self._attempt.get(session_id)
        if (finished and error is None) or attempt_id is None:
            return
        message = {
            name: value
            for name, value in (("content", final_text), ("reasoning_content", reasoning))
            if value
        }
        if not message:
            return
        ids = self._ids(session_id)
        self._writer.capture_response(
            {"role": "assistant", **message},
            session_id=ids.get("session_id", NO_SESSION),
            model_attempt_id=attempt_id,
            partial=True,
            outcome="failed",
            **{key: value for key, value in ids.items() if key != "session_id"},
        )

    # Auxiliary calls: titles, compaction summaries and so on. One pair of hooks per attempt.
    # Hermes hands plugins these bodies capped (``HERMES_PLUGIN_PAYLOAD_MAX_CHARS``, 50,000).

    def _auxiliary_ids(
        self, aux_task: str, started_at: Any, session_id: str, *, starting: bool
    ) -> tuple[dict[str, Any], str, str]:
        """The pair shares a start time and nothing else that identifies one attempt. A call is in
        the run its Session's turn was in when the call began, even if it ends after that run."""
        task = _name(aux_task) or "unknown"
        attempt_id = f"aux-{task}-{int((started_at or 0) * 1_000_000)}"
        if starting:
            self._call_run[attempt_id] = self._spawn_run.get(session_id)
            run_id = self._call_run[attempt_id]
        else:
            run_id = self._call_run.pop(attempt_id, None)
        ids = self._ids(session_id, native_event_type=task, run_id=run_id)
        return ids, attempt_id, ids.get("session_id", NO_SESSION)

    def pre_auxiliary_call(
        self,
        aux_task: str = "",
        session_id: str = "",
        request: Mapping[str, Any] | None = None,
        provider: str | None = None,
        started_at: float | None = None,
        api_request_id: str | None = None,
        **_: Any,
    ) -> None:
        ids, attempt_id, session = self._auxiliary_ids(
            aux_task, started_at, session_id, starting=True
        )
        identity = {key: value for key, value in ids.items() if key != "session_id"}
        self._writer.capture_request(
            wire_request((request or {}).get("body") or {}),
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
        response: Mapping[str, Any] | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        error: str | None = None,
        error_type: str | None = None,
        finish_reason: str | None = None,
        response_model: str | None = None,
        usage: Any = None,
        **_: Any,
    ) -> None:
        ids, attempt_id, session = self._auxiliary_ids(
            aux_task, started_at, session_id, starting=False
        )
        identity = {key: value for key, value in ids.items() if key != "session_id"}
        duration_ms = _milliseconds(started_at, ended_at)
        message = (response or {}).get("assistant_message") if not error else None
        if message:
            self._writer.capture_response(
                {key: value for key, value in message.items() if value is not None},
                session_id=session,
                model_attempt_id=attempt_id,
                occurred_at=ended_at,
                outcome="completed",
                finish_reason=finish_reason,
                responded_model=response_model,
                duration_ms=duration_ms,
                **usage_fields(usage),
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
                tool_call_id=tool_call_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                hermes_task_id=task_id,
            ),
        )

    def pre_approval_request(
        self,
        command: str = "",
        description: str = "",
        surface: str | None = None,
        session_id: str = "",
        **_: Any,
    ) -> None:
        self._writer.capture_operation(
            "approval",
            "requested",
            content={"command": command, "description": description},
            native_event_type=_name(surface),
            **self._ids(session_id),
        )

    def post_approval_response(
        self,
        command: str = "",
        choice: str | None = None,
        decided_by: str | None = None,
        surface: str | None = None,
        session_id: str = "",
        **_: Any,
    ) -> None:
        self._writer.capture_operation(
            "approval",
            "decided",
            content={"command": command},
            outcome=_name(choice),
            reason=_name(decided_by),
            native_event_type=_name(surface),
            **self._ids(session_id),
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
        **_: Any,
    ) -> None:
        if old_session_id:
            self._first_session[session_id] = self._first_session.get(
                old_session_id, old_session_id
            )
            if old_session_id in _plugin_sections:
                _plugin_sections[session_id] = _plugin_sections.pop(old_session_id)
            self._turn_calls[session_id] += self._turn_calls.pop(old_session_id, [])
        self._writer.capture_operation(
            "compaction",
            "committed",
            reason=_name(reason),
            details=_numbers(
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                messages_removed=messages_removed,
            ),
            **self._ids(session_id, kind=kind, parent_session_id=old_session_id),
        )

    def on_session_start(self, session_id: str = "", model: str | None = None, **_: Any) -> None:
        self._writer.capture_operation("session", "started", model=model, **self._ids(session_id))

    def on_session_end(
        self,
        session_id: str = "",
        task_id: str | None = None,
        turn_id: str | None = None,
        turn_exit_reason: str | None = None,
        completed: bool | None = None,
        **_: Any,
    ) -> None:
        """In Hermes this ends a turn, not a Session."""
        self._turn_parts.pop(session_id, None)
        for call in self._turn_calls.pop(session_id, ()):
            self._attempts.pop(call, None)
            self._open_attempt.pop(call, None)
        self._capture_commits(session_id, self._turn_run.pop(session_id, None))
        self._writer.capture_operation(
            "turn",
            "ended",
            reason=_name(turn_exit_reason),
            details={"completed": completed} if isinstance(completed, bool) else None,
            **self._ids(session_id, turn_id=turn_id, hermes_task_id=task_id),
        )
        if self._stopping():
            self._writer.flush(FLUSH_S)

    def on_session_finalize(self, **_: Any) -> None:
        """A Session ends. Hermes also finalizes one on ``/new``, which must not wait."""
        if self._stopping():
            self._writer.flush(FLUSH_S)

    def _capture_commits(self, session_id: str, run_id: str | None) -> None:
        """Each row of the Session that Hermes committed since the last turn's end, or since this
        process started. ``occurred_at`` is the row's own timestamp. The copy Hermes keeps of a row
        it prunes (``compacted``) and its ``session_meta`` notes are no messages of the Session."""
        from hermes_constants import get_hermes_home

        db = Path(get_hermes_home()) / "state.db"
        if not session_id or not db.exists():
            return
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, role, content, tool_call_id, tool_calls, tool_name, timestamp, "
                "finish_reason, reasoning_content FROM messages "
                "WHERE session_id = ? AND id > ? AND timestamp >= ? AND NOT compacted "
                "AND role != 'session_meta' ORDER BY id",
                (session_id, self._committed.get(session_id, 0), self._started),
            ).fetchall()
        ids = self._ids(session_id)
        if run_id is not None:
            ids["run_id"] = run_id
        for row in rows:
            self._committed[session_id] = row["id"]
            message = {name: row[name] for name in _ROW_TEXT_FIELDS if row[name]}
            if "tool_calls" in message:
                message["tool_calls"] = json.loads(message["tool_calls"])
            self._writer.capture_operation(
                "message",
                "committed",
                occurred_at=row["timestamp"],
                content={"message": message},
                kind=row["role"],
                tool_name=row["tool_name"],
                finish_reason=row["finish_reason"],
                tool_call_id=row["tool_call_id"],
                hermes_message_id=str(row["id"]),
                **ids,
            )

    def subagent_start(
        self,
        parent_session_id: str | None = None,
        child_session_id: str | None = None,
        **_: Any,
    ) -> None:
        if parent_session_id and child_session_id:
            parents[child_session_id] = parent_session_id
        self._writer.capture_operation(
            "subagent",
            "started",
            **self._ids(child_session_id, parent_session_id=parent_session_id),
        )

    def subagent_stop(
        self,
        parent_session_id: str | None = None,
        child_session_id: str | None = None,
        child_status: str | None = None,
        duration_ms: float | None = None,
        **_: Any,
    ) -> None:
        self._writer.capture_operation(
            "subagent",
            "stopped",
            outcome=_name(child_status),
            duration_ms=duration_ms,
            **self._ids(child_session_id, parent_session_id=parent_session_id),
        )
        parents.pop(child_session_id or "", None)


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
            redactor = hermes_redactor()
            _writer = TelemetryWriter(
                stream_path(),
                Producer.for_process(GATEWAY_WRITER),
                redactor=redactor,
            )
            observed.bind(_writer, hermes_call_ids)
            # Hermes's stderr handler writes JSON lines from here on, through the same scanner.
            # Imported here: this module's import needs the standard library alone.
            from research_core import telemetry

            telemetry.json_console(logging.getLogger().handlers, redactor.text)
            logging.getLogger().addHandler(DiagnosticsHandler(_writer))
            # Hermes's stderr handler writes on the logging thread, which can be the agent's: from
            # here on it sits behind a queue that drops instead of waiting for a stalled pipe.
            telemetry.configure_logging()
            # Hermes logs a failed tool's output with its `Tool X returned error` warning, and the
            # output can hold file contents and tracebacks. ``telemetry.finish_tool_call`` logs
            # each failure without it. A logger filter drops the record before any handler.
            logging.getLogger("agent.tool_executor").addFilter(
                lambda record: not str(record.msg).startswith("Tool %s returned error")
            )
        return _writer


def gateway_stopping() -> bool:
    """Whether Hermes's gateway has begun to stop: ``GatewayRunner.stop`` clears ``_running`` before
    it drains and interrupts running turns. Outside a gateway there is no runner."""
    from gateway.run import _gateway_runner_ref

    runner = _gateway_runner_ref()
    return runner is not None and not runner._running


def register_capture(ctx: Any) -> Callable[[str, Any, Sequence[tuple[str, str]]], None] | None:
    """Register every hook and the request middleware, and return where ``plugin/turn.py``
    reports what it adds to a turn.

    Outside Hermes (tests with a stand-in context, the CLI) there is no scanner, so nothing is
    captured.
    """
    try:
        writer = _gateway_writer()
    except ImportError:
        return None
    capture = GatewayCapture(writer, ctx.profile_name, gateway_stopping)
    for hook_name, callback in capture.hooks().items():
        ctx.register_hook(hook_name, callback)
    # Last of the plugin's llm_request middleware (``register.py`` calls this after the others), so
    # it sees the request as sent.
    ctx.register_middleware("llm_request", capture.keep_request)
    return capture.turn_context
