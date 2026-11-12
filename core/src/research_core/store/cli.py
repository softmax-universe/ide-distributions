"""``research store``: user edits, exports, snapshots and the event stream (design A.10, A.11).

``apply`` is how a user edit reaches the store. The bridge turns a user's edit into
``research store apply --actor user:<id> <op> <json>``, which calls the same library function
the tools call, so the record rules, the ``events`` row and the token-marked transaction are the
same whoever writes. Only the ops in ``ALLOWED`` run, and only for user or harness actors:
sessions use the tools. The reply carries the store's ``seq`` after the write, which is what
"accepted on commit" means to a client (A.11). ``snapshot`` and ``events`` are the read side of
the seam: a client loads a snapshot, then streams from its ``seq``; each store has its own
sequence, so both commands work on one store at a time. ``export`` dumps every table of both
stores as JSON lines for debugging. ``backup`` copies every database of the workspace with
SQLite's backup API into a timestamped folder with a manifest, and ``restore-check`` is the
restore test for such a folder (design R6 as amended by the data-loss review). ``quiesce``
checkpoints, write-locks and fsyncs the same databases and holds them for the moment a volume
snapshot is initiated, so the snapshot is one recovery point across the databases and the files.

Every result is one JSON line on stdout: ``{"ok": true, ...}`` or ``{"ok": false, "error",
"problems"}`` with exit code 1 (``quiesce`` prints one line when it holds and one when it
releases). The commands never show a traceback.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_core import layout
from research_core.store import (
    admissions,
    directives,
    goals,
    migrate,
    sessions,
    settings,
    tasks,
)
from research_core.store._common import now_iso, parse_display_id, require
from research_core.store.actors import Actor
from research_core.store.db import DEFAULT_MAX_HOLD_S, QUIESCE_MARKER, Store, schema_status
from research_core.store.errors import StoreError
from research_core.store.migrate import _copyable_tables
from research_core.store.registry import RECORDS, RecordSpec, record_from_row

# The user edits of design A.10 plus amendment A2.10. ``<module>.<function>`` names the library
# function; the ``workspace.*`` ops are small compositions defined in ``_call``.
ALLOWED: frozenset[str] = frozenset(
    {
        "goals.set_status",
        "goals.update",
        "tasks.approve",
        "tasks.update",
        "tasks.reject",
        "tasks.abandon",
        "settings.update",
        "directives.add",
        "directives.update",
        "directives.retire",
        "workspace.pause",
        "workspace.resume",
        "workspace.coworld_switch",
    }
)
LIBRARY_MODULES = {"goals": goals, "tasks": tasks, "settings": settings, "directives": directives}
STORE_KINDS = ("workspace", "coworld")
# Which record kind each id argument names, so ``G-3`` becomes 3 for ``goal_id`` and a Task id
# given as ``goal_id`` is refused instead of silently reading the wrong table. ``directive_id``
# stays in display form until ``_call``, because its prefix names the store.
ID_ARGUMENTS = {"goal_id": "goal", "task_id": "task"}
NO_ACTIVE_COWORLD = (
    "no session Coworld; pass --coworld for user edits, or open a session bound to a Coworld"
)
OUTPUT_TAIL_CHARS = 2000


class Refusal(Exception):
    """A result the command prints as ``{"ok": false, "error", "problems"}``."""

    def __init__(self, error: str, problems: Sequence[str] = ()) -> None:
        self.error = error
        self.problems = list(problems)
        super().__init__(error)


class Stores:
    """The workspace store and, opened on first use, the active (or named) Coworld's store."""

    def __init__(self, ws: layout.Workspace, wstore: Store, coworld_id: str | None) -> None:
        self.ws = ws
        self.wstore = wstore
        self.coworld_id = coworld_id
        self._store: Store | None = None

    @classmethod
    def open(cls, coworld_id: str | None = None) -> Stores:
        """Open the workspace store under ``IDE_WORKSPACE``; the Coworld is the named or active."""
        ws = layout.from_env()
        wstore = Store.open(ws.store_path, kind="workspace")
        session_id = os.environ.get("IDE_SESSION_ID") or os.environ.get("HERMES_SESSION_ID")
        if session_id:
            session = sessions.read(wstore, session_id)
            bound = session["coworld_id"] if session else None
            if coworld_id is not None and coworld_id != bound:
                wstore.close()
                raise StoreError(["the requested Coworld differs from this session"])
            coworld_id = bound
        elif coworld_id is None:
            coworld_id = os.environ.get("IDE_COWORLD_ID")
        return cls(ws, wstore, coworld_id)

    @property
    def coworld_store_path(self) -> Path | None:
        return None if self.coworld_id is None else self.ws.coworld(self.coworld_id).store_path

    def store(self) -> Store:
        if self._store is None:
            if self.coworld_store_path is None:
                raise StoreError([NO_ACTIVE_COWORLD])
            self._store = Store.open(self.coworld_store_path, kind="coworld")
        return self._store

    def by_kind(self, kind: str) -> Store:
        return self.wstore if kind == "workspace" else self.store()

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
        self.wstore.close()

    def __enter__(self) -> Stores:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _fail(error: str, problems: Sequence[str] = ()) -> int:
    print(json.dumps({"ok": False, "error": error, "problems": list(problems)}))
    return 1


