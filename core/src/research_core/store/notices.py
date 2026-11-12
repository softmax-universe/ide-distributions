"""Notices (design A.8): the store side, in the workspace store only (A2.4).

A notice is workspace-level news for the user (a wake's ``progress`` ``chat_update``, what the
system's own writes report; what the user should hear now goes to their chat through
``chat_updates.py``), and unread notices open the next chat turn's context. ``add`` writes the
row and an ``events`` row of kind ``notice`` in one transaction, so attached clients see it at
once and the away summary finds it in the log. ``unread`` serves the chat turn: the newest
``limit`` unread notices in chronological order (a limit drops the oldest). ``mark_read`` records
who read them and when, once; it is bookkeeping and emits no event.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from research_core.store._common import display_id, json_field, require
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.store.errors import NotFound, StoreError
from research_core.store.registry import RECORDS, record_from_row

SPEC = RECORDS["notice"]
URGENCIES = ("low", "normal", "high")


def add(
    store: Store,
    actor: Actor,
    *,
    text: str,
    refs: Sequence[str] = (),
    urgency: str = "normal",
    coworld_id: str | None = None,
) -> dict[str, Any]:
    """Write a notice and its ``notice`` event."""
    _require_workspace(store)
    problems: list[str] = []
    require(problems, isinstance(text, str) and text.strip(), "text is a non-empty string")
    require(
        problems,
        isinstance(refs, list | tuple) and all(isinstance(r, str) and r for r in refs),
        "refs is a list of record references",
    )
    require(problems, urgency in URGENCIES, f"urgency is one of {', '.join(URGENCIES)}")
    if problems:
        raise StoreError(problems)
    refs = list(refs)
    if actor.is_session:
        from research_core.store import sessions

        session = sessions.read(store, actor.id)
        if session is not None:
            bound = session["coworld_id"]
            if coworld_id is not None and coworld_id != bound:
                raise StoreError(["notice Coworld differs from its session"])
            coworld_id = bound
    with store.tx(actor) as tx:
        cursor = tx.execute(
            "INSERT INTO notices(text, refs, urgency, created, created_by, coworld_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (text, json_field(refs), urgency, tx.at, str(actor), coworld_id),
        )
        record = _record(tx.one("SELECT * FROM notices WHERE id = ?", (cursor.lastrowid,)))
        tx.event(
            "notice",
            record["id"],
            {"text": text, "refs": refs, "urgency": urgency, "coworld_id": coworld_id},
        )
        return record


def unread(store: Store, limit: int = 20, *, coworld_id: str | None = None) -> list[dict[str, Any]]:
    """The newest ``limit`` unread notices, oldest first."""
    _require_workspace(store)
    if not (_is_int(limit) and limit >= 1):
        raise StoreError(["limit is a positive integer"])
    rows = store.q(
        "SELECT * FROM notices WHERE read_at IS NULL AND coworld_id IS ? ORDER BY id DESC LIMIT ?",
        (coworld_id, limit),
    )
    return [_record(row) for row in reversed(rows)]


def mark_read(store: Store, actor: Actor, ids: Sequence[int], by: str) -> list[dict[str, Any]]:
    """Mark notices read by ``by`` (a client or user); an already-read notice keeps its mark."""
    _require_workspace(store)
    problems: list[str] = []
    require(
        problems,
        isinstance(ids, list | tuple) and all(_is_int(i) for i in ids),
        "ids is a list of notice numbers",
    )
    require(problems, isinstance(by, str) and by, "by names who read them (a client or user)")
    if problems:
        raise StoreError(problems)
    if not ids:
        return []
    with store.tx(actor) as tx:
        missing = [i for i in ids if tx.one("SELECT 1 FROM notices WHERE id = ?", (i,)) is None]
        if missing:
            raise NotFound([f"{display_id('N', i)} does not exist" for i in missing])
        for notice_id in ids:
            tx.execute(
                "UPDATE notices SET read_at = ?, read_by = ? WHERE id = ? AND read_at IS NULL",
                (tx.at, by, notice_id),
            )
        return [_record(tx.one("SELECT * FROM notices WHERE id = ?", (i,))) for i in ids]


# --- helpers -----------------------------------------------------------------------------


def _require_workspace(store: Store) -> None:
    if store.kind != "workspace":
        raise StoreError(["notices live in the workspace store"])


def _record(row: Any) -> dict[str, Any]:
    return record_from_row(SPEC, row)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
