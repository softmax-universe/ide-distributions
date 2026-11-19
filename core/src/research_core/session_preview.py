"""Requests and fallback titles embedded in AIDE session listings."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from research_core.store.db import Store

EMBEDDED_ACTIVE = 1000
RECENT_TERMINAL = 10


def request_ids(store: Store, session_ids: list[str]) -> list[str]:
    """All active requests, the most recently updated terminal ones, and each fallback title."""
    ids = set()
    terminal_by_session: dict[str, list[tuple[str, str]]] = {}
    titles = set()
    for row in store.conn.execute(
        "SELECT op_id,session_id,status,origin,"
        " (SELECT at FROM events WHERE subject=op_id ORDER BY seq DESC LIMIT 1) AS updated"
        " FROM client_admissions WHERE kind='message'"
        " AND session_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,rowid",
        (json.dumps(session_ids),),
    ):
        op_id, session = row["op_id"], row["session_id"]
        if row["status"] in ("queued", "running"):
            ids.add(op_id)
        else:
            terminal_by_session.setdefault(session, []).append((row["updated"], op_id))
        if session not in titles and row["origin"] == "client":
            message = store.one(
                "SELECT json_extract(request,'$.message') FROM client_admissions WHERE op_id=?",
                (op_id,),
            )[0]
            if message is not None and fallback_title(json.loads(message)["content"]):
                ids.add(op_id)
                titles.add(session)
    # Match the stable updated-at ordering in Session.requests, including ties.
    for terminal in terminal_by_session.values():
        terminal.sort(key=lambda entry: entry[0])
        ids.update(op_id for _, op_id in terminal[-RECENT_TERMINAL:])
    return list(ids)


def fallback_title(content: str | list[dict]) -> str:
    text = (
        content
        if isinstance(content, str)
        else " ".join(part["text"] for part in content if part["type"] == "text")
    )
    return " ".join(text.split())[:80]


def latest_session_id(store: Store) -> str | None:
    """Rank chat activity like Play, hydrating no transcripts or tool results.

    A session's newest message bounds its newest embedded user request. Visit the largest
    bounds first. Once a remaining chat cannot win, its admission history need never be read.
    Store times are UTC ISO strings. Play compares activity at JS millisecond precision and
    breaks ties by the full session start time, then the ordinary listing order.
    """
    best = None
    winner = None
    for row in store.conn.execute(
        """
        SELECT id, started, rowid AS ordinal, substr(max(started, coalesce((
            SELECT a.created_at FROM client_admissions a
            WHERE a.session_id=s.id AND a.kind='message' AND a.origin IN ('client','answer')
            ORDER BY a.created_at DESC LIMIT 1
        ), started)), 1, 23) AS bound
        FROM sessions s WHERE kind='chat' AND parent IS NULL
          AND (coworld_id IS NULL) = (json_extract(session_context, '$.league') IS NULL)
        ORDER BY bound DESC, started DESC, ordinal
        """
    ):
        tie = (row["started"], -row["ordinal"])
        if best is not None and (row["bound"], *tie) <= best:
            break
        activity = row["started"][:23]
        if row["bound"] > activity:
            newest = store.one(
                """
                WITH active AS (
                    SELECT op_id FROM client_admissions WHERE session_id=? AND kind='message'
                      AND status IN ('queued','running') ORDER BY created_at, rowid LIMIT ?
                ), terminal AS (
                    SELECT op_id FROM client_admissions WHERE session_id=? AND kind='message'
                      AND status NOT IN ('queued','running')
                    ORDER BY (SELECT at FROM events WHERE subject=op_id
                              ORDER BY seq DESC LIMIT 1) DESC, created_at DESC, rowid DESC LIMIT ?
                )
                SELECT max(created_at) FROM client_admissions
                WHERE op_id IN (SELECT op_id FROM active UNION ALL SELECT op_id FROM terminal)
                  AND (origin='answer' OR (origin='client'
                       AND json_type(request,'$.message')='object'))
                """,
                (row["id"], EMBEDDED_ACTIVE, row["id"], RECENT_TERMINAL),
            )[0]
            if newest:
                activity = max(activity, newest[:23])
        score = (activity, *tie)
        if best is None or score > best:
            best, winner = score, row["id"]
    return winner