# --- apply ----------------------------------------------------------------------------------


def run_apply(args: argparse.Namespace) -> int:
    op = args.op
    try:
        actor = parse_actor(args.actor)
        if op not in ALLOWED:
            raise Refusal(
                f"{op!r} is not an op store apply runs; allowed: {', '.join(sorted(ALLOWED))}"
            )
        op_args = parse_op_args(args.args)
        with Stores.open(args.coworld) as stores:
            kind = args.store or store_kind_for(op, op_args)
            record = _call(op, kind, stores, actor, op_args)
            seq = stores.by_kind(kind).last_seq()
    except Refusal as error:
        return _fail(error.error, error.problems)
    except StoreError as error:
        return _fail(f"{op} refused", error.problems)
    except Exception as error:  # the bridge reads JSON, never a traceback
        return _fail(f"{op} failed: {type(error).__name__}: {error}")
    from research_core.transitions import CommandReceipt

    if isinstance(record, CommandReceipt):
        print(
            json.dumps(
                {
                    "status": "pending",
                    "op": op,
                    "store": kind,
                    "command": record.payload,
                    "seq": seq,
                },
                ensure_ascii=False,
            )
        )
        return 2
    result = {"ok": True, "op": op, "store": kind, "record": record, "seq": seq}
    print(json.dumps(result, ensure_ascii=False))
    return 0


def parse_actor(text: str) -> Actor:
    try:
        actor = Actor.parse(text)
    except ValueError as error:
        raise Refusal(str(error)) from None
    if actor.is_session:
        raise Refusal("sessions use the tools; store apply is for user:<id> and harness actors")
    return actor


