"""The cogent's own identity: one name for the whole workspace, chosen once.

The name lives in the workspace store's ``settings`` table (``identity.name``) so the briefing
reads it in workspace sessions and in every Coworld. It is the cogent's own record, not a
directive (directives are the user's). Writes are write-once: the setup wake ``choose``s it
only while none exists, and the user may ``rename`` it once (confirming or changing it); after
that it is locked. Nothing else writes it, so retiring claims, deleting records or a later
Coworld's setup can never rename the cogent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from research_core.store._common import json_field, parse_json
from research_core.store.errors import StoreError

if TYPE_CHECKING:
    from research_core.store.actors import Actor
    from research_core.store.db import Store

NAME = "identity.name"
CONFIRMED = "identity.name_confirmed"
MET = "identity.met_user"
MAX_NAME = 40


def _need_workspace(store: Store) -> None:
    if store.kind != "workspace":
        raise StoreError([f"identity lives in the workspace store; this is the {store.kind} store"])


def _get(store: Store, key: str) -> Any:
    row = store.one("SELECT value FROM settings WHERE key = ?", (key,))
    return None if row is None else parse_json(row["value"])


def _put(tx: Any, actor: Actor, key: str, value: Any, cites: str) -> None:
    tx.execute(
        "INSERT INTO settings(key, value, updated, updated_by, cites) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated,"
        " updated_by = excluded.updated_by, cites = excluded.cites",
        (key, json_field(value), tx.at, str(actor), cites),
    )
    tx.event("settings.updated", key, {"key": key, "value": value, "cites": cites})


def read(store: Store) -> dict[str, Any]:
    return {
        "name": _get(store, NAME),
        "name_confirmed": bool(_get(store, CONFIRMED)),
        "met_user": bool(_get(store, MET)),
    }


def _clean(name: Any) -> str:
    text = name.strip() if isinstance(name, str) else ""
    if not text or len(text) > MAX_NAME or "\n" in text:
        raise StoreError([f"name must be one line of 1 to {MAX_NAME} characters"])
    return text


def choose(store: Store, actor: Actor, name: Any) -> dict[str, Any]:
    """Set the name when none exists; refused once one is set, whoever asks."""
    _need_workspace(store)
    text = _clean(name)
    with store.tx(actor) as tx:
        current = tx.one("SELECT value FROM settings WHERE key = ?", (NAME,))
        if current is not None and parse_json(current["value"]):
            raise StoreError(
                [f"the cogent is already named {parse_json(current['value'])!r}; keep it"]
            )
        _put(tx, actor, NAME, text, "setup")
    return read(store)


def confirm(store: Store, actor: Actor, *, cites: str, name: Any = None) -> dict[str, Any]:
    """The user's one allowed rewrite: confirm the name, or give a new one. Then it is locked."""
    _need_workspace(store)
    if not isinstance(cites, str) or not cites.strip():
        raise StoreError(["cites is required: the user's message that confirmed or renamed"])
    new = None if name is None else _clean(name)
    with store.tx(actor) as tx:
        current = tx.one("SELECT value FROM settings WHERE key = ?", (NAME,))
        existing = None if current is None else parse_json(current["value"])
        locked = tx.one("SELECT value FROM settings WHERE key = ?", (CONFIRMED,))
        if locked is not None and parse_json(locked["value"]):
            raise StoreError(["the name is already confirmed by the user and cannot change"])
        final = new or existing
        if not final:
            raise StoreError(["no name yet; give one"])
        if final != existing:
            _put(tx, actor, NAME, final, cites)
        _put(tx, actor, CONFIRMED, True, cites)
    return read(store)


def mark_met(store: Store, actor: Actor, *, cites: str) -> dict[str, Any]:
    """Record that the first get-to-know-you chat happened (idempotent)."""
    _need_workspace(store)
    if not isinstance(cites, str) or not cites.strip():
        raise StoreError(["cites is required: the user's message from that chat"])
    if not _get(store, MET):
        with store.tx(actor) as tx:
            _put(tx, actor, MET, True, cites)
    return read(store)
