"""Chat updates (the ``chat_update`` tool): what a background wake wants the user to hear.

A wake cannot write into a chat, and nobody reads its own Session. ``add`` records the update in
the workspace store, with a ``category`` saying why the user should hear it now; ``progress``,
and a category the ``chat_updates.categories`` setting leaves out, becomes a notice at once
instead. The coordinator
calls ``deliver`` on every pass, which hands a Coworld's pending updates to that Coworld's most
recently active chat as one queued ``followup`` admission.
The chat's next turn tells the user, after any run already live in that chat ends.

Delivery waits until the updates are quiet (the newest is ``DEBOUNCE`` old) or the oldest has
waited ``MAX_WAIT``, so a burst of wakes becomes one message. A chat gets no second update
admission while an earlier one is still queued. With no chat for the Coworld, the updates become
notices. The receipt on each row (``admission_id`` or ``notice_id``) commits in the transaction
that writes the admission or notice, and the admission's key is made of the rows it carries, so
a coordinator restart cannot deliver one twice. The caller checks ``paused``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from research_core.store import admissions, notices, settings
from research_core.store._common import json_field, parse_iso, parse_json, require
from research_core.store.actors import Actor
from research_core.store.db import Store, Tx
from research_core.store.errors import StoreError
from research_core.store.registry import CHAT_UPDATE_CATEGORIES, CHAT_UPDATE_PROGRESS

DEBOUNCE = timedelta(seconds=120)
MAX_WAIT = timedelta(minutes=10)
MAX_TEXT = 4000
MAX_REFS = 20
# One admission carries at most this many; the rest follow once it has left the queue.
MAX_BATCH = 20
KEY_PREFIX = "chat-updates:"
HEADER = (
    "Background updates arrived from your own background work. Tell the user about them in this "
    "chat, briefly. If the conversation is on another topic, give each update a sentence or two "
    "and go back to that topic; do not drop what you and the user were doing. Lead with anything "
    "the user must decide. Read the records before you report numbers."
)

_PENDING = (
    "SELECT * FROM chat_updates WHERE admission_id IS NULL AND notice_id IS NULL"
    " AND coworld_id IS ? ORDER BY id"
)
# The chat the user used last, by the rule AIDE's ``session.page`` offers: a top-level chat of
# the Coworld, by the later of its start and its latest user message, then start and id.
_TARGET_CHAT = (
    "SELECT s.id, s.profile FROM sessions s WHERE s.kind='chat' AND s.parent IS NULL"
    " AND s.coworld_id IS ? ORDER BY max(s.started, COALESCE((SELECT max(a.created_at)"
    " FROM client_admissions a WHERE a.session_id=s.id AND a.kind='message'"
    " AND a.origin IN ('client','answer')), '')) DESC, s.started DESC, s.id DESC LIMIT 1"
)


def add(
    store: Store,
    actor: Actor,
    *,
    category: str,
    text: str,
    refs: Sequence[str] = (),
    coworld_id: str | None = None,
) -> dict[str, Any]:
    """Record one update and its ``chat_update.recorded`` event: ``{"update": row}``.

    ``progress`` and a category the ``chat_updates.categories`` setting leaves out never reach
    the chat: they are written as a notice at once, ``{"notice": record}``.
    """
    _require_workspace(store)
    problems: list[str] = []
    categories = (*CHAT_UPDATE_CATEGORIES, CHAT_UPDATE_PROGRESS)
    require(problems, category in categories, f"category is one of {', '.join(categories)}")
    require(problems, isinstance(text, str) and text.strip(), "text is a non-empty string")
    require(
        problems,
        not isinstance(text, str) or len(text) <= MAX_TEXT,
        f"text is at most {MAX_TEXT} characters; say it shorter and give the records in refs",
    )
    require(
        problems,
        isinstance(refs, list | tuple) and all(isinstance(r, str) and r for r in refs),
        "refs is a list of record references",
    )
    require(
        problems,
        not isinstance(refs, list | tuple) or len(refs) <= MAX_REFS,
        f"refs names at most {MAX_REFS} records",
    )
    if problems:
        raise StoreError(problems)
    refs = list(refs)
    if category == CHAT_UPDATE_PROGRESS or category not in settings.get(
        store, "chat_updates.categories"
    ):
        notice = notices.add(store, actor, text=text, refs=refs, coworld_id=coworld_id)
        return {"notice": notice}
    with store.tx(actor) as tx:
        cursor = tx.execute(
            "INSERT INTO chat_updates(coworld_id, category, text, refs, created, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (coworld_id, category, text, json_field(refs), tx.at, str(actor)),
        )
        row = tx.one("SELECT * FROM chat_updates WHERE id = ?", (cursor.lastrowid,))
        tx.event(
            "chat_update.recorded",
            f"chat_update:{row['id']}",
            {"coworld_id": coworld_id, "category": category, "refs": refs},
        )
        return {"update": _record(row)}


def pending(store: Store, coworld_id: str | None) -> list[dict[str, Any]]:
    """The Coworld's updates that no chat or notice has carried yet, oldest first."""
    return [_record(row) for row in store.q(_PENDING, (coworld_id,))]