def parse_op_args(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except ValueError as error:
        raise Refusal(f"the op arguments are not JSON: {error}") from None
    if not isinstance(parsed, dict):
        raise Refusal("the op arguments must be a JSON object of the function's arguments")
    problems: list[str] = []
    for name, kind in ID_ARGUMENTS.items():
        if isinstance(parsed.get(name), str):
            parsed[name] = _record_number(parsed[name], name, kind, problems)
    if problems:
        raise StoreError(problems)
    return parsed


def _record_number(value: str, name: str, kind: str, problems: list[str]) -> Any:
    """``"G-3"`` → 3 for ``goal_id``; another kind's id is a problem; other text passes through."""
    try:
        found_kind, number = parse_display_id(value)
    except ValueError:
        return value
    if found_kind != kind:
        problems.append(f"{name} is {value!r}, a {found_kind} id; expected a {kind} id")
        return value
    return number


def store_kind_for(op: str, op_args: dict[str, Any]) -> str:
    """Which store the op writes: settings by the key's scope, directives by their scope or
    id, Goals and Tasks in the active Coworld, the ``workspace.*`` ops in the workspace."""
    module = op.split(".", 1)[0]
    if module in ("goals", "tasks"):
        return "coworld"
    if module == "workspace":
        return "workspace"
    if op == "settings.update":
        key = op_args.get("key")
        if not isinstance(key, str):
            raise StoreError(["key is required: the setting to change"])
        return settings.spec(key).scope
    if op == "directives.add":
        return "workspace" if op_args.get("scope") == directives.WORKSPACE_SCOPE else "coworld"
    return directives.parse_reference(op_args.get("directive_id"))[0]


def _with_directive_number(op_args: dict[str, Any], kind: str) -> dict[str, Any]:
    """Replace ``directive_id`` with its number, refusing a store that contradicts its prefix."""
    reference = op_args["directive_id"]
    home, number = directives.parse_reference(reference)
    if home != kind:
        raise Refusal(f"{reference} is in the {home} store, not the {kind} store")
    return {**op_args, "directive_id": number}


def _call(
    op: str,
    kind: str,
    stores: Stores,
    actor: Actor,
    op_args: dict[str, Any],
    *,
    coordinator_instance: str | None = None,
) -> Any:
    if "directive_id" in op_args:
        op_args = _with_directive_number(op_args, kind)
    store = stores.by_kind(kind)
    if op.startswith("tasks.") and coordinator_instance is None:
        from uuid import uuid4

        from research_core.tools.base import ToolContext
        from research_core.transitions import CommandReceipt, request

        ctx = ToolContext(
            role="orchestrator",
            actor=actor,
            workspace=stores.ws,
            env={"IDE_WORKSPACE": str(stores.ws.root), "IDE_COWORLD_ID": stores.coworld_id},
        )
        ctx.wstore, ctx.store = stores.wstore, store
        args = dict(op_args)
        operation_id = args.pop("operation_id", str(uuid4()))
        result = request(ctx, "store", {"op": op, "args": args}, operation_id)
        receipt = CommandReceipt.parse(result)
        if receipt.state == "pending":
            return receipt
        if receipt.state == "refused":
            raise StoreError(result["problems"])
        return result["record"]
    if op == "workspace.coworld_switch":
        return _coworld_switch(actor, op_args)
    if op in ("workspace.pause", "workspace.resume"):
        # The op's only argument is cites; the library says what it is when it is missing.
        unknown = sorted(set(op_args) - {"cites"})
        if unknown:
            raise Refusal(
                f"arguments do not fit {op}(cites): unknown arguments: {', '.join(unknown)}"
            )
        paused = op == "workspace.pause"
        return settings.update(store, actor, "paused", paused, cites=op_args.get("cites"))
    module, name = op.split(".", 1)
    function = getattr(LIBRARY_MODULES[module], name)
    bound = _bind(op, function, (store, actor), op_args)
    return function(*bound.args, **bound.kwargs)


def _bind(
    op: str, function: Callable[..., Any], leading: tuple, op_args: dict[str, Any]
) -> inspect.BoundArguments:
    """Match the JSON arguments to the function before calling it, so a missing or unknown
    argument is a refusal that shows the arguments the op takes."""
    signature = inspect.signature(function)
    try:
        return signature.bind(*leading, **op_args)
    except TypeError as error:
        shown = inspect.Signature(list(signature.parameters.values())[len(leading) :])
        raise Refusal(f"arguments do not fit {op}{shown}: {error}") from None


def _coworld_switch(actor: Actor, op_args: dict[str, Any]) -> dict[str, Any]:
    """Delegate to ``research coworld switch`` in the workspace environment (A2.10): the switch
    fetches the Coworld's source and mixin, which needs the platform client."""
    problems: list[str] = []
    coworld = op_args.get("coworld")
    cites = op_args.get("cites")
    require(
        problems,
        isinstance(coworld, str) and coworld.strip(),
        "coworld is required: the Coworld id to switch to",
    )
    require(
        problems,
        cites is None or (isinstance(cites, str) and cites.strip()),
        "cites must be the reference of the user's message",
    )
    unknown = sorted(set(op_args) - {"coworld", "cites"})
    require(problems, not unknown, f"unknown arguments: {', '.join(unknown)}")
    if problems:
        raise StoreError(problems)
    argv = [sys.executable, "-m", "research_core.cli", "coworld", "switch", coworld]
    if cites:
        argv += ["--cites", cites]
    done = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "IDE_ACTOR": str(actor)},
    )
    if done.returncode != 0:
        tails = [done.stderr.strip()[-OUTPUT_TAIL_CHARS:], done.stdout.strip()[-OUTPUT_TAIL_CHARS:]]
        raise StoreError(
            [f"research coworld switch {coworld} exited {done.returncode}", *filter(None, tails)]
        )
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    try:
        record = json.loads(lines[-1]) if lines else None
    except ValueError:
        record = None
    if isinstance(record, dict):
        return record
    return {"output": done.stdout.strip()[-OUTPUT_TAIL_CHARS:]}


# --- export ---------------------------------------------------------------------------------


