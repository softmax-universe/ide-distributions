"""Numbered SQL migrations, the expected schema and the in-place repair that keeps rows (B.2, B.10).

Migrations are ``migrations/<kind>/NNNN_name.sql``; ``PRAGMA user_version`` records the last
one applied. The store's shape is fixed machinery: ``expected_schema`` is what the migrations
produce on an empty database, ``actual_schema`` is what a file has, and ``Store.open`` refuses
to work through any difference. ``rebuild_keeping_rows`` is the watcher's repair: one
transaction inside the same file, after a copy of the original is saved next to it.
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from research_core.store.errors import SchemaNewer

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
KINDS = ("coworld", "workspace")
BUSY_TIMEOUT_MS = 5000
# A wait for, or a hold of, the write lock longer than this is logged (``lock_wait``).
LOCK_LOG_S = 1.0
WAL_SWITCH_POLL_S = 0.01
# The repair renames the tables it keeps rows from to this prefix while it copies them.
OLD_PREFIX = "old_"

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_SCHEMA_TYPES = ("table", "index", "trigger", "view")
# Tables FTS5 creates behind a virtual table; they are not part of the declared shape.
_FTS_SHADOW_SUFFIXES = ("data", "idx", "docsize", "config", "content")
_TIMESTAMP_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"not a store kind: {kind!r} (one of {', '.join(KINDS)})")


def store_name(conn: sqlite3.Connection) -> str:
    """The folder that holds a store's ``.state``: the workspace's or a Coworld's. Never the path
    (B.10)."""
    file = conn.execute("PRAGMA database_list").fetchone()[2]
    return Path(file).parents[1].name if file else ":memory:"


@contextmanager
def lock_wait(conn: sqlite3.Connection, operation: str) -> Iterator[None]:
    """Log a statement that waited over ``LOCK_LOG_S`` for the store's locks, or failed waiting.

    ``operation`` is ``wal_switch``, ``migration`` or ``begin_immediate``. A ``BEGIN IMMEDIATE``
    that ends in "database is locked" after the busy timeout is logged ``failed`` with its wait,
    then its error propagates unchanged.
    """
    started = time.monotonic()
    outcome = "failed"
    try:
        yield
        outcome = "acquired"
    finally:
        waited_s = time.monotonic() - started
        if outcome == "failed" or waited_s > LOCK_LOG_S:
            store = store_name(conn)
            log.warning(
                "store %s: %s %s after waiting %.2fs for its lock",
                store,
                operation,
                outcome,
                waited_s,
                extra={
                    "attributes": {
                        "store": store,
                        "store.operation": operation,
                        "store.lock_outcome": outcome,
                        "store.lock_wait_s": round(waited_s, 3),
                    }
                },
            )


def connect_writable(path: Path, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    """A connection that may write a store: WAL, ``synchronous=FULL`` and a busy wait, 5 s unless
    the caller needs longer.

    ``FULL`` fsyncs the WAL on every commit, so a committed record survives a power loss when
    the storage honours fsync (``NORMAL`` would only promise consistency, not the last commits).
    Every connection that writes a live store goes through here.
    """
    busy_timeout_ms = BUSY_TIMEOUT_MS if busy_timeout_ms is None else busy_timeout_ms
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    _enable_wal(conn, busy_timeout_ms)
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def _enable_wal(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    """Switch to WAL, waiting out another connection that is making the same switch.

    A file not yet in WAL mode (a store's first open, or a restored copy) needs a write lock taken
    while this statement holds a read lock, and SQLite fails that upgrade at once rather than wait,
    whatever the busy timeout. Two first opens of a new Coworld's store then raise "database is
    locked" within milliseconds. Starting the statement again drops the read lock and finds the
    file already in WAL mode. SQLite's timeout still applies to its other locks, so each attempt
    gets only what is left of the one wait.
    """
    deadline = time.monotonic() + busy_timeout_ms / 1000
    try:
        with lock_wait(conn, "wal_switch"):
            while True:
                left_ms = max(0, int((deadline - time.monotonic()) * 1000))
                conn.execute(f"PRAGMA busy_timeout = {left_ms}")
                try:
                    conn.execute("PRAGMA journal_mode = WAL").fetchone()
                    return
                except sqlite3.OperationalError as error:
                    # sqlite_errorcode is the extended code; its low byte is the primary one.
                    busy = error.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
                    if not busy or time.monotonic() >= deadline:
                        raise
                time.sleep(min(WAL_SWITCH_POLL_S, left_ms / 1000))
    finally:
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")


def migration_files(kind: str) -> list[tuple[int, Path]]:
    """The migrations of ``kind`` as ``(number, path)``, in numeric order."""
    _check_kind(kind)
    found: list[tuple[int, Path]] = []
    for path in sorted((MIGRATIONS_DIR / kind).iterdir()):
        if path.suffix != ".sql":
            continue
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise ValueError(f"migration file {path.name!r} is not named NNNN_name.sql")
        found.append((int(match.group(1)), path))
    found.sort()
    numbers = [number for number, _ in found]
    duplicates = sorted({f"{n:04d}" for n in numbers if numbers.count(n) > 1})
    if duplicates:
        raise ValueError(f"duplicate migration numbers: {', '.join(duplicates)}")
    return found


def latest_version(kind: str) -> int:
    return migration_files(kind)[-1][0]


def split_statements(script: str) -> list[str]:
    """Split a SQL script into statements, keeping trigger bodies whole."""
    statements: list[str] = []
    buffer: list[str] = []
    for line in script.splitlines(keepends=True):
        buffer.append(line)
        if sqlite3.complete_statement("".join(buffer)):
            statements.append("".join(buffer).strip())
            buffer = []
    rest = [line for line in buffer if line.strip() and not line.lstrip().startswith("--")]
    if rest:
        raise ValueError("the migration ends with an incomplete statement")
    return statements


def apply_migrations(conn: sqlite3.Connection, kind: str) -> int:
    """Apply every migration above ``PRAGMA user_version``; return the version reached.

    Each migration runs in its own ``BEGIN IMMEDIATE`` transaction and re-reads the version
    inside it, so two processes opening a brand-new store at once cannot both apply ``0001``.
    A failing migration rolls back whole. Calling this on a current store is a no-op.
    """
    for number, path in migration_files(kind):
        with lock_wait(conn, "migration"):
            conn.execute("BEGIN IMMEDIATE")
        try:
            if user_version(conn) >= number:
                conn.execute("COMMIT")
                continue
            _run_migration(conn, number, path)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return user_version(conn)


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _run_migration(conn: sqlite3.Connection, number: int, path: Path) -> None:
    """The statements of one migration and its version stamp; the caller owns the transaction."""
    for statement in split_statements(path.read_text()):
        conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {number}")


def normalise_sql(sql: str) -> str:
    """Whitespace collapsed to single spaces, lower case: the form schemas are compared in."""
    return " ".join(sql.split()).lower()


def actual_schema(conn: sqlite3.Connection) -> dict[str, str]:
    """``"<type> <name>" -> normalised SQL`` for every table, index, trigger and view.

    SQLite's own objects (``sqlite_sequence``, auto-indexes) and the shadow tables behind FTS5
    virtual tables are left out: they follow from the declared objects.
    """
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
    ).fetchall()
    fts_tables = {
        row[1] for row in rows if row[0] == "table" and " using fts5" in normalise_sql(row[2])
    }
    shadow = {f"{fts}_{suffix}" for fts in fts_tables for suffix in _FTS_SHADOW_SUFFIXES}
    schema: dict[str, str] = {}
    for type_, name, sql in rows:
        if type_ not in _SCHEMA_TYPES or name.startswith("sqlite_") or name in shadow:
            continue
        schema[f"{type_} {name}"] = normalise_sql(sql)
    return schema


@lru_cache
def expected_schema(kind: str) -> dict[str, str]:
    """The schema the migrations of ``kind`` produce on an empty database (cached, read-only)."""
    _check_kind(kind)
    conn = sqlite3.connect(":memory:")
    try:
        apply_migrations(conn, kind)
        return actual_schema(conn)
    finally:
        conn.close()


def expected_tables(kind: str) -> list[str]:
    """The ordinary (non-virtual) tables the migrations of ``kind`` create, by name."""
    return sorted(
        key.split(" ", 1)[1]
        for key, sql in expected_schema(kind).items()
        if key.startswith("table ") and not sql.startswith("create virtual table")
    )


def schema_diff(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    """One problem per object that is missing, unexpected or defined differently."""
    problems: list[str] = []
    for key in sorted(set(expected) | set(actual)):
        if key not in actual:
            problems.append(f"missing {key}")
        elif key not in expected:
            problems.append(f"unexpected {key}")
        elif expected[key] != actual[key]:
            problems.append(f"{key} differs: expected {expected[key]!r}, actual {actual[key]!r}")
    return problems


# --- the repair ------------------------------------------------------------------------------


@dataclass
class RebuildReport:
    tables: dict[str, int]
    dropped_columns: dict[str, list[str]]
    dropped_tables: list[str]
    user_version: int
    foreign_key_violations: int
    # The copy of the store as it was before the repair, next to the store file.
    backup: str

    def to_dict(self) -> dict:
        return asdict(self)


def rebuild_keeping_rows(path: Path, kind: str) -> RebuildReport:
    """Rebuild the store's schema from the migrations inside the same file, keeping its rows.

    One ``BEGIN IMMEDIATE`` transaction holds the write lock for the whole repair: a concurrent
    writer waits on its busy timeout or gets "database is locked", and readers keep seeing the
    old shape until the commit, so a committed change can never vanish. Inside it the derived
    objects (triggers, indexes, views, FTS tables) are dropped, tables the migrations do not
    know are dropped, every other table is renamed to ``old_<name>``, the migrations create the
    fresh schema, rows are copied over the columns both shapes share (``events`` keeps its
    ``seq``) and the ``old_`` tables are dropped. Anything that fails rolls back and leaves the
    file exactly as it was. The copy's own ``change_log`` rows are marked with a ``rebuild:``
    token and the old ``change_log`` is appended after them, so earlier unmarked writes stay
    visible.

    Before anything changes, and while the write lock already excludes other writers, a copy of
    the original is written next to it (``<stem>.before-repair-<timestamp>.db``) with SQLite's
    backup API; ``RebuildReport.backup`` names it. A store a newer release wrote is refused
    (``SchemaNewer``) rather than downgraded. The file keeps its inode, so open connections in
    other processes stay valid and see the new shape on their next statement.
    """
    _check_kind(kind)
    path = Path(path)
    conn = connect_writable(path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = user_version(conn)
            if version > latest_version(kind):
                raise SchemaNewer(version, latest_version(kind))
            backup = _copy_before_repair(path)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        try:
            report = _rebuild_in_place(conn, kind, backup)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            # The original is untouched, so the copy says nothing the file does not.
            backup.unlink(missing_ok=True)
            raise
    finally:
        conn.close()
    return report


def _copy_before_repair(path: Path) -> Path:
    """A consistent copy of the store next to it, made by a second connection.

    The caller holds the write lock, so nothing can commit between this copy and the repair; a
    second connection is needed because a connection cannot back itself up mid-transaction.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(f"{path.stem}.before-repair-{stamp}{path.suffix}")
    source = sqlite3.connect(path)
    dest = sqlite3.connect(backup)
    try:
        dest.execute("PRAGMA synchronous = FULL")
        source.backup(dest)
        dest.execute("PRAGMA journal_mode = DELETE")  # one self-contained file
    finally:
        dest.close()
        source.close()
    return backup


def _rebuild_in_place(conn: sqlite3.Connection, kind: str, backup: Path) -> RebuildReport:
    """The repair's statements, inside the caller's transaction."""
    present = _copyable_tables(conn, "main")
    wanted = expected_tables(kind)
    dropped_tables = sorted(set(present) - set(wanted))
    kept = [name for name in present if name in wanted]
    _drop_derived_objects(conn)
    for name in dropped_tables:
        conn.execute(f'DROP TABLE "{name}"')
    for name in kept:
        conn.execute(f'ALTER TABLE "{name}" RENAME TO "{OLD_PREFIX}{name}"')
    conn.execute("PRAGMA user_version = 0")
    for number, path in migration_files(kind):
        _run_migration(conn, number, path)
    tables, dropped_columns = _copy_rows(conn, wanted, kept)
    for name in kept:
        conn.execute(f'DROP TABLE "{OLD_PREFIX}{name}"')
    violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    return RebuildReport(
        tables=tables,
        dropped_columns=dropped_columns,
        dropped_tables=dropped_tables,
        user_version=user_version(conn),
        foreign_key_violations=violations,
        backup=str(backup),
    )


def _drop_derived_objects(conn: sqlite3.Connection) -> None:
    """Drop triggers, views, indexes and FTS tables: the migrations recreate every wanted one."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
    ).fetchall()
    for wanted_type in ("trigger", "view", "index"):
        for type_, name, _ in rows:
            if type_ == wanted_type:
                conn.execute(f'DROP {wanted_type.upper()} "{name}"')
    for type_, name, sql in rows:
        if type_ == "table" and normalise_sql(sql).startswith("create virtual table"):
            conn.execute(f'DROP TABLE "{name}"')


def _copy_rows(
    conn: sqlite3.Connection, new_tables: list[str], kept: list[str]
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Copy each ``old_<name>`` table into the fresh ``<name>`` over the columns they share."""
    tables: dict[str, int] = {}
    dropped_columns: dict[str, list[str]] = {}
    for name in new_tables:
        if name == "change_log":
            continue
        if name not in kept:
            tables[name] = 0
            continue
        old_name = f"{OLD_PREFIX}{name}"
        old_columns = _columns(conn, old_name)
        new_columns = set(_columns(conn, name))
        shared = [column for column in old_columns if column in new_columns]
        dropped = [column for column in old_columns if column not in new_columns]
        if dropped:
            dropped_columns[name] = dropped
        if shared:
            columns = ", ".join(f'"{column}"' for column in shared)
            # OR REPLACE: the migrations' own seed rows (meta) yield to the old store's values.
            conn.execute(
                f'INSERT OR REPLACE INTO "{name}" ({columns}) SELECT {columns} FROM "{old_name}"'
            )
        tables[name] = conn.execute(f'SELECT COUNT(*) FROM "{old_name}"').fetchone()[0]
    # The copy fired the change_log triggers: mark those rows as this rebuild's, then carry the
    # old log over (new ids, same tokens) so unmarked writes from before the rebuild stay visible.
    conn.execute(
        "UPDATE change_log SET token = ? WHERE token IS NULL",
        (f"rebuild:{secrets.token_hex(4)}",),
    )
    if "change_log" in kept:
        old_log = f"{OLD_PREFIX}change_log"
        shared = [c for c in _columns(conn, old_log) if c in _CHANGE_LOG_COPY]
        columns = ", ".join(f'"{column}"' for column in shared)
        conn.execute(
            f'INSERT INTO change_log ({columns}) SELECT {columns} FROM "{old_log}" ORDER BY id'
        )
        tables["change_log"] = conn.execute(f'SELECT COUNT(*) FROM "{old_log}"').fetchone()[0]
    return tables, dropped_columns


_CHANGE_LOG_COPY = ("at", "tbl", "row_id", "op", "token")


def _copyable_tables(conn: sqlite3.Connection, schema: str) -> list[str]:
    """Ordinary tables of an attached database: no virtual tables, shadow tables or sqlite_*."""
    rows = conn.execute(
        f"SELECT name, sql FROM {schema}.sqlite_master WHERE type = 'table' AND sql IS NOT NULL"
        " ORDER BY name"
    ).fetchall()
    virtual = {name for name, sql in rows if normalise_sql(sql).startswith("create virtual table")}
    shadow = {f"{name}_{suffix}" for name in virtual for suffix in _FTS_SHADOW_SUFFIXES}
    return [
        name
        for name, _ in rows
        if name not in virtual and name not in shadow and not name.startswith("sqlite_")
    ]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
