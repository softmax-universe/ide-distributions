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

from research_core import refs
from research_core.store._common import json_field, parse_json
from research_core.store.errors import StoreError

if TYPE_CHECKING:
    from research_core.store.actors import Actor
    from research_core.store.db import Store

NAME = "identity.name"
CONFIRMED = "identity.name_confirmed"
MET = "identity.met_user"
OFFERED = "identity.offer_made"
# Placeholders a cogent might cite instead of a message; none is a reference to one.
_PLACEHOLDERS = frozenset({"setup", "wake", "none", "n/a", "na", "x", "user", "yes", "ok"})
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


def user_cites(cites: Any, what: str) -> str:
    """The user's message that ``what`` rests on. Refuses a blank, a placeholder ("setup") or
    cites that only name records (``C-5``), the same rule settings.update's tool applies."""
    text = cites.strip() if isinstance(cites, str) else ""
    if not text or text.lower() in _PLACEHOLDERS or refs.names_only_records(text):
        raise StoreError(
            [f"cites must be the user's own message that {what}, not a record or a placeholder"]
        )
    return text


def read(store: Store) -> dict[str, Any]:
    return {
        "name": _get(store, NAME),
        "name_confirmed": bool(_get(store, CONFIRMED)),
        "met_user": bool(_get(store, MET)),
        "offer_made": bool(_get(store, OFFERED)),
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
                [
                    f"the cogent is already named {parse_json(current['value'])!r}; "
                    "if the user asked to rename it, call again with name and cites (their message)"
                ]
            )
        _put(tx, actor, NAME, text, "setup")
    return read(store)


def confirm(store: Store, actor: Actor, *, cites: str, name: Any = None) -> dict[str, Any]:
    """The user's one allowed rewrite: confirm the name, or give a new one. Then it is locked."""
    _need_workspace(store)
    cites = user_cites(cites, "confirmed or renamed it")
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
    cites = user_cites(cites, "you met them in")
    if not _get(store, MET):
        with store.tx(actor) as tx:
            _put(tx, actor, MET, True, cites)
    return read(store)


def mark_offered(store: Store, actor: Actor) -> dict[str, Any]:
    """Record that the one rename offer was shown, so it is never made again (write-once)."""
    _need_workspace(store)
    if not _get(store, OFFERED):
        with store.tx(actor) as tx:
            _put(tx, actor, OFFERED, True, "offer")
    return read(store)