def run_export(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    try:
        with Stores.open(args.coworld) as stores:
            if args.coworld and not stores.coworld_store_path.exists():
                raise StoreError([f"Coworld {args.coworld} has no store in this workspace"])
            summary: dict[str, Any] = {"out": str(out_dir), "stores": {}}
            summary["stores"]["workspace"] = _export_store(stores.wstore, out_dir / "workspace")
            if stores.coworld_id:
                folder = out_dir / stores.coworld_id
                summary["stores"][stores.coworld_id] = _export_store(stores.store(), folder)
    except (StoreError, OSError) as error:
        return _fail(str(error), getattr(error, "problems", []))
    print(json.dumps({"ok": True, **summary}, ensure_ascii=False))
    return 0


def _export_store(store: Store, folder: Path) -> dict[str, Any]:
    """One ``<table>.jsonl`` per ordinary table (FTS indexes are derived; they are left out).

    Every table and the ``seq`` are read inside one read transaction, so a writer committing
    meanwhile cannot leave the export with a newer table than its cursor.
    """
    folder.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with store.read_tx():
        for table in _copyable_tables(store.conn, "main"):
            rows = store.q(f"SELECT * FROM {table} ORDER BY rowid")
            with (folder / f"{table}.jsonl").open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
            counts[table] = len(rows)
        seq = store.last_seq()
    return {"path": str(folder), "seq": seq, "tables": counts}


# --- backup and restore-check ---------------------------------------------------------------

MANIFEST_NAME = "manifest.json"
HERMES_STATE_DB = "state.db"
# Schema verdicts restore-check gives a copied store; ``current`` and ``older`` restore cleanly
# (``Store.open`` migrates an older store), the other two would be refused or repaired.
RESTORABLE_SCHEMAS = ("current", "older")


def run_backup(args: argparse.Namespace) -> int:
    try:
        manifest = backup_workspace(layout.from_env(), Path(args.dest), os.environ)
    except (sqlite3.Error, OSError) as error:
        return _fail(f"backup failed: {error}")
    print(json.dumps({"ok": True, **manifest}, ensure_ascii=False))
    return 0


def backup_workspace(ws: layout.Workspace, dest_root: Path, env: Mapping[str, str]) -> dict:
    """Copy every database of the workspace into ``<dest_root>/<timestamp>/`` with a manifest.

    The workspace store, every registered Coworld's store and, when ``HERMES_HOME`` is set,
    that profile's Hermes ``state.db`` are copied with SQLite's backup API (never a raw file
    copy of a live database). The stores are read with plain read-only connections, so a
    store this release refuses to open is still backed up. The manifest records each copy's
    source, size and sha256, the workspace store's ``last_seq`` as copied, and is what
    ``restore-check`` verifies.
    """
    created = datetime.now(UTC)
    folder = dest_root / created.strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True, exist_ok=True)
    databases = [
        copy_database(source, folder / name, name=name, kind=kind)
        for name, kind, source in backup_sources(ws, env)
    ]
    workspace = next(entry for entry in databases if entry["kind"] == "workspace")
    manifest = {
        "created": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "workspace": str(ws.root),
        "dir": str(folder),
        "last_seq": workspace["last_seq"],
        "databases": databases,
    }
    (folder / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _fsync(folder / MANIFEST_NAME)
    _fsync(folder)
    return manifest


def backup_sources(ws: layout.Workspace, env: Mapping[str, str]) -> list[tuple[str, str, Path]]:
    """``(copy name, kind, source path)`` for every database a backup covers."""
    if not ws.store_path.exists():
        raise FileNotFoundError("the workspace has no store yet; nothing to back up")
    sources = [("workspace.db", "workspace", ws.store_path)]
    conn = _read_only(ws.store_path)
    try:
        ids = [
            row[0]
            for row in conn.execute("SELECT coworld_id FROM coworlds ORDER BY created, coworld_id")
        ]
    finally:
        conn.close()
    for coworld_id in ids:
        store_path = ws.coworld(coworld_id).store_path
        if store_path.exists():
            sources.append((f"coworld-{coworld_id}.db", "coworld", store_path))
    if env.get("HERMES_HOME"):
        hermes_state = Path(env["HERMES_HOME"]).expanduser() / HERMES_STATE_DB
        if hermes_state.exists():
            sources.append(("hermes-state.db", "hermes", hermes_state))
    return sources


def lock_order(ws: layout.Workspace, env: Mapping[str, str]) -> list[Path]:
    """The databases ``backup`` covers, in the order every holder of several write locks takes them.

    A transition writes to its Coworld store's transaction and then, inside it, to the workspace
    store, so anything locking both must take a Coworld store first. The other order lets the
    two wait on each other until one of them fails ``database is locked``. Hermes's ``state.db``
    comes last: nothing here writes it inside a store's transaction.
    """
    rank = {"coworld": 0, "workspace": 1, "hermes": 2}
    return [path for _, kind, path in sorted(backup_sources(ws, env), key=lambda s: rank[s[1]])]


def copy_database(
    source: Path,
    dest: Path,
    *,
    name: str,
    kind: str,
    pages: int = -1,
    progress: Callable[[int, int, int], object] | None = None,
) -> dict[str, Any]:
    """One consistent copy of a live SQLite database through the backup API.

    ``pages`` and ``progress`` are the backup API's own (all pages in one step by default); a
    write by another connection while a stepped copy runs restarts the copy, so the result is
    always one committed state. The copy is left in rollback-journal mode: one plain file.
    """
    src = _read_only(source)
    dst = sqlite3.connect(dest)
    try:
        dst.execute("PRAGMA synchronous = FULL")
        src.backup(dst, pages=pages, progress=progress)
        dst.execute("PRAGMA journal_mode = DELETE")
        entry = {"name": name, "kind": kind, "source": str(source)}
        if kind in migrate.KINDS:
            row = dst.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
            entry["last_seq"] = row[0]
    finally:
        dst.close()
        src.close()
    entry["bytes"] = dest.stat().st_size
    entry["sha256"] = _sha256(dest)
    return entry


def run_restore_check(args: argparse.Namespace) -> int:
    try:
        verdict = restore_check(Path(args.backup_dir))
    except (OSError, ValueError, KeyError) as error:
        return _fail(f"restore-check failed: {type(error).__name__}: {error}")
    print(json.dumps({"ok": verdict["ok"], **verdict}, ensure_ascii=False))
    return 0 if verdict["ok"] else 1


def restore_check(folder: Path) -> dict[str, Any]:
    """Open every copy of a backup folder read-only and say whether it would restore.

    Each database must pass ``PRAGMA integrity_check`` and match the manifest's sha256; a store
    must also have a schema this release restores (current, or older with migrations pending).
    """
    manifest = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
    checks = [check_database(folder / entry["name"], entry) for entry in manifest["databases"]]
    return {
        "ok": all(check["ok"] for check in checks),
        "dir": str(folder),
        "created": manifest["created"],
        "last_seq": manifest["last_seq"],
        "databases": checks,
    }


def check_database(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"name": entry["name"], "kind": entry["kind"]}
    if not path.exists():
        return {**result, "ok": False, "integrity": "missing"}
    result["sha256_matches"] = _sha256(path) == entry["sha256"]
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        result["integrity"] = "ok" if rows == [("ok",)] else "; ".join(row[0] for row in rows)
        if entry["kind"] in migrate.KINDS:
            status = schema_status(conn, entry["kind"])
            result["schema"] = _schema_verdict(status)
            result["user_version"] = status.user_version
            result["expected_version"] = status.expected_version
    except sqlite3.DatabaseError as error:
        result["integrity"] = str(error)
    finally:
        conn.close()
    result["ok"] = (
        result["integrity"] == "ok"
        and result["sha256_matches"]
        and result.get("schema", "current") in RESTORABLE_SCHEMAS
    )
    return result


def _schema_verdict(status) -> str:
    if status.ok:
        return "current"
    if status.newer:
        return "newer"
    if status.user_version < status.expected_version:
        return "older"
    return "drift"


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --- quiesce --------------------------------------------------------------------------------

# How many times the checkpoint is tried when a writer commits between it and the lock.
CHECKPOINT_ATTEMPTS = 3


def run_quiesce(args: argparse.Namespace) -> int:
    if args.max_hold <= 0:
        return _fail("--max-hold must be a positive number of seconds")
    if args.then is not None and not args.then:
        return _fail("--then needs a command to run while the locks are held")
    ws = layout.from_env()
    try:
        databases = lock_order(ws, os.environ)
    except (sqlite3.Error, OSError) as error:
        return _fail(f"quiesce failed: {error}")
    return quiesce(ws, databases, max_hold_s=args.max_hold, then=args.then)


def quiesce(
    ws: layout.Workspace, databases: list[Path], *, max_hold_s: float, then: list[str] | None
) -> int:
    """Checkpoint, write-lock and fsync every database, hold, then release the locks.

    The databases are those ``backup`` copies, in ``lock_order``, so a quiesce cannot deadlock
    with another one or with a cross-store writer. No commit lands anywhere once the last lock is
    held, so the order does not change what a snapshot taken after that contains. Readers
    continue throughout; a writer waits on its busy timeout, which the marker file raises for
    stores opened during the hold (``Store.open``). The hold is the
    ``--then`` command bounded by ``max_hold_s`` (killed when it runs over) or a plain sleep of
    ``max_hold_s``. The locks are released however the hold ends: a failing or killed command,
    SIGTERM, or Ctrl-C.
    """
    marker = ws.state / QUIESCE_MARKER
    held_at = now_iso()
    started = time.monotonic()
    write_marker(marker, held_at, max_hold_s)
    locks: list[sqlite3.Connection] = []
    outcome: dict[str, Any] = {"then_exit": None}
    try:
        with _sigterm_raises_keyboard_interrupt():
            checkpoints = {}
            for path in databases:
                conn, report = checkpoint_and_lock(path)
                locks.append(conn)
                checkpoints[str(path)] = report
            flush_to_disk(databases)
            held = {"quiesced": [str(p) for p in databases], "checkpoints": checkpoints}
            print(json.dumps({**held, "held_at": held_at}), flush=True)
            deadline = started + max_hold_s
            if then:
                outcome.update(run_then(then, deadline))
            else:
                time.sleep(max(0.0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        outcome["interrupted"] = True
    except (sqlite3.Error, OSError, subprocess.SubprocessError) as error:
        return _fail(f"quiesce failed: {error}")
    finally:
        release_locks(locks)
        marker.unlink(missing_ok=True)
    ok = outcome["then_exit"] in (None, 0) and not outcome.get("interrupted")
    released = {"ok": ok, "released_at": now_iso(), "held_s": round(time.monotonic() - started, 3)}
    print(json.dumps({**released, **outcome}), flush=True)
    return 0 if ok else 1


def write_marker(marker: Path, held_at: str, max_hold_s: float) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    content = {"held_at": held_at, "max_hold_s": max_hold_s, "pid": os.getpid()}
    partial = marker.with_name(marker.name + ".tmp")
    partial.write_text(json.dumps(content) + "\n", encoding="utf-8")
    os.replace(partial, marker)


def checkpoint_and_lock(path: Path) -> tuple[sqlite3.Connection, dict[str, Any]]:
    """Move the WAL into the database file, then hold the write lock on a dedicated connection.

    SQLite refuses a checkpoint inside a transaction, so the checkpoint comes first; when a
    writer commits in the gap before the lock (the WAL is no longer empty) the lock is dropped
    and both are redone. The connection always returns holding the lock: what the last attempt
    or a checkpoint that could not finish (``busy``, a long reader) left in the WAL is reported,
    not retried. Those frames are committed and the lock freezes them, so a snapshot that takes
    the fsynced WAL with the file is still one consistent point.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    wal_bytes_before = _wal_bytes(path)
    try:
        conn.execute(f"PRAGMA busy_timeout = {migrate.BUSY_TIMEOUT_MS}")
        for attempt in range(1, CHECKPOINT_ATTEMPTS + 1):
            # TRUNCATE resets the WAL header before the frame counts are read, so the row's
            # counts are always 0; the WAL's size before and after says what happened.
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            wal_bytes = _wal_bytes(path)
            if busy or wal_bytes == 0 or attempt == CHECKPOINT_ATTEMPTS:
                break
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        conn.close()
        raise
    report = {"busy": bool(busy), "wal_bytes_before": wal_bytes_before, "wal_bytes": wal_bytes}
    return conn, report


def _wal_path(path: Path) -> Path:
    return path.with_name(path.name + "-wal")


def _wal_bytes(path: Path) -> int:
    wal = _wal_path(path)
    return wal.stat().st_size if wal.exists() else 0


# Run by ``flush_to_disk`` in a child process: fsync every path given.
FSYNC_SCRIPT = """
import os, sys
for path in sys.argv[1:]:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
"""


def flush_to_disk(databases: list[Path]) -> None:
    """fsync every database file, its WAL when there is one, and the directories holding them.

    A child process does the fsyncs on purpose: closing any descriptor of a file releases every
    POSIX lock this process holds on that file, so an fsync from here would silently drop the
    write lock just taken on a rollback-journal database (the WAL stores keep their locks in
    the ``-shm`` file and would not show the problem).
    """
    paths: list[Path] = []
    for path in databases:
        paths.append(path)
        if _wal_path(path).exists():
            paths.append(_wal_path(path))
        paths.append(path.parent)
    unique = [str(path) for path in dict.fromkeys(paths)]
    subprocess.run([sys.executable, "-c", FSYNC_SCRIPT, *unique], check=True)


def flush_databases(databases: list[Path]) -> dict[str, dict[str, Any]]:
    """Checkpoint every database into its main file, fsync it, then release: ``quiesce`` without
    the hold. ``research workspace sleep`` runs it last, when nothing writes any more.

    Per database: ``busy`` (a reader kept the checkpoint from finishing) and ``wal_bytes`` (the
    WAL left after it). ``busy`` false with ``wal_bytes`` 0 means the main file alone holds
    every committed write, and the fsync put it on disk.
    """
    locks: list[sqlite3.Connection] = []
    reports: dict[str, dict[str, Any]] = {}
    try:
        for path in databases:
            conn, report = checkpoint_and_lock(path)
            locks.append(conn)
            reports[str(path)] = {"busy": report["busy"], "wal_bytes": report["wal_bytes"]}
        flush_to_disk(databases)
    finally:
        release_locks(locks)
    return reports


def run_then(command: list[str], deadline: float) -> dict[str, Any]:
    """Run the snapshot command with the inherited environment; kill it at the deadline."""
    child = subprocess.Popen(command)
    try:
        child.wait(timeout=max(0.0, deadline - time.monotonic()))
        return {"then_exit": child.returncode}
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        return {"then_exit": child.returncode, "then_killed": "ran past --max-hold"}
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def release_locks(locks: list[sqlite3.Connection]) -> None:
    for conn in locks:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            conn.close()


class _sigterm_raises_keyboard_interrupt:
    """While active, SIGTERM raises ``KeyboardInterrupt`` so the ``finally`` releases the locks.

    Signal handlers can only be set from the main thread; elsewhere this does nothing.
    """

    def __enter__(self) -> None:
        self.previous = None
        if threading.current_thread() is threading.main_thread():
            self.previous = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    def __exit__(self, *exc_info: object) -> None:
        if self.previous is not None:
            signal.signal(signal.SIGTERM, self.previous)


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


# --- snapshot -------------------------------------------------------------------------------


def run_snapshot(args: argparse.Namespace) -> int:
    try:
        with Stores.open(args.coworld) as stores:
            store = stores.by_kind(args.store)
            print(json.dumps(snapshot(store, stores.coworld_id), ensure_ascii=False))
    except StoreError as error:
        return _fail(str(error), error.problems)
    return 0


def snapshot(store: Store, coworld_id: str | None = None) -> dict[str, Any]:
    """The store's records, effective settings and last ``seq``.

    A client that loads this and then reads ``events --after <seq>`` misses nothing: rows,
    settings and ``seq`` are read inside one read transaction, so a writer committing
    meanwhile moves either all of them or none of them.
    """
    with store.read_tx():
        existing = set(_copyable_tables(store.conn, "main"))
        records: dict[str, Any] = {}
        for spec in RECORDS.values():
            # Both directive kinds share a table name, so match the spec to this store too.
            if spec.store == store.kind and spec.table in existing:
                rows = store.q(f"SELECT * FROM {spec.table} ORDER BY rowid")
                records[spec.table] = [_record(spec, row) for row in rows]
        records["settings"] = settings.all(store)
        if store.kind == "workspace":
            records["admissions"] = admissions.snapshot(store)
        result: dict[str, Any] = {"store": store.kind}
        if store.kind == "coworld":
            result["coworld_id"] = coworld_id
        result["seq"] = store.last_seq()
        result["records"] = records
    return result


def _record(spec: RecordSpec, row: Any) -> dict[str, Any]:
    record = record_from_row(spec, row)
    for field in spec.stored_fields:
        if field.type == "bool" and record.get(field.name) is not None:
            record[field.name] = bool(record[field.name])
    return record


# --- events ---------------------------------------------------------------------------------


def run_events(args: argparse.Namespace) -> int:
    try:
        with Stores.open(args.coworld) as stores:
            store = stores.by_kind(args.store)
            return _print_events(store, args)
    except StoreError as error:
        return _fail(str(error), error.problems)
    except KeyboardInterrupt:
        return 0


def _print_events(store: Store, args: argparse.Namespace) -> int:
    """Rows after ``--after`` as JSON lines; ``--follow`` keeps polling every ``--interval``
    seconds until SIGINT or, for tests and scripts, until ``--max-rows`` rows were printed."""
    cursor = args.after
    printed = 0
    while True:
        for event in store.events_after(cursor, args.limit):
            print(
                json.dumps({"store": store.kind, **event.to_dict()}, ensure_ascii=False), flush=True
            )
            cursor = event.seq
            printed += 1
            if args.max_rows is not None and printed >= args.max_rows:
                return 0
        if not args.follow:
            return 0
        time.sleep(args.interval)


# --- the parser -----------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "store",
        help="User edits (apply), exports, snapshots and the event stream of the stores",
        description="The store side of the user-edit route and the seam (design A.10, A.11).",
    )
    commands = parser.add_subparsers(dest="store_command", metavar="<command>", required=True)

    apply_parser = commands.add_parser(
        "apply",
        help="Run one allowed library function as a user or harness actor",
        description=(
            "Ops: " + ", ".join(sorted(ALLOWED)) + ". The arguments are a JSON object of the"
            " function's keyword arguments; record ids may be given as G-3 or T-42, and a"
            " directive_id must be W-2 (workspace) or D-2 (Goal)."
        ),
    )
    apply_parser.add_argument(
        "--coworld", default=None, help="Explicit Coworld for user or harness edits"
    )
    apply_parser.add_argument("--actor", required=True, help="user:<id> or harness")
    apply_parser.add_argument(
        "--store",
        choices=STORE_KINDS,
        default=None,
        help="Which store the op writes; by default the op works it out",
    )
    apply_parser.add_argument("op", metavar="<op>")
    apply_parser.add_argument("args", metavar="<json-args>", nargs="?", default="{}")
    apply_parser.set_defaults(run=run_apply)

    export_parser = commands.add_parser(
        "export", help="Write every table of the workspace and Coworld stores as JSON lines"
    )
    export_parser.add_argument(
        "--coworld", default=None, help="A Coworld id; default: this session's binding"
    )
    export_parser.add_argument("--out", required=True, help="The output directory")
    export_parser.set_defaults(run=run_export)

    backup_parser = commands.add_parser(
        "backup",
        help="Copy every store and the profile's Hermes state.db into <dest-dir>/<timestamp>/",
        description=(
            "A database-aware backup: the workspace store, every registered Coworld's store"
            " and, with HERMES_HOME set, that profile's Hermes state.db, copied with SQLite's"
            " backup API, plus manifest.json (sources, sizes, sha256, the workspace store's"
            " last_seq). Hosting may run it as often as its loss window needs."
        ),
    )
    backup_parser.add_argument("dest", metavar="<dest-dir>", help="Where timestamped folders go")
    backup_parser.set_defaults(run=run_backup)

    restore_check_parser = commands.add_parser(
        "restore-check",
        help="Open every copy of a backup folder read-only and print a JSON verdict",
        description=(
            "The restore test: PRAGMA integrity_check, the manifest's sha256 and the schema"
            " status against this release for every copied database. Exit 1 when any fails."
        ),
    )
    restore_check_parser.add_argument(
        "backup_dir", metavar="<backup-dir>", help="One <dest-dir>/<timestamp>/ folder"
    )
    restore_check_parser.set_defaults(run=run_restore_check)

    quiesce_parser = commands.add_parser(
        "quiesce",
        help="Checkpoint, write-lock and fsync every database while a volume snapshot starts",
        description=(
            "Pauses writes to the workspace store, every registered Coworld store and, with"
            " HERMES_HOME set, that profile's Hermes state.db: PRAGMA wal_checkpoint(TRUNCATE),"
            " then a held write lock and an fsync of each file and its directory. Readers"
            " continue. Prints one JSON line when the hold is in place and one when it is"
            " released. With --then, runs that command (the snapshot initiation) during the"
            " hold and reports its exit code; otherwise holds for --max-hold seconds."
        ),
    )
    quiesce_parser.add_argument(
        "--max-hold",
        type=float,
        default=DEFAULT_MAX_HOLD_S,
        metavar="<seconds>",
        help=f"The longest the locks are held; a --then command still running is killed"
        f" (default {DEFAULT_MAX_HOLD_S:g})",
    )
    quiesce_parser.add_argument(
        "--then",
        nargs=argparse.REMAINDER,
        default=None,
        metavar="<command>",
        help="A command to run while the locks are held, with the inherited environment;"
        " everything after --then is the command",
    )
    quiesce_parser.set_defaults(run=run_quiesce)

    snapshot_parser = commands.add_parser(
        "snapshot", help="Print one store's records, settings and last seq as JSON"
    )
    snapshot_parser.add_argument(
        "--coworld", default=None, help="Coworld id; defaults to this session's binding"
    )
    snapshot_parser.add_argument("--store", choices=STORE_KINDS, default="coworld")
    snapshot_parser.set_defaults(run=run_snapshot)

    events_parser = commands.add_parser(
        "events", help="Print one store's events after a seq as JSON lines; --follow tails"
    )
    events_parser.add_argument(
        "--coworld", default=None, help="Coworld id; defaults to this session's binding"
    )
    events_parser.add_argument("--after", type=int, required=True, help="The last seq seen")
    events_parser.add_argument("--store", choices=STORE_KINDS, default="coworld")
    events_parser.add_argument("--limit", type=int, default=500, help="Rows per read")
    events_parser.add_argument("--follow", action="store_true", help="Keep polling until SIGINT")
    events_parser.add_argument("--interval", type=float, default=0.5, help="Poll seconds")
    events_parser.add_argument(
        "--max-rows", type=int, default=None, help="Exit after this many rows (for scripts)"
    )
    events_parser.set_defaults(run=run_events)
