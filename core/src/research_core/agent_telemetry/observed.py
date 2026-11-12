"""What is recorded outside the gateway's hooks: admissions, committed state, coordinator work.

Each process binds its one writer here: ``research serve``, the gateway and every other ``research``
command each have their own. Code in the process then calls
``record``, which does nothing when no writer is bound. Everything here is metadata: no payload is
copied whole, because ``client.delivery_failed`` and ``task.failed`` hold exception and free text.

``observe_commit`` sees a transaction's events after its COMMIT, so a rollback records nothing.
``_ALLOWED`` lists what each event type may send from its payload. Beyond that a record holds the
event's type, its ``seq`` and actor, a subject that is a record's display ID (``T-3``), and the row
behind the event when the event is about an admission, a durable command or a Session. These rows
are read after the commit, so a record says what they hold at that moment.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from research_core.agent_telemetry.records import Producer
from research_core.agent_telemetry.stream import stream_path
from research_core.agent_telemetry.writer import InlineWriter, Writer
from research_core.store._common import parse_iso

if TYPE_CHECKING:
    from research_core.store.actors import Actor
    from research_core.store.db import Store

logger = logging.getLogger(__name__)

COMMAND_WRITER = "research-command"
_DISPLAY_ID = re.compile(r"^([A-Z])-(\d+)$")
_DETAIL_NAME = re.compile(r"[^a-z0-9_.-]")


@dataclass(frozen=True)
class _Allowed:
    """What one event type sends from its payload."""

    subject: str | None = None  # The record field its subject fills, when that is an ID.
    fields: Mapping[str, str] = field(default_factory=dict)  # Record field -> payload key.
    details: tuple[str, ...] = ()  # Payload keys holding numbers or booleans.
    times: tuple[str, ...] = ()  # Payload keys holding a time, sent as ``<key>_s``, epoch seconds.


_ALLOWED: dict[str, _Allowed] = {
    "client.started": _Allowed(fields={"run_id": "run_id"}, details=("steered",)),
    "client.waiting": _Allowed(
        fields={"run_id": "run_id", "reason": "error_class"}, times=("resume_after",)
    ),
    "client.resumed": _Allowed(fields={"reason": "error_class"}, details=("resume",)),
    "client.finished": _Allowed(
        fields={"outcome": "status", "reason": "error_class"},
        details=("local_runtime_stopped", "replayed"),
    ),
    "client.delivery_failed": _Allowed(details=("retryable", "replayed")),
    "client.open_failed": _Allowed(details=("retryable",)),
    "transition.retry_waiting": _Allowed(times=("retry_at",)),
    "task.assigned": _Allowed(
        fields={
            "assignment_id": "op_id",
            "admission_id": "op_id",
            "session_id": "session_id",
            "profile": "profile",
        }
    ),
    "task.release_requested": _Allowed(fields={"assignment_id": "op_id"}),
    "task.claimed": _Allowed(
        fields={"session_id": "session", "run_id": "run_id", "lease_epoch": "epoch"}
    ),
    "task.attempt_transferred": _Allowed(
        fields={"session_id": "successor", "parent_session_id": "previous", "lease_epoch": "epoch"}
    ),
    "task.completed": _Allowed(fields={"session_id": "session", "outcome": "outcome"}),
    "task.failed": _Allowed(fields={"session_id": "session"}),
    "task.released": _Allowed(fields={"session_id": "session"}),
    "task.proposed_submission": _Allowed(fields={"session_id": "session"}),
    "task.abandoned": _Allowed(fields={"session_id": "previous_holder", "run_id": "run_id"}),
    "session.ended": _Allowed(fields={"run_id": "run_id", "initiator": "stopped_by"}),
    "turn.rendered": _Allowed(
        subject="session_id",
        fields={"coworld_id": "coworld_id"},
        details=("wseq", "cseq", "first"),
    ),
    "ui.patch": _Allowed(subject="conversation_id", details=("version",)),
    "notice": _Allowed(fields={"coworld_id": "coworld_id", "reason": "urgency"}),
    "chat_update.recorded": _Allowed(fields={"coworld_id": "coworld_id", "reason": "category"}),
    "chat_update.delivered": _Allowed(
        fields={
            "coworld_id": "coworld_id",
            "session_id": "session_id",
            "admission_id": "admission_id",
            "reason": "via",
        },
        details=("updates",),
    ),
    "recovery.failed": _Allowed(
        fields={
            "coworld_id": "coworld_id",
            "task_id": "task",
            "session_id": "session",
            "reason": "stage",
        }
    ),
    "wake.failed": _Allowed(
        fields={"coworld_id": "coworld_id", "reason": "wake"}, details=("seq", "attempts")
    ),
    "watcher.heartbeat": _Allowed(details=("has_work",)),
    "platform.request": _Allowed(fields={"outcome": "outcome"}, details=("status",)),
    "coworld.registered": _Allowed(subject="coworld_id"),
    "coworld.switched": _Allowed(subject="coworld_id"),
    "coworld.setup_needed": _Allowed(subject="coworld_id"),
    "coworld.set_up": _Allowed(subject="coworld_id"),
    "champion.changed": _Allowed(subject="league_id"),
    "profile.model_updated": _Allowed(fields={"profile": "profile", "model": "model"}),
}


@dataclass(frozen=True)
class _Binding:
    writer: Writer
    # The run and tool call the caller is inside, where the process can tell.
    call_ids: Callable[[], Mapping[str, str | None]]


_binding: _Binding | None = None


def bind(
    writer: Writer,
    call_ids: Callable[[], Mapping[str, str | None]] = dict,
) -> None:
    """Make ``writer`` this process's writer, from now until ``unbind``."""
    from research_core.store import db

    global _binding
    _binding = _Binding(writer, call_ids)
    db.observe_commits(observe_commit)


