"""Store and Tx: opening a store, token-marked transactions, events, detection (design B.2, B.10).

Every library write is one ``BEGIN IMMEDIATE`` transaction (``Store.tx``), so concurrent writers
serialise. Triggers log each change to ``change_log`` with a NULL token; before commit the
transaction stamps its own rows with its token, so any NULL token left after a commit is a write
made outside the library (``unmarked_changes``). ``open`` applies pending migrations and refuses
a file whose schema differs from the migrations (``SchemaMismatch``) or that a newer release
wrote (``SchemaNewer``). ``read_tx`` gives several reads one consistent view of the store.
``observe_commits`` names who sees a transaction's events, only after its COMMIT has succeeded.

Error messages never name the store path (B.10).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from research_core.store import migrate
from research_core.store._common import Clock, json_field, now_iso, parse_iso
from research_core.store.actors import Actor
from research_core.store.errors import SchemaMismatch, SchemaNewer
from research_core.store.events import Event

# In-process wakeups are hints. SQLite event cursors remain the durable source of truth.
_changes = threading.Condition()
_change_version = 0


# ``agent_telemetry.observed`` while a process has a telemetry writer. It must not raise.
_commit_observer: Callable[[Store, Actor, list[tuple[int, str, str, dict]]], None] | None = None


def observe_commits(
    observer: Callable[[Store, Actor, list[tuple[int, str, str, dict]]], None] | None,
) -> None:
    global _commit_observer
    _commit_observer = observer


def change_version() -> int:
    with _changes:
        return _change_version


def wait_for_change(version: int, timeout: float) -> None:
    with _changes:
        _changes.wait_for(lambda: version != _change_version, timeout)


STATE_README = """\
# .state