def deliver(store: Store, actor: Actor, *, coworld_id: str | None) -> dict[str, Any]:
    """Hand the Coworld's due updates to its chat, or to notices when it has none."""
    _require_workspace(store)
    # Decide on a plain read first: this runs on every coordinator pass, mostly with nothing to
    # write, and a write transaction would take the store's write lock each time.
    plan = _plan(store, coworld_id, parse_iso(store.now()))
    if plan is None or "waiting" in plan:
        return plan or {}
    with store.atomic(actor) as tx:
        plan = _plan(tx, coworld_id, parse_iso(tx.at))
        if plan is None or "waiting" in plan:
            return plan or {}
        batch, chat = plan["batch"], plan["chat"]
        ids = [row["id"] for row in batch]
        key = f"{KEY_PREFIX}{ids[0]}-{ids[-1]}"
        if chat is None:
            return {"noticed": _as_notices(store, actor, tx, batch, key, coworld_id)}
        op_id = admissions.enqueue_in_transaction(
            tx,
            key=key,
            session_id=chat["id"],
            profile=chat["profile"],
            origin="followup",
            text=render(batch),
        )
        tx.execute(
            "UPDATE chat_updates SET admission_id=? WHERE id IN (SELECT value FROM json_each(?))",
            (op_id, json_field(ids)),
        )
        tx.event(
            "chat_update.delivered",
            key,
            {
                "coworld_id": coworld_id,
                "via": "chat",
                "session_id": chat["id"],
                "admission_id": op_id,
                "updates": len(ids),
            },
        )
        return {"delivered": {"session": chat["id"], "admission": op_id, "updates": len(ids)}}


def render(rows: Sequence[Any]) -> str:
    """The admission text: the instruction, then each update with its time and records."""
    lines = [HEADER, ""]
    for number, row in enumerate(rows, start=1):
        refs = parse_json(row["refs"], default=[])
        about = f"; about {', '.join(refs)}" if refs else ""
        at = parse_iso(row["created"]).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"{number}. {row['category']} ({at}{about}): {row['text']}")
    return "\n".join(lines)


# --- helpers -----------------------------------------------------------------------------


def _plan(reader: Store | Tx, coworld_id: str | None, now: datetime) -> dict[str, Any] | None:
    """What to deliver now: ``None`` when nothing is due, ``waiting`` while the chat still has a
    queued update admission, else the ``batch`` and its ``chat`` (``None``: no chat)."""
    rows = reader.q(_PENDING, (coworld_id,))
    if not _due(rows, now):
        return None
    chat = reader.one(_TARGET_CHAT, (coworld_id,))
    if chat is not None and reader.one(
        "SELECT 1 FROM client_admissions WHERE session_id=? AND status='queued'"
        " AND substr(native_key, 1, ?)=?",
        (chat["id"], len(KEY_PREFIX), KEY_PREFIX),
    ):
        return {"waiting": {"session": chat["id"], "updates": len(rows)}}
    return {"batch": rows[:MAX_BATCH], "chat": chat}


def _due(rows: Sequence[Any], now: datetime) -> bool:
    """Quiet for ``DEBOUNCE``, or the oldest has waited ``MAX_WAIT``, or a batch is full."""
    if not rows:
        return False
    return (
        len(rows) >= MAX_BATCH
        or now - parse_iso(rows[-1]["created"]) >= DEBOUNCE
        or now - parse_iso(rows[0]["created"]) >= MAX_WAIT
    )


def _as_notices(
    store: Store, actor: Actor, tx: Tx, rows: Sequence[Any], key: str, coworld_id: str | None
) -> int:
    """No chat to tell: each update becomes a notice, in this transaction."""
    for row in rows:
        notice = notices.add(
            store,
            actor,
            text=row["text"],
            refs=parse_json(row["refs"], default=[]),
            coworld_id=coworld_id,
        )
        number = int(notice["id"].removeprefix("N-"))
        tx.execute("UPDATE chat_updates SET notice_id=? WHERE id=?", (number, row["id"]))
    tx.event(
        "chat_update.delivered",
        key,
        {"coworld_id": coworld_id, "via": "notice", "updates": len(rows)},
    )
    return len(rows)


def _require_workspace(store: Store) -> None:
    if store.kind != "workspace":
        raise StoreError(["chat updates live in the workspace store"])


def _record(row: Any) -> dict[str, Any]:
    return {**dict(row), "refs": parse_json(row["refs"], default=[])}