def unbind(writer: Writer) -> None:
    """Stop using ``writer``. Does nothing if another writer has been bound since."""
    from research_core.store import db

    global _binding
    if _binding is not None and _binding.writer is writer:
        _binding = None
        db.observe_commits(None)


def bind_command(
    env: Mapping[str, str], trace_ids: Callable[[], Mapping[str, str]]
) -> InlineWriter:
    """For a short command, which writes each record as it happens and so exits with none left.
    ``trace_ids`` names the span the command is in when it records, and the tool call it serves
    comes from its environment."""
    writer = InlineWriter(stream_path(env), Producer.for_process(COMMAND_WRITER, env))
    bind(writer, lambda: {**trace_ids(), "tool_call_id": env.get("IDE_TOOL_CALL_ID")})
    return writer


def record(action: str, stage: str, **fields: Any) -> None:
    """One operation record. ``fields`` are metadata, as the contract lists them. No free text."""
    binding = _binding
    if binding is not None:
        binding.writer.capture_operation(action, stage, **{**binding.call_ids(), **fields})


def report_details(report: Mapping[str, Any]) -> dict[str, bool | int | float]:
    """A coordinator report as numbers: a list becomes its length, and text only says it was there.

    Reports mix counts with reasons and error text, which is why no string is copied.
    """
    details: dict[str, bool | int | float] = {}

    def walk(name: str, value: Any) -> None:
        if isinstance(value, Mapping):
            for key, entry in value.items():
                walk(f"{name}.{key}" if name else str(key), entry)
        elif value is not None:
            if isinstance(value, list | tuple):
                value = len(value)
            elif not isinstance(value, bool | int | float):
                value = True
            details[_DETAIL_NAME.sub("_", name.lower())] = value

    walk("", report)
    return details


def observe_commit(store: Store, actor: Actor, events: list[tuple[int, str, str, dict]]) -> None:
    """``Tx`` calls this after its COMMIT has succeeded. It must never fail the committed work."""
    binding = _binding
    if binding is None:
        return
    try:
        for seq, kind, subject, payload in events:
            fields = _event_fields(store, actor, seq, kind, subject, payload)
            ids = {**binding.call_ids(), **fields}
            binding.writer.capture_operation("store_event", "committed", **ids)
    except Exception as error:
        logger.warning("telemetry: dropped a commit's records (%s)", type(error).__name__)


