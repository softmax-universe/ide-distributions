"""The Event record and the watcher's handled-events bookkeeping.

``events`` is append-only (a trigger refuses UPDATE and DELETE); which events the watcher has
handled lives in ``handled_events``, keyed by the event's ``seq``. The watcher commits each
event before it starts the event's run and marks it handled only after the run ends (design
B.13), so a crash leaves the event unhandled and ``attempts`` says how often it was tried.
These bookkeeping writes go through ``Store.tx`` (so they are token-marked) but write no
``events`` row of their own: an event about handling an event would only feed itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from research_core.store._common import parse_json
from research_core.store.errors import NotFound

if TYPE_CHECKING:
    from research_core.store.actors import Actor
    from research_core.store.db import Store, Tx


# Every event kind any store function or the watcher emits (amendment A2.9). A test asserts
# the kinds the modules emit are listed here, and the watcher's ownership map covers them.
KINDS: frozenset[str] = frozenset(
    {
        "transition.requested",
        "transition.applied",
        "transition.refused",
        "transition.retry_waiting",
        "execution.queued",
        "execution.admitted",
        "task.assigned",
        "task.release_requested",
        "goal.created",
        "goal.updated",
        "goal.status",
        "task.created",
        "task.updated",
        "task.proposed",
        "task.approved",
        "task.claimed",
        "task.released",
        "task.waiting",
        "task.wait_resolved",
        "task.asked",
        "task.answered",
        "task.message",
        "task.completed",
        "task.failed",
        "task.reopened",
        "task.reviewed",
        "task.abandoned",
        "task.merged",
        "submission.candidate",
        "task.proposed_submission",
        "task.stuck",
        "claim.created",
        "claim.updated",
        "claim.retired",
        "experiment.created",
        "experiment.updated",
        "experiment.started",
        "experiment.run",
        "experiment.closed",
        "experiment.abandoned",
        "version.created",
        "version.uploaded",
        "version.submitted",
        "champion.changed",
        "directive.added",
        "directive.updated",
        "directive.retired",
        "settings.updated",
        "profile.model_updated",
        "capability.failed",
        "capability.repaired",
        "repair.failed_twice",
        "intent.pending",
        "intent.done",
        "intent.failed",
        "session.started",
        "session.ended",
        "session.dead",
        "notice",
        "chat_update.recorded",
        "chat_update.delivered",
        "coworld.registered",
        "coworld.switched",
        "coworld.setup_needed",
        "coworld.set_up",
        "store.unmarked_write",
        "store.schema_rebuilt",
        "store.schema_newer",
        "platform.request",
        "platform.forum_post",
        "platform.wiki_edit",
        "turn.rendered",
        "watcher.heartbeat",
        "merge.failed",
        "task.failure_review",
        "goal.needs_ideation",
        "recovery.failed",
        "wake.failed",
        "checks.ran",
        "drain.done",
        "goal.park",
        "workspace.resumed",
        "workspace.sleep",
        "anti_stall",
    }
)


@dataclass(frozen=True)
class Event:
    seq: int
    at: str
    actor: str
    kind: str
    subject: str
    payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Event:
        return cls(
            seq=row["seq"],
            at=row["at"],
            actor=row["actor"],
            kind=row["kind"],
            subject=row["subject"],
            payload=parse_json(row["payload"], default={}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def unhandled(store: Store, kinds: Iterable[str], limit: int = 500) -> list[Event]:
    """Events of ``kinds`` with no ``handled_events`` row or one whose ``handled_at`` is NULL."""
    kinds = list(kinds)
    if not kinds:
        return []
    placeholders = ", ".join("?" for _ in kinds)
    rows = store.q(
        "SELECT e.* FROM events e LEFT JOIN handled_events h ON h.seq = e.seq"
        f" WHERE e.kind IN ({placeholders}) AND h.handled_at IS NULL"
        " ORDER BY e.seq LIMIT ?",
        (*kinds, limit),
    )
    return [Event.from_row(row) for row in rows]


def claim_handling(store: Store, actor: Actor, seq: int, kind: str, session_id: str) -> int:
    """Record one more attempt to handle event ``seq``; returns the attempt count."""
    with store.tx(actor) as tx:
        _require_event(tx, seq)
        tx.execute(
            "INSERT INTO handled_events(seq, kind, session_id, attempts) VALUES (?, ?, ?, 1)"
            " ON CONFLICT(seq) DO UPDATE SET"
            " attempts = attempts + 1, session_id = excluded.session_id, kind = excluded.kind",
            (seq, kind, session_id),
        )
        return tx.one("SELECT attempts FROM handled_events WHERE seq = ?", (seq,))[0]


def mark_handled(store: Store, actor: Actor, seq: int, run_id: str | None) -> None:
    """Event ``seq`` is done: set ``handled_at`` (and the run that did it), clear the error."""
    with store.tx(actor) as tx:
        kind = _require_event(tx, seq)
        tx.execute(
            "INSERT INTO handled_events(seq, kind, run_id, handled_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(seq) DO UPDATE SET"
            " handled_at = excluded.handled_at, run_id = excluded.run_id, last_error = NULL",
            (seq, kind, run_id, tx.at),
        )


def mark_failed(store: Store, actor: Actor, seq: int, error: str) -> None:
    """Handling event ``seq`` failed; it stays unhandled with the error recorded."""
    with store.tx(actor) as tx:
        kind = _require_event(tx, seq)
        tx.execute(
            "INSERT INTO handled_events(seq, kind, last_error) VALUES (?, ?, ?)"
            " ON CONFLICT(seq) DO UPDATE SET last_error = excluded.last_error",
            (seq, kind, error),
        )


def _require_event(tx: Tx, seq: int) -> str:
    row = tx.one("SELECT kind FROM events WHERE seq = ?", (seq,))
    if row is None:
        raise NotFound([f"event {seq} does not exist"])
    return row[0]
