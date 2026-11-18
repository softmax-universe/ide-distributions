"""Read-only wake accounting and prompt rendering. Coordinator owns all wake transitions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_core.store import events, settings
from research_core.store._common import parse_display_id, parse_json
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.store.events import Event
from research_core.watcher.context import TickContext
from research_core.watcher.handlers import MODEL_KINDS
from research_core.watcher.store_checks import labelled_stores

TEMPLATES_DIR = Path(__file__).parent / "wakes"
PLANNED_STOPS_PREFIX = "wake_planned_stops:"
PAYLOAD_CHARS = 4000


def has_queued_wakes(ctx: TickContext) -> bool:
    """Whether any model-wake event is still unacknowledged and not given up on."""
    max_attempts = int(settings.get(ctx.wstore, "recovery.max_attempts"))
    for label, store in labelled_stores(ctx):
        for event in events.unhandled(store, MODEL_KINDS):
            row = store.one("SELECT * FROM handled_events WHERE seq=?", (event.seq,))
            if (
                row is None
                or counted_attempts(ctx, label, event.seq, row["attempts"]) <= max_attempts
            ):
                return True
    return False


def counted_attempts(ctx: TickContext, label: str, seq: int, attempts: int) -> int:
    """The attempts that count toward ``recovery.max_attempts``: stops for sleep do not."""
    return attempts - int(ctx.meta(f"{PLANNED_STOPS_PREFIX}{label}:{seq}") or 0)


def question_id(event: Event) -> int | None:
    return event.payload.get("message_id") if event.kind == "task.asked" else None


def question_for_session(store: Store, session_id: str) -> Event | None:
    row = store.one(
        "SELECT e.* FROM handled_events h JOIN events e ON e.seq=h.seq"
        " WHERE h.session_id=? AND e.kind='task.asked' ORDER BY e.seq LIMIT 1",
        (session_id,),
    )
    return Event.from_row(row) if row is not None else None


def question_settled(store: Store, wstore: Store, event: Event, session_id: str | None) -> bool:
    """A question is current only while its exact Task wait remains open."""
    ident = question_id(event)
    if ident is None:
        return False
    row = store.one(
        "SELECT status,waiting_on FROM tasks WHERE id=?",
        (_subject_ids(event.subject)["task_id"],),
    )
    if row is None or row["status"] != "in-progress":
        return True
    waiting = parse_json(row["waiting_on"])
    if not waiting or waiting["kind"] != "question" or waiting["ref"] != ident:
        return True
    return (
        session_id is not None
        and wstore.one(
            "SELECT 1 FROM events WHERE kind='chat_update.recorded' AND actor=?"
            " AND json_extract(payload,'$.question_id')=? LIMIT 1",
            (str(Actor.session(session_id)), ident),
        )
        is not None
    )


def render_wake(kind: str, event: Event, ctx: TickContext) -> str:
    """The wake prompt: ``wakes/<kind>.md`` with the event filled in."""
    return _fill(kind, event.kind, event.subject, _payload_text(event.payload), ctx)


def render_batch(kind: str, batch: list[Event], ctx: TickContext) -> str:
    """The wake prompt for a batch of events of one kind: the template rendered once, with every
    subject in ``{subject}`` and a list of ``{"subject", "payload"}`` entries, each cut at
    :data:`PAYLOAD_CHARS`, in ``{payload_json}``."""
    subjects = ", ".join(event.subject for event in batch)
    entries = [_payload_text({"subject": e.subject, "payload": e.payload}) for e in batch]
    payload = "[\n" + ",\n".join(entries) + "\n]"
    return _fill(kind, batch[0].kind, subjects, payload, ctx)


def _payload_text(payload: Any) -> str:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if len(text) > PAYLOAD_CHARS:
        text = text[:PAYLOAD_CHARS] + "\n… (payload cut here; the event has the rest)"
    return text


def _fill(kind: str, event_kind: str, subject: str, payload: str, ctx: TickContext) -> str:
    template = (TEMPLATES_DIR / f"{kind}.md").read_text(encoding="utf-8")
    coworld_id = ctx.coworld.coworld_id if ctx.coworld is not None else "none active"
    replacements = (
        ("{event_kind}", event_kind),
        ("{subject}", subject),
        ("{payload_json}", payload),
        ("{coworld_id}", coworld_id),
    )
    text = template
    for placeholder, value in replacements:
        text = text.replace(placeholder, value)
    return text


def _subject_ids(subject: str) -> dict[str, int]:
    """``task_id`` or ``goal_id`` for the Session row when the subject is such a record."""
    try:
        kind, number = parse_display_id(subject)
    except ValueError:
        return {}
    if kind == "task":
        return {"task_id": number}
    if kind == "goal":
        return {"goal_id": number}
    return {}


def _is_record_ref(subject: str) -> bool:
    try:
        parse_display_id(subject)
    except ValueError:
        return False
    return True