Records live in store.db and are changed through the profile tools (goal, task, claim, ...).
Direct edits are detected and reported. Do not edit files here.
"""
# ``research store quiesce`` writes this file in the workspace's ``.state`` while it holds every
# database's write lock for a volume snapshot, and removes it when it releases them.
QUIESCE_MARKER = "quiesce.json"
DEFAULT_MAX_HOLD_S = 30.0


def quiesce_busy_timeout_ms(store_path: Path, now: datetime | None = None) -> int | None:
    """The busy timeout a fresh quiesce marker asks for; ``None`` when there is none.

    A store opened while ``research store quiesce`` holds the write locks waits the quiesce's
    ``max_hold_s`` plus the usual 5 s, so its first write lands after the release instead of
    failing, and a restored copy not yet in WAL mode waits to make its switch. The marker is
    looked for in the store's own ``.state`` (the workspace store) and in
    the workspace's ``.state`` two folders up (a Coworld store). A marker older than its own
    max hold is a leftover of a quiesce that did not clean up, and is ignored.
    """
    for marker in _quiesce_markers(store_path):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            held_at = parse_iso(data["held_at"])
            max_hold_s = float(data["max_hold_s"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        age_s = ((now or datetime.now(UTC)) - held_at).total_seconds()
        if age_s <= max_hold_s:
            return int(max_hold_s * 1000) + migrate.BUSY_TIMEOUT_MS
    return None


def _quiesce_markers(store_path: Path) -> list[Path]:
    markers = [store_path.parent / QUIESCE_MARKER]
    if len(store_path.parents) > 2:
        markers.append(store_path.parents[2] / ".state" / QUIESCE_MARKER)
    return markers


@dataclass(frozen=True)
class SchemaStatus:
    ok: bool
    diff: list[str]
    user_version: int
    expected_version: int
    # A newer release of this kind wrote the store: refused, never repaired.
    newer: bool = False


def schema_status(conn: sqlite3.Connection, kind: str) -> SchemaStatus:
    """Compare a connection's schema with the migrations of ``kind``; every difference listed.

    A version above the release's latest migration marks the status ``newer``, unless the
    store says it is of another kind: that is a plain mismatch, whatever its version.
    """
    expected = migrate.expected_schema(kind)
    actual = migrate.actual_schema(conn)
    diff = migrate.schema_diff(expected, actual)
    user_version = migrate.user_version(conn)
    expected_version = migrate.latest_version(kind)
    if user_version != expected_version:
        diff.append(f"user_version is {user_version}, expected {expected_version}")
    right_kind = True
    if "table meta" in actual:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_kind'").fetchone()
        schema_kind = None if row is None else row[0]
        right_kind = schema_kind == kind
        if not right_kind:
            diff.append(f"meta.schema_kind is {schema_kind!r}, expected {kind!r}")
    return SchemaStatus(
        ok=not diff,
        diff=diff,
        user_version=user_version,
        expected_version=expected_version,
        newer=right_kind and user_version > expected_version,
    )


class Store:
    """One open connection to a Coworld or workspace store."""

    def __init__(
        self,
        path: Path,
        kind: Literal["coworld", "workspace"],
        conn: sqlite3.Connection,
        clock: Clock | None = None,
    ) -> None:
        self.path = path
        self.kind = kind
        self.conn = conn
        self.clock = clock
        self._tx: Tx | None = None
        self._atomic: Tx | None = None

    @classmethod
    def open(
        cls, path: Path, *, kind: Literal["coworld", "workspace"], clock: Clock | None = None
    ) -> Store:
        """Open (creating and migrating if needed), check the schema, write the README.

        WAL journal, ``synchronous=FULL``, foreign keys on, a 5 s busy timeout (longer while a
        ``research store quiesce`` holds the write locks, see ``quiesce_busy_timeout_ms``).
        Pending migrations are applied; a store a newer release wrote raises ``SchemaNewer`` and
        a schema that differs from the migrations raises ``SchemaMismatch`` with every difference.
        """
        if kind not in migrate.KINDS:
            raise ValueError(f"not a store kind: {kind!r}")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = migrate.connect_writable(path, quiesce_busy_timeout_ms(path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if migrate.user_version(conn) < migrate.latest_version(kind):
                migrate.apply_migrations(conn, kind)
            store = cls(path, kind, conn, clock)
            status = store.schema_status()
            if status.newer:
                raise SchemaNewer(status.user_version, status.expected_version)
            if not status.ok:
                raise SchemaMismatch(status.diff)
        except BaseException:
            conn.close()
            raise
        readme = path.parent / "README.md"
        if not readme.exists():
            readme.write_text(STATE_README)
        return store

    # --- reads ---------------------------------------------------------------------------

    def q(self, sql: str, params: tuple | list | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def now(self) -> str:
        """The current time from the store's clock (tests inject one)."""
        return now_iso(self.clock)

    def events_after(self, seq: int, limit: int = 500) -> list[Event]:
        rows = self.q("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit))
        return [Event.from_row(row) for row in rows]

    def last_seq(self) -> int:
        return self.one("SELECT COALESCE(MAX(seq), 0) FROM events")[0]

    def get_meta(self, key: str) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        """``meta`` is the store's own bookkeeping (heartbeat, has_work); it is not logged."""
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def delete_meta(self, key: str) -> None:
        self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    @contextmanager
    def read_tx(self) -> Iterator[Store]:
        """One consistent view for several reads: ``with store.read_tx(): ...``.

        A deferred ``BEGIN`` fixes the snapshot at the first read, so a writer committing on
        another connection in the middle cannot make one query newer than the previous one
        (the rows of a snapshot and its ``seq`` cursor agree). Writers are not blocked.
        """
        if self.conn.in_transaction:
            raise RuntimeError("a transaction is already open on this store")
        self.conn.execute("BEGIN")
        try:
            yield self
        finally:
            self.conn.execute("ROLLBACK")

    # --- the schema check and detection --------------------------------------------------

    def schema_status(self) -> SchemaStatus:
        return schema_status(self.conn, self.kind)

    def unmarked_changes(self) -> list[dict[str, Any]]:
        """``change_log`` rows no library transaction stamped: writes made outside the library."""
        rows = self.q(
            "SELECT id, at, tbl, row_id, op FROM change_log WHERE token IS NULL ORDER BY id"
        )
        return [dict(row) for row in rows]

    def acknowledge_unmarked(self, ids: list[int], note: str = "reported") -> int:
        """Stamp reported unmarked rows ``ack:<note>`` so they are not reported again."""
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        cursor = self.conn.execute(
            f"UPDATE change_log SET token = ? WHERE token IS NULL AND id IN ({placeholders})",
            (f"ack:{note}", *ids),
        )
        return cursor.rowcount

    # --- writes --------------------------------------------------------------------------

    def tx(self, actor: Actor) -> Tx | nullcontext[Tx]:
        """One transaction: ``with store.tx(actor) as tx: ...``. Nested use raises."""
        if self._atomic is not None:
            if actor != self._atomic.actor:
                raise RuntimeError("atomic command cannot change its actor")
            return nullcontext(self._atomic)
        return Tx(self, actor)

    @contextmanager
    def atomic(self, actor: Actor) -> Iterator[Tx]:
        """Commit domain effects and their command receipt together."""
        with self.tx(actor) as tx:
            self._atomic = tx
            try:
                yield tx
            finally:
                self._atomic = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class Tx:
    """One ``BEGIN IMMEDIATE`` transaction with a token; commit stamps ``change_log``.

    ``at`` is the transaction's timestamp: every row and event written in it shares one time.
    """

    def __init__(self, store: Store, actor: Actor) -> None:
        self.store = store
        self.actor = actor
        self.token = secrets.token_hex(8)
        self.at = store.now()
        self._open = False
        self._locked_at = 0.0
        self._start_id = 0
        self._start_event = 0
        self._events: list[tuple[int, str, str, dict]] = []

    def __enter__(self) -> Tx:
        if self.store._tx is not None:
            raise RuntimeError("a transaction is already open on this store")
        conn = self.store.conn
        with migrate.lock_wait(conn, "begin_immediate"):
            conn.execute("BEGIN IMMEDIATE")
        self._locked_at = time.monotonic()
        self.store._tx = self
        self._open = True
        self._start_event = self.store.last_seq()
        # Rows above this id are ours: the write lock is held from here until commit.
        self._start_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()[0]
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        conn = self.store.conn
        try:
            if exc_type is None:
                conn.execute(
                    "UPDATE change_log SET token = ? WHERE token IS NULL AND id > ?",
                    (self.token, self._start_id),
                )
                conn.execute("COMMIT")
                if self.store.last_seq() > self._start_event:
                    global _change_version
                    with _changes:
                        _change_version += 1
                        _changes.notify_all()
            else:
                conn.execute("ROLLBACK")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            self._open = False
            self.store._tx = None
            migrate.log_lock_held(conn, str(self.actor), time.monotonic() - self._locked_at)
        if exc_type is None and self._events and _commit_observer is not None:
            _commit_observer(self.store, self.actor, self._events)
        return False

    def _check_open(self) -> None:
        if not self._open:
            raise RuntimeError("the transaction is not open")

    def execute(self, sql: str, params: tuple | list | dict = ()) -> sqlite3.Cursor:
        self._check_open()
        return self.store.conn.execute(sql, params)

    def q(self, sql: str, params: tuple | list | dict = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list | dict = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def event(self, kind: str, subject: str, payload: dict[str, Any]) -> int:
        """Append an ``events`` row by this transaction's actor; returns its ``seq``."""
        if not isinstance(payload, dict):
            raise TypeError("an event payload is a dict")
        cursor = self.execute(
            "INSERT INTO events(at, actor, kind, subject, payload) VALUES (?, ?, ?, ?, ?)",
            (self.at, str(self.actor), kind, subject, json_field(payload)),
        )
        self._events.append((cursor.lastrowid, kind, subject, payload))
        return cursor.lastrowid