def _event_fields(
    store: Store, actor: Actor, seq: int, kind: str, subject: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    allowed = _ALLOWED.get(kind, _Allowed())
    fields: dict[str, Any] = {
        "native_event_type": kind,
        "reported_actor": str(actor),
        "session_id": actor.id if actor.is_session else None,
        "lease_epoch": actor.epoch,
    }
    details: dict[str, Any] = {"store_seq": seq}
    display = _DISPLAY_ID.match(subject)
    if display is not None:
        # Experiments, versions and the rest have no record field: their type says what it counts.
        if display[1] == "T":
            fields["task_id"] = subject
        elif display[1] == "G":
            fields["goal_id"] = subject
        else:
            details["record_number"] = int(display[2])
    if allowed.subject is not None:
        fields[allowed.subject] = subject
    reader = _ROW_READERS.get(kind.split(".")[0])
    if reader is not None:
        row_fields = reader(store, subject)
        details.update(row_fields.pop("details", {}))
        fields.update({name: value for name, value in row_fields.items() if value is not None})
    for name, key in allowed.fields.items():
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and name != "lease_epoch":
            value = str(value)
        if value:
            fields[name] = value
    for key in allowed.details:
        if isinstance(payload.get(key), bool | int | float):
            details[key] = payload[key]
    for key in allowed.times:
        if payload.get(key):
            details[f"{key}_s"] = parse_iso(payload[key]).timestamp()
    fields["details"] = details
    return fields


def _admission_fields(store: Store, op_id: str) -> dict[str, Any]:
    row = store.one(
        "SELECT session_id,profile,origin,owner_instance,resumes FROM client_admissions"
        " WHERE op_id=?",
        (op_id,),
    )
    fields: dict[str, Any] = {"admission_id": op_id}
    if row is not None:
        fields.update(
            session_id=row["session_id"],
            profile=row["profile"],
            initiator=row["origin"],
            owner_instance=row["owner_instance"],
            details={"resumes": row["resumes"]},
        )
    return fields


def _command_fields(store: Store, command_id: str) -> dict[str, Any]:
    row = store.one(
        "SELECT tool,actor,actor_epoch,owner_instance,issuer_attempt_id,attempts"
        " FROM transition_commands WHERE id=?",
        (command_id,),
    )
    fields: dict[str, Any] = {"command_id": command_id}
    if row is not None:
        actor = row["actor"]
        fields.update(
            tool_name=row["tool"],
            session_id=actor.removeprefix("session:") if actor.startswith("session:") else None,
            lease_epoch=row["actor_epoch"],
            owner_instance=row["owner_instance"],
            execution_attempt_id=row["issuer_attempt_id"],
            details={"attempts": row["attempts"]},
        )
    return fields


def _session_fields(store: Store, session_id: str) -> dict[str, Any]:
    row = store.one(
        "SELECT profile,kind,coworld_id,task_id,goal_id,event,parent,session_context"
        " FROM sessions WHERE id=?",
        (session_id,),
    )
    fields: dict[str, Any] = {"session_id": session_id}
    if row is not None:
        context = json.loads(row["session_context"] or "{}")
        fields.update(
            profile=row["profile"],
            kind=row["kind"],
            coworld_id=row["coworld_id"],
            session_scope="coworld" if row["coworld_id"] else "workspace",
            league_id=context.get("league"),
            task_id=None if row["task_id"] is None else f"T-{row['task_id']}",
            goal_id=None if row["goal_id"] is None else f"G-{row['goal_id']}",
            initiator=row["event"],
            parent_session_id=row["parent"],
        )
    return fields


_ROW_READERS: dict[str, Callable[[Any, str], dict[str, Any]]] = {
    "client": _admission_fields,
    "execution": _admission_fields,
    "transition": _command_fields,
    "session": _session_fields,
}
