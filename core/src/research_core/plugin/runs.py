"""Which ``/v1/runs`` run a Session is executing, read from Hermes's own database.

Hermes admits a run's input as a row of ``session_admissions`` whose ``request_id`` is the run's
ID, and executes one admission per Session at a time: the one whose ``status`` is ``started``. A
delegated child Session has no admission of its own; it works for its parent's run, which
``subagent_start`` names. Nothing here guesses "the Session's latest run".
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

# A delegated child Session's parent, by live Session ID. Single dict operations need no lock.
parents: dict[str, str] = {}


def _state_db():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state.db"


def run_of(session_id: str | None) -> str | None:
    """The run ``session_id`` (or the Session it was delegated from) is executing, or ``None``."""
    seen = set()
    while session_id and session_id not in seen:
        seen.add(session_id)
        db = _state_db()
        if not db.exists():
            return None
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)) as connection:
            row = connection.execute(
                "SELECT request_id, payload_json FROM session_admissions "
                "WHERE target_session_id = ? AND status = 'started'",
                (session_id,),
            ).fetchone()
        if row is not None:
            api_turn = json.loads(row[1]).get("api_turn_v1") or {}
            return row[0] if "run_owner_scope" in api_turn else None
        session_id = parents.get(session_id)
    return None
