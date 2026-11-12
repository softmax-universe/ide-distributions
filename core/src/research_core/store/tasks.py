"""Tasks: statuses, approval, leases with epochs, waits, messages, dependencies, checklists.

Design B.4 (the record and its rules), B.12 (completion, review, merge), B.13 (dispatch,
waits, recovery, messages), Figure 3 (the life of a Task); amendments A2.2 (goal-less
maintenance Tasks), A2.3 (epochs on the session row, ``require_holder``), A2.7 (outcomes),
A2.8 (approval and the user's word), A2.10 (``reject``).

Leases. ``claim`` takes the lease inside one ``BEGIN IMMEDIATE`` transaction and re-reads the
Task there, so of two concurrent claims exactly one wins. A lease is fresh for
``LEASE_FRESH_SECONDS`` after ``lease_renewed``. A Task stays with its holder, whatever the age of
the lease, until the holder releases it or the coordinator transfers it (``transfer_attempt``),
which raises ``lease_epoch`` and records the new epoch on the successor's ``task_sessions`` row.
Every Task-scoped write by a Researcher goes through ``require_holder``:
the caller must be the holding Session and its session row's epoch must equal the Task's, so a
replaced run cannot write (its late commits are never merged either: the merge uses
``completed_commit``, which only the current holder can record).

Events. Each record write emits one event named here: ``task.created``, ``task.proposed``,
``task.approved``, ``task.updated``, ``task.claimed``, ``task.released``, ``task.waiting``,
``task.wait_resolved``, ``task.asked``, ``task.answered``, ``task.message``,
``task.completed``, ``task.failed``, ``task.reopened``, ``task.reviewed``, ``task.abandoned``,
``task.merged`` and ``task.proposed_submission``. A review that reopens emits both
``task.reviewed`` and ``task.reopened``. Bookkeeping writes (``renew_lease``, ``set_run``,
``set_last_error``, ``mark_delivered``) emit none: they would only flood the stream.

Ids: every ``task_id``, ``goal_id``, ``experiment_id`` and ``version_id`` argument accepts the
integer or the display form (``T-3``, ``G-1``, ``X-2``, ``V-7``).
"""

from __future__ import annotations

import builtins
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from research_core import refs, task_types
from research_core.store import settings
from research_core.store._common import (
    display_id,
    fetch_row,
    json_field,
    parse_display_id,
    parse_iso,
    parse_json,
    require,
)
from research_core.store.errors import LeaseError, StoreError
from research_core.store.registry import (
    TASK,
    TASK_STATUSES,
    TASK_TYPES,
    WAIT_KINDS,
    record_from_row,
)

if TYPE_CHECKING:
    import sqlite3

    from research_core.store.actors import Actor
    from research_core.store.db import Store, Tx

LEASE_FRESH_SECONDS = 300
OPEN_STATUSES = ("proposed", "unclaimed", "in-progress")
PRIORITIES = ("normal", "high")
END_CONDITIONS = ("deliverable", "steps", "effort")
MESSAGE_DIRECTIONS = ("to_researcher", "to_orchestrator")
SESSION_KINDS = ("task", "change")
FAILURE_REQUIRED = ("what_failed", "next_step")
FAILURE_OPTIONAL = ("error", "exit_code", "tried")
UPDATABLE_FIELDS = (
    "title",
    "description",
    "reasoning",
    "for_human",
    "end_condition",
    "end_detail",
    "experiment_id",
    "priority",
    "ideation_round",
    "capability",
)

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_MERGE = re.compile(r"^(merged (?P<sha>[0-9a-f]{7,40})|not merged: \S.*|pending)$")

# ``list`` is a function of this module (the plan names it so); keep the builtin reachable.
_list = builtins.list


# --- creation and approval ------------------------------------------------------------------


def create(
    store: Store,
    actor: Actor,
    *,
    type: str,
    title: str,
    description: str,
    goal_id: int | str | None = None,
    reasoning: str = "",
    for_human: str = "",
    end_condition: str | None = None,
    end_detail: str | None = None,
    experiment_id: int | str | None = None,
    priority: str = "normal",
    depends_on: Sequence[int | str] = (),
    status: str = "unclaimed",
    ideation_round: int | None = None,
    capability: str | None = None,
    cites: str | None = None,
) -> dict[str, Any]:
    """Create a Task; status ``unclaimed`` (accepted) or ``proposed``.

    Rules: only a maintenance Task may have no Goal; only an improvement Task carries an
    Experiment; at most one open ideation Task per Goal; ``capability`` belongs to maintenance
    Tasks and ``ideation_round`` to ideation Tasks. An ``unclaimed`` Task is approved by its
    creator unless ``approval_required.task_types`` names the type and the actor is not the
    user and gives no ``cites`` (the user's message reference): then it is stored ``proposed``
    and the result's ``note`` says why (A2.8).
    """
    problems: list[str] = []
    require(
        problems, type in TASK_TYPES, f"type must be one of {', '.join(TASK_TYPES)}, not {type!r}"
    )
    require(problems, _text(title), "title is required")
    require(problems, _text(description), "description is required")
    require(problems, status in ("unclaimed", "proposed"), "status must be unclaimed or proposed")
    require(problems, priority in PRIORITIES, f"priority must be normal or high, not {priority!r}")
    problems.extend(_end_condition_problems(end_condition, end_detail))
    goal_n = None
    if goal_id is None:
        require(
            problems,
            type == "maintenance",
            "goal_id is required (only a maintenance Task may have no Goal)",
        )
    else:
        goal_n = _number(goal_id, "goal", "goal_id", problems)
        if goal_n is not None and not _exists(store, "goals", goal_n):
            problems.append(f"{display_id('G', goal_n)} does not exist")
    experiment_n = _experiment_number(store, type, experiment_id, problems)
    if capability is not None:
        require(problems, type == "maintenance", "capability belongs to a maintenance Task only")
    if ideation_round is not None:
        require(problems, type == "ideation", "ideation_round belongs to an ideation Task only")
        require(problems, _positive_int(ideation_round), "ideation_round must be a positive int")
    if type == "ideation" and goal_n is not None:
        open_ideation = store.one(
            "SELECT id FROM tasks WHERE goal_id = ? AND type = 'ideation' AND status IN (?, ?, ?)",
            (goal_n, *OPEN_STATUSES),
        )
        if open_ideation is not None:
            problems.append(
                f"{display_id('G', goal_n)} already has an open ideation Task "
                f"({display_id('T', open_ideation['id'])}); at most one is open per Goal"
            )
    deps = _dependency_numbers(store, depends_on, problems)
    if cites is not None:
        require(problems, _text(cites), "cites must be the reference of the user's message")
    if problems:
        raise StoreError(problems)

    with store.tx(actor) as tx:
        note = why_not = None
        approved_by = approved_at = proposed_by = None
        if status == "unclaimed":
            approved_by, why_not = _approval(store, actor, type, cites)
            if approved_by is None:
                status, proposed_by = "proposed", str(actor)
                note = f"stored as proposed: {why_not}"
            else:
                approved_at = tx.at
        else:
            proposed_by = str(actor)
        cursor = tx.execute(
            "INSERT INTO tasks(goal_id, type, title, description, reasoning, for_human,"
            " end_condition, end_detail, experiment_id, status, priority, proposed_by,"
            " approved_by, approved_at, ideation_round, capability, created, created_by, updated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                goal_n,
                type,
                title,
                description,
                reasoning,
                for_human,
                end_condition,
                end_detail,
                experiment_n,
                status,
                priority,
                proposed_by,
                approved_by,
                approved_at,
                ideation_round,
                capability,
                tx.at,
                str(actor),
                tx.at,
            ),
        )
        n = cursor.lastrowid
        for dep in deps:
            tx.execute("INSERT INTO task_deps(task_id, depends_on) VALUES (?, ?)", (n, dep))
        payload = {
            "type": type,
            "title": title,
            "goal": None if goal_n is None else display_id("G", goal_n),
            "status": status,
            "priority": priority,
            "approved_by": approved_by,
        }
        if why_not is not None:
            payload["reason"] = why_not
        tx.event("task.proposed" if status == "proposed" else "task.created", _t(n), payload)
        record = _record(store, _row(store, n))
    if note is not None:
        record["note"] = note
    return record


def propose(store: Store, actor: Actor, **fields: Any) -> dict[str, Any]:
    """``create`` with status ``proposed``: a Researcher's suggestion for the Orchestrator."""
    fields.pop("status", None)
    return create(store, actor, status="proposed", **fields)


def approve(
    store: Store, actor: Actor, task_id: int | str, *, cites: str | None = None
) -> dict[str, Any]:
    """Accept a proposed (or edited, hence unapproved) Task: it becomes ``unclaimed``.

    For a type in ``approval_required.task_types`` only the user approves, or a session that
    cites the user's request; ``approved_by`` then records both (A2.8).
    """
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        if row["status"] == "proposed" or (row["status"] == "unclaimed" and not row["approved_by"]):
            pass
        elif row["status"] == "unclaimed":
            problems.append(f"{_t(n)} is unclaimed and already approved by {row['approved_by']}")
        else:
            problems.append(f"{_t(n)} is {row['status']}; only a proposed Task can be approved")
        if cites is not None:
            require(problems, _text(cites), "cites must be the reference of the user's message")
        approved_by, why_not = _approval(store, actor, row["type"], cites)
        if approved_by is None:
            problems.append(why_not)
        if problems:
            raise StoreError(problems)
        tx.execute(
            "UPDATE tasks SET status = 'unclaimed', approved_by = ?, approved_at = ?, updated = ?"
            " WHERE id = ?",
            (approved_by, tx.at, tx.at, n),
        )
        tx.event(
            "task.approved",
            _t(n),
            {"approved_by": approved_by, "cites": cites, "type": row["type"]},
        )
        return _record(store, _row(store, n))


def update(
    store: Store, actor: Actor, task_id: int | str, *, cites: str | None = None, **fields: Any
) -> dict[str, Any]:
    """Edit a proposed or unclaimed Task. An edit clears the earlier approval (B.4).

    The editor's own acceptance replaces it under the ``create`` rule: an unclaimed Task stays
    approved unless its type needs the user's approval and the editor is a session without
    ``cites``; then ``approved_by`` is cleared, the Task is not dispatchable until ``approve``
    runs again, and the result's ``note`` says so. A proposed Task stays proposed.
    """
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        require(problems, fields, "no fields to update")
        require(
            problems,
            row["status"] in ("proposed", "unclaimed"),
            f"{_t(n)} is {row['status']}; a Task is edited only while proposed or unclaimed",
        )
        unknown = sorted(set(fields) - set(UPDATABLE_FIELDS))
        if unknown:
            problems.append(f"unknown fields: {', '.join(unknown)}")
        values = _field_values(
            store, row, {k: v for k, v in fields.items() if k not in unknown}, problems
        )
        if cites is not None:
            require(problems, _text(cites), "cites must be the reference of the user's message")
        if problems:
            raise StoreError(problems)
        note = None
        approved_by = approved_at = None
        if row["status"] == "unclaimed":
            approved_by, why_not = _approval(store, actor, row["type"], cites)
            if approved_by is None:
                note = f"approval cleared: {why_not}"
            else:
                approved_at = tx.at
        assignments = ", ".join(f"{name} = ?" for name in values)
        tx.execute(
            f"UPDATE tasks SET {assignments}, approved_by = ?, approved_at = ?, updated = ?"
            " WHERE id = ?",
            (*values.values(), approved_by, approved_at, tx.at, n),
        )
        tx.event(
            "task.updated",
            _t(n),
            {"fields": sorted(values), "approved_by": approved_by},
        )
        record = _record(store, _row(store, n))
    if note is not None:
        record["note"] = note
    return record


def link(
    store: Store, actor: Actor, task_id: int | str, depends_on: Sequence[int | str]
) -> dict[str, Any]:
    """Add dependencies: the Task starts only after they complete. Cycles are refused."""
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        require(
            problems,
            row["status"] in OPEN_STATUSES,
            f"{_t(n)} is {row['status']}; dependencies apply to open Tasks",
        )
        deps = _dependency_numbers(store, depends_on, problems)
        for dep in deps:
            if dep == n:
                problems.append(f"{_t(n)} cannot depend on itself")
            elif _depends_transitively(store, dep, n):
                problems.append(f"{_t(dep)} already depends on {_t(n)}: that would be a cycle")
        if problems:
            raise StoreError(problems)
        for dep in deps:
            tx.execute(
                "INSERT OR IGNORE INTO task_deps(task_id, depends_on) VALUES (?, ?)", (n, dep)
            )
        tx.event("task.updated", _t(n), {"depends_on": [_t(dep) for dep in deps]})
        return _record(store, _row(store, n))


# --- leases ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimRefused:
    problems: tuple[str, ...]
    lease: bool = False


def claim(
    store: Store,
    actor: Actor,
    task_id: int | str,
    *,
    run_id: str | None,
    worktree: str,
    branch: str,
    kind: str = "task",
) -> dict[str, Any]:
    """Claim the task, raising the existing typed store refusal for tool callers."""
    result = claim_result(
        store, actor, task_id, run_id=run_id, worktree=worktree, branch=branch, kind=kind
    )
    if isinstance(result, ClaimRefused):
        raise (LeaseError if result.lease else StoreError)(builtins.list(result.problems))
    return result


def claim_result(
    store: Store,
    actor: Actor,
    task_id: int | str,
    *,
    run_id: str | None,
    worktree: str,
    branch: str,
    kind: str = "task",
) -> dict[str, Any] | ClaimRefused:
    """Take the Task's lease: status ``in-progress``, ``held_by`` the session, epoch raised.

    Refused when the Task is not unclaimed, not approved, waits for a dependency, or is held by
    another Session (``LeaseError``). The holder itself may claim again (a resumed run) and only
    renews. ``kind`` is ``task`` for a
    Researcher's Task Session or ``change`` for an Orchestrator change (A3.6).
    """
    n = _task_number(task_id)
    problems: list[str] = []
    require(problems, actor.is_session, f"only a session can hold a Task; {actor} is not one")
    require(problems, _text(worktree), "worktree is required")
    require(problems, _text(branch), "branch is required")
    require(problems, kind in SESSION_KINDS, f"kind must be task or change, not {kind!r}")
    if problems:
        return ClaimRefused(tuple(problems))
    # BEGIN IMMEDIATE holds the write lock from here: the status check below is serialised
    # with every other claim, so of two concurrent claims one sees the other's result.
    with store.tx(actor) as tx:
        row = _row(store, n)
        same_holder = row["status"] == "in-progress" and row["held_by"] == actor.id
        lease_problem = False
        if same_holder:
            pass
        elif row["status"] == "in-progress":
            problems.append(
                f"{_t(n)} is held by session:{row['held_by']}; "
                "only that Session can work on it until the watcher releases it"
            )
            lease_problem = True
        else:
            require(
                problems, row["status"] == "unclaimed", f"{_t(n)} is {row['status']}, not unclaimed"
            )
        require(problems, row["approved_by"], f"{_t(n)} is not approved")
        blocking = _blocking_dependencies(store, n)
        if blocking:
            problems.append(f"{_t(n)} waits for {', '.join(blocking)} to complete")
        if problems:
            return ClaimRefused(tuple(problems), lease=lease_problem)
        epoch = row["lease_epoch"] if same_holder else row["lease_epoch"] + 1
        tx.execute(
            "UPDATE tasks SET status = 'in-progress', held_by = ?, lease_epoch = ?,"
            " lease_renewed = ?, run_id = ?, worktree = ?, branch = ?, updated = ? WHERE id = ?",
            (actor.id, epoch, tx.at, run_id, worktree, branch, tx.at, n),
        )
        # A Task naming an existing Experiment owns it from here, so only its holder records runs.
        tx.execute(
            "UPDATE experiments SET task_id = ? WHERE id = ? AND (task_id IS NULL OR task_id NOT IN"
            f" (SELECT id FROM tasks WHERE status IN {OPEN_STATUSES!r}))",
            (n, row["experiment_id"]),
        )
        tx.execute(
            "INSERT INTO task_sessions(task_id, session_id, kind, epoch, started)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id, session_id) DO UPDATE SET"
            " epoch = excluded.epoch, kind = excluded.kind",
            (n, actor.id, kind, epoch, tx.at),
        )
        tx.event(
            "task.claimed",
            _t(n),
            {
                "session": actor.id,
                "epoch": epoch,
                "run_id": run_id,
                "worktree": worktree,
                "branch": branch,
                "kind": kind,
                "took_over_from": None if same_holder else row["held_by"],
            },
        )
        return _record(store, _row(store, n))


@dataclass(frozen=True)
class TransferRefused:
    problems: list[str]


def transfer_attempt(
    store: Store, actor: Actor, task_id: int | str, *, previous: str, successor: str
) -> dict[str, Any] | TransferRefused:
    """Coordinator-only holder transfer after exact predecessor exclusion; replay is harmless.

    Keep task progress, waits, branch and worktree. Historical holders retain their epoch.
    """
    require_problems = []
    require(require_problems, actor.is_harness, "only the coordinator transfers attempts")
    if require_problems:
        raise StoreError(require_problems)
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        if row["held_by"] == successor:
            return _record(store, row)
        if row["status"] != "in-progress" or row["held_by"] != previous:
            return TransferRefused([f"{_t(n)} no longer belongs to predecessor {previous}"])
        epoch = row["lease_epoch"] + 1
        tx.execute(
            "UPDATE tasks SET held_by=?,lease_epoch=?,run_id=NULL,lease_renewed=?,updated=?"
            " WHERE id=?",
            (successor, epoch, tx.at, tx.at, n),
        )
        tx.execute(
            "INSERT INTO task_sessions(task_id,session_id,kind,epoch,started) "
            "VALUES (?,?,'task',?,?)",
            (n, successor, epoch, tx.at),
        )
        tx.event(
            "task.attempt_transferred",
            _t(n),
            {"previous": previous, "successor": successor, "epoch": epoch},
        )
        return _record(store, _row(store, n))


def renew_lease(store: Store, actor: Actor, task_id: int | str) -> dict[str, Any]:
    """The holder keeps its lease fresh. No event."""
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        _check_holder(store, row, actor)
        tx.execute(
            "UPDATE tasks SET lease_renewed = ?, updated = ? WHERE id = ?", (tx.at, tx.at, n)
        )
        return _record(store, _row(store, n))


def require_holder(store: Store, task_id: int | str, actor: Actor) -> None:
    """``LeaseError`` unless ``actor`` is the holding Session at the Task's current epoch.

    The epoch comes from the Session's ``task_sessions`` row (``claim`` writes it); an
    ``Actor.epoch`` overrides it for tests (A2.3).
    """
    _check_holder(store, _row(store, _task_number(task_id)), actor)


def release(store: Store, actor: Actor, task_id: int | str, *, reason: str) -> dict[str, Any]:
    """The holder gives the Task back: ``unclaimed``, lease cleared, wait cleared."""
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        _check_holder(store, row, actor)
        problems: list[str] = []
        require(problems, _text(reason), "reason is required")
        require(
            problems, row["status"] == "in-progress", f"{_t(n)} is {row['status']}, not in-progress"
        )
        if problems:
            raise StoreError(problems)
        tx.execute(
            "UPDATE tasks SET status = 'unclaimed', held_by = NULL, run_id = NULL,"
            " lease_renewed = NULL, waiting_on = NULL, updated = ? WHERE id = ?",
            (tx.at, n),
        )
        tx.event("task.released", _t(n), {"reason": reason, "session": actor.id})
        return _record(store, _row(store, n))


# --- waits and messages ---------------------------------------------------------------------


def wait(
    store: Store, actor: Actor, task_id: int | str, *, kind: str, detail: str, ref: Any = None
) -> dict[str, Any]:
    """The holder records what the Task waits on; its run then ends (B.13).

    A later ``wait`` replaces the earlier one: a Task waits on one thing at a time.
    """
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        _check_holder(store, row, actor)
        problems: list[str] = []
        require(
            problems,
            kind in WAIT_KINDS,
            f"kind must be one of {', '.join(WAIT_KINDS)}, not {kind!r}",
        )
        require(problems, _text(detail), "detail is required: what the Task waits for")
        require(
            problems, row["status"] == "in-progress", f"{_t(n)} is {row['status']}, not in-progress"
        )
        if kind == "dependency":
            require(
                problems,
                _names_a_dependency(store, n, ref),
                "a dependency wait names the Task it waits on: ref T-n",
            )
        if problems:
            raise StoreError(problems)
        waiting_on = {"kind": kind, "detail": detail, "ref": ref, "since": tx.at}
        tx.execute(
            "UPDATE tasks SET waiting_on = ?, updated = ? WHERE id = ?",
            (json_field(waiting_on), tx.at, n),
        )
        tx.event("task.waiting", _t(n), {"kind": kind, "detail": detail, "ref": ref})
        return _record(store, _row(store, n))


def _names_a_dependency(store: Store, n: int, ref: Any) -> bool:
    """A wait on nothing would resolve on the next tick and restart the Task for no reason."""
    if ref is None:
        return store.one("SELECT 1 FROM task_deps WHERE task_id = ?", (n,)) is not None
    numbers: list[str] = []
    number = _number(ref, "task", "ref", numbers)
    return (
        number not in (None, n)
        and store.one("SELECT 1 FROM tasks WHERE id = ?", (number,)) is not None
    )


def resolve_wait(store: Store, actor: Actor, task_id: int | str, *, note: str) -> dict[str, Any]:
    """Clear ``waiting_on`` (the watcher or the Orchestrator); the holder's Session resumes."""
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        require(problems, _text(note), "note is required: what resolved the wait")
        waiting_on = parse_json(row["waiting_on"])
        require(problems, waiting_on, f"{_t(n)} is not waiting")
        if problems:
            raise StoreError(problems)
        tx.execute("UPDATE tasks SET waiting_on = NULL, updated = ? WHERE id = ?", (tx.at, n))
        tx.event(
            "task.wait_resolved",
            _t(n),
            {"kind": waiting_on["kind"], "ref": waiting_on.get("ref"), "note": note},
        )
        return _record(store, _row(store, n))


def ask(store: Store, actor: Actor, task_id: int | str, *, question: str) -> dict[str, Any]:
    """The holder asks the Orchestrator: a question message plus ``waiting_on: question``."""
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        _check_holder(store, row, actor)
        problems: list[str] = []
        require(problems, _text(question), "question is required")
        require(
            problems, row["status"] == "in-progress", f"{_t(n)} is {row['status']}, not in-progress"
        )
        if problems:
            raise StoreError(problems)
        message_id = _insert_message(tx, n, "to_orchestrator", "question", question)
        waiting_on = {"kind": "question", "detail": question, "ref": message_id, "since": tx.at}
        tx.execute(
            "UPDATE tasks SET waiting_on = ?, updated = ? WHERE id = ?",
            (json_field(waiting_on), tx.at, n),
        )
        tx.event("task.asked", _t(n), {"message_id": message_id, "question": question})
        return _record(store, _row(store, n))


def answer(
    store: Store,
    actor: Actor,
    task_id: int | str,
    *,
    text: str,
    question_id: int | None = None,
) -> dict[str, Any]:
    """Answer the Task's open question (the latest one unless ``question_id`` says which).

    Resolves the ``question`` wait when it is for that question.
    """
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        require(problems, _text(text), "text is required")
        question = _question_row(tx, n, question_id, problems)
        if problems:
            raise StoreError(problems)
        message_id = _insert_message(tx, n, "to_researcher", "answer", text)
        waiting_on = parse_json(row["waiting_on"])
        resolved = (
            waiting_on is not None
            and waiting_on.get("kind") == "question"
            and waiting_on.get("ref") == question["id"]
        )
        if resolved:
            tx.execute("UPDATE tasks SET waiting_on = NULL, updated = ? WHERE id = ?", (tx.at, n))
        else:
            tx.execute("UPDATE tasks SET updated = ? WHERE id = ?", (tx.at, n))
        tx.event("task.answered", _t(n), {"message_id": message_id, "question_id": question["id"]})
        return _record(store, _row(store, n))


def message(
    store: Store, actor: Actor, task_id: int | str, *, text: str, direction: str
) -> dict[str, Any]:
    """A note between the Orchestrator and the Researcher; delivered by steer or on resume.

    Only the holder writes ``to_orchestrator``: that direction is the Researcher's (A2.3).
    """
    n = _task_number(task_id)
    problems: list[str] = []
    require(
        problems,
        direction in MESSAGE_DIRECTIONS,
        f"direction must be to_researcher or to_orchestrator, not {direction!r}",
    )
    require(problems, _text(text), "text is required")
    if problems:
        raise StoreError(problems)
    with store.tx(actor) as tx:
        row = _row(store, n)
        if direction == "to_orchestrator":
            _check_holder(store, row, actor)
        message_id = _insert_message(tx, n, direction, "message", text)
        tx.execute("UPDATE tasks SET updated = ? WHERE id = ?", (tx.at, n))
        tx.event("task.message", _t(n), {"message_id": message_id, "direction": direction})
        return _record(store, _row(store, n))


def pending_messages(store: Store, task_id: int | str, direction: str) -> list[dict[str, Any]]:
    """Undelivered messages in ``direction``, oldest first."""
    n = _task_number(task_id)
    if direction not in MESSAGE_DIRECTIONS:
        raise StoreError([f"direction must be to_researcher or to_orchestrator, not {direction!r}"])
    _row(store, n)
    rows = store.q(
        "SELECT id, task_id, direction, kind, text, created, created_by, delivered"
        " FROM task_messages WHERE task_id = ? AND direction = ? AND delivered IS NULL"
        " ORDER BY id",
        (n, direction),
    )
    return [dict(row) for row in rows]


def mark_delivered(store: Store, actor: Actor, message_ids: Sequence[int]) -> int:
    """Stamp ``delivered`` on the messages; returns how many were newly delivered. No event."""
    ids = [int(message_id) for message_id in message_ids]
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    with store.tx(actor) as tx:
        cursor = tx.execute(
            "UPDATE task_messages SET delivered = ? WHERE delivered IS NULL"
            f" AND id IN ({placeholders})",
            (tx.at, *ids),
        )
        return cursor.rowcount


# --- completion, review, abandonment -------------------------------------------------------


def complete(
    store: Store,
    actor: Actor,
    task_id: int | str,
    *,
    checklist: Mapping[str, Any] | None = None,
    commit: str | None = None,
    merge_request: str | None = None,
    merge_reason: str = "",
    submission_candidate: str | None = None,
    result: str | None = None,
    outcome: str = "succeeded",
    failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The holder finishes the Task (B.12). ``outcome`` is ``succeeded`` or ``failed`` (A2.7).

    Succeeded: ``checklist`` must satisfy the type's items (``task_types.check``) and every
    record it names must exist (claims, the Experiment, Versions, Tasks this Task proposed for
    its Goal, whatever has been decided about them); ``commit`` and ``submission_candidate`` are
    folded into the checklist;
    ``merge_request`` ``yes`` needs the commit, ``no`` needs ``merge_reason``; ``result`` says
    what came of it. ``completed_commit`` is what the merge uses, never the branch tip. The
    lease stays with the holder so a review that reopens continues the Task; emits
    ``task.completed``, the review wake's trigger. Failed: delegates to ``fail``.
    """
    n = _task_number(task_id)
    if outcome == "failed":
        if failure is None:
            raise StoreError(["outcome failed needs failure {what_failed, next_step, ...}"])
        return fail(store, actor, n, failure=failure, commit=commit, result=result)
    with store.tx(actor) as tx:
        row = _row(store, n)
        _check_holder(store, row, actor)
        problems, accepted, experiment_n = _completion_problems(
            store,
            row,
            checklist=checklist,
            commit=commit,
            merge_request=merge_request,
            merge_reason=merge_reason,
            submission_candidate=submission_candidate,
            result=result,
            outcome=outcome,
            failure=failure,
        )
        if problems:
            raise StoreError(problems)
        accepted["merge_request"] = merge_request
        accepted["merge_reason"] = merge_reason
        tx.execute(
            "UPDATE tasks SET status = 'complete', outcome = 'succeeded', result = ?,"
            " checklist = ?, completed_commit = ?, experiment_id = COALESCE(experiment_id, ?),"
            " waiting_on = NULL, merge = NULL, failure = NULL, updated = ? WHERE id = ?",
            (result, json_field(accepted), commit, experiment_n, tx.at, n),
        )
        tx.event(
            "task.completed",
            _t(n),
            {
                "outcome": "succeeded",
                "commit": commit,
                "merge_request": merge_request,
                "merge_reason": merge_reason,
                "submission_candidate": accepted.get("submission_candidate"),
                "result": result,
                "session": actor.id,
            },
        )
        return _record(store, _row(store, n))


def fail(
    store: Store,
    actor: Actor,
    task_id: int | str,
    *,
    failure: Mapping[str, Any],
    commit: str | None = None,
    result: str | None = None,
) -> dict[str, Any]:
    """The holder records that the Task failed: ``complete`` with outcome ``failed`` (A2.7).

    ``failure`` needs ``what_failed`` and ``next_step``; ``error``, ``exit_code`` and ``tried``
    are optional. The checklist is skipped. Emits ``task.failed``.
    """
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        _check_holder(store, row, actor)
        problems = _fail_problems(row, failure, commit)
        if problems:
            raise StoreError(problems)
        tx.execute(
            "UPDATE tasks SET status = 'complete', outcome = 'failed', failure = ?, result = ?,"
            " completed_commit = ?, waiting_on = NULL, updated = ? WHERE id = ?",
            (json_field(dict(failure)), result, commit, tx.at, n),
        )
        tx.event(
            "task.failed",
            _t(n),
            {"failure": dict(failure), "commit": commit, "session": actor.id},
        )
        return _record(store, _row(store, n))


def reopen(store: Store, actor: Actor, task_id: int | str, *, reason: str) -> dict[str, Any]:
    """A complete Task goes back to ``in-progress``; the same holder resumes with the reason.

    The reason is also a ``to_researcher`` message, so it reaches the Session on resume.
    """
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        require(problems, _text(reason), "reason is required")
        require(problems, row["status"] == "complete", f"{_t(n)} is {row['status']}, not complete")
        if problems:
            raise StoreError(problems)
        _reopen(tx, n, reason)
        tx.event("task.reopened", _t(n), {"reason": reason, "by": str(actor)})
        return _record(store, _row(store, n))


def review(
    store: Store, actor: Actor, task_id: int | str, *, accept: bool, merge: str, reason: str
) -> dict[str, Any]:
    """The review wake's verdict on a succeeded Task (B.12).

    Accept with ``merge`` ``yes`` sets ``merge`` to ``pending`` (the watcher merges and calls
    ``set_merge``); ``no`` records ``not merged: <reason>``. Rejecting reopens the Task with
    the reason: events ``task.reviewed`` then ``task.reopened``.
    """
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        require(problems, isinstance(accept, bool), "accept must be true or false")
        if accept:
            require(problems, merge in ("yes", "no"), "merge must be yes or no when accepting")
        require(problems, _text(reason), "reason is required")
        if row["status"] != "complete":
            problems.append(f"{_t(n)} is {row['status']}, not complete")
        elif row["outcome"] != "succeeded":
            problems.append(f"{_t(n)} {row['outcome']}; a review judges a succeeded Task")
        if problems:
            raise StoreError(problems)
        if accept:
            merge_text = "pending" if merge == "yes" else f"not merged: {reason}"
            tx.execute(
                "UPDATE tasks SET merge = ?, updated = ? WHERE id = ?", (merge_text, tx.at, n)
            )
            tx.event("task.reviewed", _t(n), {"accept": True, "merge": merge, "reason": reason})
        else:
            tx.event("task.reviewed", _t(n), {"accept": False, "merge": merge, "reason": reason})
            _reopen(tx, n, reason)
            tx.event("task.reopened", _t(n), {"reason": reason, "by": "review"})
        return _record(store, _row(store, n))


def abandon(
    store: Store, actor: Actor, task_id: int | str, *, failure: Mapping[str, Any]
) -> dict[str, Any]:
    """Drop an open Task with a ``failure`` record; the checklist is skipped (B.4)."""
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        require(
            problems,
            row["status"] in OPEN_STATUSES,
            f"{_t(n)} is {row['status']}; only an open Task can be abandoned",
        )
        problems.extend(_failure_problems(failure))
        if problems:
            raise StoreError(problems)
        _abandon(tx, row, dict(failure), {})
        return _record(store, _row(store, n))


def reject(store: Store, actor: Actor, task_id: int | str, *, next_step: str) -> dict[str, Any]:
    """Turn down a proposed Task: abandoned with ``what_failed: rejected by ...`` (A2.10)."""
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        require(problems, row["status"] == "proposed", f"{_t(n)} is {row['status']}, not proposed")
        require(problems, _text(next_step), "next_step is required: what to do instead")
        if problems:
            raise StoreError(problems)
        who = "the user" if actor.is_user else str(actor)
        failure = {"what_failed": f"rejected by {who}", "next_step": next_step}
        _abandon(tx, row, failure, {"rejected": True})
        return _record(store, _row(store, n))


def propose_submission(
    store: Store, actor: Actor, task_id: int | str, *, version_id: int | str, reason: str
) -> dict[str, Any]:
    """The holder proposes a Version for submission; a management wake applies the rules (H).

    The proposal is a ``to_orchestrator`` message and the ``task.proposed_submission`` event.
    """
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        _check_holder(store, row, actor)
        problems: list[str] = []
        version_n = _number(version_id, "version", "version_id", problems)
        if version_n is not None and not _exists(store, "versions", version_n):
            problems.append(f"{display_id('V', version_n)} does not exist")
        require(problems, _text(reason), "reason is required")
        if problems:
            raise StoreError(problems)
        version = display_id("V", version_n)
        message_id = _insert_message(
            tx, n, "to_orchestrator", "message", f"Proposes {version} for submission: {reason}"
        )
        tx.execute("UPDATE tasks SET updated = ? WHERE id = ?", (tx.at, n))
        tx.event(
            "task.proposed_submission",
            _t(n),
            {"version": version, "reason": reason, "message_id": message_id, "session": actor.id},
        )
        return _record(store, _row(store, n))


# --- bookkeeping ----------------------------------------------------------------------------


def set_run(store: Store, actor: Actor, task_id: int | str, run_id: str | None) -> dict[str, Any]:
    """The watcher records the holder's current Hermes run. No event."""
    n = _task_number(task_id)
    if run_id is not None and not _text(run_id):
        raise StoreError(["run_id must be text or null"])
    with store.tx(actor) as tx:
        _row(store, n)
        tx.execute("UPDATE tasks SET run_id = ?, updated = ? WHERE id = ?", (run_id, tx.at, n))
        return _record(store, _row(store, n))


def set_merge(store: Store, actor: Actor, task_id: int | str, merge: str) -> dict[str, Any]:
    """Record the merge result: ``merged <sha>``, ``not merged: <reason>`` or ``pending``.

    Emits ``task.merged`` when the branch was merged; a failed merge is the watcher's
    ``merge.failed`` event.
    """
    n = _task_number(task_id)
    match = _MERGE.match(merge) if isinstance(merge, str) else None
    with store.tx(actor) as tx:
        row = _row(store, n)
        problems: list[str] = []
        require(
            problems, match, "merge must be 'merged <sha>', 'not merged: <reason>' or 'pending'"
        )
        require(problems, row["status"] == "complete", f"{_t(n)} is {row['status']}, not complete")
        if problems:
            raise StoreError(problems)
        tx.execute("UPDATE tasks SET merge = ?, updated = ? WHERE id = ?", (merge, tx.at, n))
        if match.group("sha"):
            tx.event("task.merged", _t(n), {"commit": match.group("sha"), "merge": merge})
        return _record(store, _row(store, n))


def set_last_error(
    store: Store, actor: Actor, task_id: int | str, error: Mapping[str, Any]
) -> dict[str, Any]:
    """The holder's hook keeps its last failed tool call on the Task. No event."""
    n = _task_number(task_id)
    with store.tx(actor) as tx:
        row = _row(store, n)
        _check_holder(store, row, actor)
        if not isinstance(error, Mapping):
            raise StoreError(["error must be an object (command, exit code, error tail)"])
        tx.execute(
            "UPDATE tasks SET last_error_seen = ?, updated = ? WHERE id = ?",
            (json_field(dict(error)), tx.at, n),
        )
        return _record(store, _row(store, n))


# --- reads ----------------------------------------------------------------------------------


def read(store: Store, task_id: int | str, *, live_run: bool = False) -> dict[str, Any]:
    """The Task with its dependencies, sessions, messages and the computed ``stuck``.

    ``live_run`` is what the watcher knows about the holder's run; a Task is stuck when it is
    in progress, waits on nothing, its lease is stale and no run is live (B.4).
    """
    return _record(store, _row(store, _task_number(task_id)), live_run=live_run)


def note_seen(
    store: Store, actor: Actor, session_id: str, records: Sequence[Mapping[str, Any]]
) -> None:
    """Record the Task revisions a Session was shown, for :func:`revision_for_assignment`.

    Most reads show nothing new, and those must not wait for the store's write lock.
    """
    seen = {
        row["task_id"]: row["updated"]
        for row in store.q(
            "SELECT task_id,updated FROM task_views WHERE session_id=?", (session_id,)
        )
    }
    views = {_task_number(record["id"]): record["updated"] for record in records}
    changed = {
        task_id: updated for task_id, updated in views.items() if seen.get(task_id) != updated
    }
    if not changed:
        return
    with store.tx(actor) as tx:
        for task_id, updated in changed.items():
            tx.execute(
                "INSERT INTO task_views(session_id,task_id,updated) VALUES (?,?,?)"
                " ON CONFLICT(session_id,task_id) DO UPDATE SET updated=excluded.updated",
                (session_id, task_id, updated),
            )


def revision_for_assignment(store: Store, session_id: str, task_id: int) -> str:
    """The revision an assignment must match: what this Session last saw of the Task. A Session
    that has seen none chose from elsewhere (its briefing), so there is no older view to protect
    and the current revision stands."""
    seen = store.one(
        "SELECT updated FROM task_views WHERE session_id=? AND task_id=?", (session_id, task_id)
    )
    return seen["updated"] if seen is not None else read(store, task_id)["updated"]


def list(
    store: Store,
    *,
    status: str | None = None,
    goal_id: int | str | None = None,
    type: str | None = None,
    held_by: str | None = None,
) -> list[dict[str, Any]]:
    """Tasks matching every given filter, oldest first."""
    problems: list[str] = []
    clauses = []
    params: _list[Any] = []
    if status is not None:
        require(
            problems, status in TASK_STATUSES, f"status must be one of {', '.join(TASK_STATUSES)}"
        )
        clauses.append("status = ?")
        params.append(status)
    if goal_id is not None:
        goal_n = _number(goal_id, "goal", "goal_id", problems)
        clauses.append("goal_id = ?")
        params.append(goal_n)
    if type is not None:
        require(problems, type in TASK_TYPES, f"type must be one of {', '.join(TASK_TYPES)}")
        clauses.append("type = ?")
        params.append(type)
    if held_by is not None:
        clauses.append("held_by = ?")
        params.append(held_by)
    if problems:
        raise StoreError(problems)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = store.q(f"SELECT * FROM tasks{where} ORDER BY created, id", params)
    return [_record(store, row) for row in rows]


def dispatchable(store: Store) -> list[dict[str, Any]]:
    """Unclaimed, approved Tasks whose dependencies are complete: high priority first, then
    oldest first. The watcher starts runs for these (B.13)."""
    rows = store.q(
        "SELECT t.* FROM tasks t WHERE t.status = 'unclaimed' AND t.approved_by IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks dep ON dep.id = d.depends_on"
        "   WHERE d.task_id = t.id AND (dep.status <> 'complete' OR dep.outcome = 'failed'))"
        " ORDER BY CASE t.priority WHEN 'high' THEN 0 ELSE 1 END, t.created, t.id"
    )
    return [_record(store, row) for row in rows]


# --- internals ------------------------------------------------------------------------------


def _t(n: int) -> str:
    return display_id("T", n)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _exists(store: Store, table: str, n: int) -> bool:
    return store.one(f"SELECT 1 FROM {table} WHERE id = ?", (n,)) is not None


def _row(store: Store, n: int) -> sqlite3.Row:
    return fetch_row(store, "tasks", n, display=_t(n))


def _task_number(value: Any) -> int:
    problems: list[str] = []
    n = _number(value, "task", "task_id", problems)
    if n is None:
        raise StoreError([f"not a Task id: {value!r} (an integer or T-<n>)"])
    return n


def _number(value: Any, kind: str, label: str, problems: list[str]) -> int | None:
    """An id from an integer or a display id of ``kind``; records a problem otherwise."""
    if _positive_int(value):
        return value
    if isinstance(value, str):
        try:
            found_kind, n = parse_display_id(value)
        except ValueError:
            pass
        else:
            if found_kind == kind:
                return n
            problems.append(f"{label}: {value} is a {found_kind} id, not a {kind} id")
            return None
    problems.append(f"{label}: expected a {kind} id, got {value!r}")
    return None


def _record(store: Store, row: sqlite3.Row, *, live_run: bool = False) -> dict[str, Any]:
    record = record_from_row(TASK, row)
    n = row["id"]
    record["depends_on"] = [
        _t(dep["depends_on"])
        for dep in store.q(
            "SELECT depends_on FROM task_deps WHERE task_id = ? ORDER BY depends_on", (n,)
        )
    ]
    record["sessions"] = [
        dict(session)
        for session in store.q(
            "SELECT session_id, kind, epoch, started FROM task_sessions WHERE task_id = ?"
            " ORDER BY started, session_id",
            (n,),
        )
    ]
    record["messages"] = [
        dict(message)
        for message in store.q(
            "SELECT id, direction, kind, text, created, created_by, delivered FROM task_messages"
            " WHERE task_id = ? ORDER BY id",
            (n,),
        )
    ]
    record["stuck"] = (
        row["status"] == "in-progress"
        and not row["waiting_on"]
        and not live_run
        and not _lease_fresh(store, row)
    )
    return record


def _lease_fresh(store: Store, row: sqlite3.Row) -> bool:
    if not row["lease_renewed"]:
        return False
    age = parse_iso(store.now()) - parse_iso(row["lease_renewed"])
    return age.total_seconds() < LEASE_FRESH_SECONDS


def _check_holder(store: Store, row: sqlite3.Row, actor: Actor) -> None:
    n = row["id"]
    if not actor.is_session:
        raise LeaseError([f"{_t(n)} is written by its holding session; {actor} is not a session"])
    if row["held_by"] != actor.id:
        held = f"held by session:{row['held_by']}" if row["held_by"] else "not held"
        raise LeaseError([f"{_t(n)} is {held}; {actor} does not hold it"])
    epoch = actor.epoch
    if epoch is None:
        session = store.one(
            "SELECT epoch FROM task_sessions WHERE task_id = ? AND session_id = ?", (n, actor.id)
        )
        epoch = None if session is None else session["epoch"]
    if epoch != row["lease_epoch"]:
        raise LeaseError(
            [
                f"{_t(n)} is at lease epoch {row['lease_epoch']}; {actor} holds epoch {epoch}"
                " (its run was replaced)"
            ]
        )


def _approval(
    store: Store, actor: Actor, type: str, cites: str | None
) -> tuple[str | None, str | None]:
    """``(approved_by, None)`` when ``actor`` may accept a ``type`` Task; else ``(None, why)``."""
    gated = type in settings.get(store, "approval_required.task_types")
    if actor.is_user or not gated:
        return str(actor), None
    if cites and not refs.names_only_records(cites):
        return f"user via {actor} citing {cites}", None
    return None, (
        f"approval_required.task_types names {type}: only the user approves a {type} Task;"
        " a user request in the conversation overrides it (pass cites with the message reference)"
    )


def _end_condition_problems(end_condition: Any, end_detail: Any) -> list[str]:
    problems: list[str] = []
    if end_condition is None:
        if end_detail:
            problems.append("end_detail needs an end_condition")
        return problems
    require(
        problems,
        end_condition in END_CONDITIONS,
        f"end_condition must be one of {', '.join(END_CONDITIONS)}, not {end_condition!r}",
    )
    require(problems, _text(end_detail), "end_detail is required with an end_condition")
    return problems


def _experiment_number(
    store: Store, type: str, experiment_id: Any, problems: list[str]
) -> int | None:
    if experiment_id is None:
        return None
    require(problems, type == "improvement", "only an improvement Task carries an Experiment")
    n = _number(experiment_id, "experiment", "experiment_id", problems)
    if n is not None and not _exists(store, "experiments", n):
        problems.append(f"{display_id('X', n)} does not exist")
    return n


def _dependency_numbers(store: Store, depends_on: Any, problems: list[str]) -> _list[int]:
    if not isinstance(depends_on, _list | tuple):
        problems.append("depends_on must be a list of Task ids")
        return []
    deps: _list[int] = []
    for value in depends_on:
        n = _number(value, "task", "depends_on", problems)
        if n is None:
            continue
        if not _exists(store, "tasks", n):
            problems.append(f"{_t(n)} does not exist")
        elif n not in deps:
            deps.append(n)
    return deps


def _depends_transitively(store: Store, start: int, target: int) -> bool:
    """Whether ``start`` depends, through any chain, on ``target``."""
    seen = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for dep in store.q("SELECT depends_on FROM task_deps WHERE task_id = ?", (current,)):
            n = dep["depends_on"]
            if n == target:
                return True
            if n not in seen:
                seen.add(n)
                frontier.append(n)
    return False


def _blocking_dependencies(store: Store, n: int) -> _list[str]:
    rows = store.q(
        "SELECT dep.id FROM task_deps d JOIN tasks dep ON dep.id = d.depends_on"
        " WHERE d.task_id = ? AND (dep.status <> 'complete' OR dep.outcome = 'failed')"
        " ORDER BY dep.id",
        (n,),
    )
    return [_t(row["id"]) for row in rows]


def _field_values(
    store: Store, row: sqlite3.Row, fields: Mapping[str, Any], problems: list[str]
) -> dict[str, Any]:
    """The column values of an ``update``, validated against the Task's type."""
    values: dict[str, Any] = {}
    for name, value in fields.items():
        if name in ("end_condition", "end_detail"):
            continue  # checked as a pair below
        if name in ("title", "description"):
            ok = require(problems, _text(value), f"{name} is required")
        elif name in ("reasoning", "for_human"):
            ok = require(problems, isinstance(value, str), f"{name} must be text")
        elif name == "priority":
            ok = require(
                problems, value in PRIORITIES, f"priority must be normal or high, not {value!r}"
            )
        elif name == "experiment_id":
            value = _experiment_number(store, row["type"], value, problems)
            ok = True
        elif name == "capability":
            ok = require(
                problems,
                value is None or row["type"] == "maintenance",
                "capability belongs to a maintenance Task only",
            )
        else:  # ideation_round
            ok = require(
                problems,
                value is None or (row["type"] == "ideation" and _positive_int(value)),
                "ideation_round is a positive int on an ideation Task",
            )
        if ok:
            values[name] = value
    if "end_condition" in fields or "end_detail" in fields:
        end_condition = fields.get("end_condition", row["end_condition"])
        end_detail = fields.get("end_detail", row["end_detail"])
        pair_problems = _end_condition_problems(end_condition, end_detail)
        problems.extend(pair_problems)
        if not pair_problems:
            values["end_condition"] = end_condition
            values["end_detail"] = end_detail
    return values


def _insert_message(tx: Tx, n: int, direction: str, kind: str, text: str) -> int:
    cursor = tx.execute(
        "INSERT INTO task_messages(task_id, direction, kind, text, created, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (n, direction, kind, text, tx.at, str(tx.actor)),
    )
    return cursor.lastrowid


def _question_row(
    tx: Tx, n: int, question_id: int | None, problems: list[str]
) -> sqlite3.Row | None:
    if question_id is None:
        # The open question: the latest one asked after the latest answer.
        row = tx.one(
            "SELECT * FROM task_messages WHERE task_id = ? AND kind = 'question' AND id >"
            " COALESCE((SELECT MAX(id) FROM task_messages WHERE task_id = ? AND kind = 'answer'),"
            " 0) ORDER BY id DESC LIMIT 1",
            (n, n),
        )
        if row is None:
            problems.append(f"{_t(n)} has no open question; use message for a note")
        return row
    row = tx.one("SELECT * FROM task_messages WHERE id = ? AND task_id = ?", (question_id, n))
    if row is None:
        problems.append(f"{_t(n)} has no message {question_id}")
    elif row["kind"] != "question":
        problems.append(f"message {question_id} is not a question")
        return None
    return row


def _completion_problems(
    store: Store,
    row: sqlite3.Row,
    *,
    checklist: Mapping[str, Any] | None,
    commit: str | None,
    merge_request: str | None,
    merge_reason: str,
    submission_candidate: str | None,
    result: str | None,
    outcome: str,
    failure: Mapping[str, Any] | None,
) -> tuple[_list[str], dict[str, Any], int | None]:
    """What ``complete`` refuses for a succeeded Task, the checklist it would store, and the
    Experiment that checklist names."""
    problems: _list[str] = []
    require(problems, outcome == "succeeded", "outcome must be succeeded or failed")
    require(problems, failure is None, "failure belongs to outcome failed")
    require(
        problems,
        row["status"] == "in-progress",
        f"{_t(row['id'])} is {row['status']}, not in-progress",
    )
    from research_core.store.experiments import REQUEST_TERMINAL_STATES

    pending = {
        display_id("X", run["experiment_id"])
        for run in store.q(
            "SELECT r.experiment_id,r.state FROM experiment_runs r"
            " JOIN experiments e ON e.id=r.experiment_id"
            " WHERE e.task_id=? AND e.status='running' AND r.kind='xp'",
            (row["id"],),
        )
        if run["state"] not in REQUEST_TERMINAL_STATES
    }
    if pending:
        problems.append(f"{_t(row['id'])} has pending requests in {', '.join(sorted(pending))}")
    require(
        problems,
        isinstance(checklist, Mapping),
        "checklist is required: the type's deliverables",
    )
    require(problems, merge_request in ("yes", "no"), "merge must be yes or no")
    if merge_request == "yes":
        require(problems, commit, "merge yes needs the commit to merge")
    if merge_request == "no":
        require(problems, _text(merge_reason), "merge no needs merge_reason")
    require(problems, _text(result), "result is required: what came of the Task")
    if submission_candidate is not None:
        require(
            problems,
            submission_candidate in ("yes", "no"),
            "submission_candidate must be yes or no",
        )
    problems.extend(_commit_problems(commit))
    accepted: dict[str, Any] = dict(checklist) if isinstance(checklist, Mapping) else {}
    if commit is not None:
        accepted.setdefault("commit", commit)
    if submission_candidate is not None:
        accepted.setdefault("submission_candidate", submission_candidate)
    experiment_n = None
    if isinstance(checklist, Mapping):
        missing = task_types.check(row["type"], accepted, task_fields=dict(row))
        problems.extend(f"checklist: {item}" for item in missing)
        if missing:
            problems.append(
                "give each missing deliverable; if the Task could not produce one, finish with"
                " outcome failed and failure {what_failed, next_step} instead"
            )
        experiment_n = _reference_problems(store, row, accepted, problems)
    return problems, accepted, experiment_n


def _fail_problems(row: sqlite3.Row, failure: Any, commit: Any) -> _list[str]:
    """What ``fail`` refuses."""
    problems: _list[str] = []
    require(
        problems,
        row["status"] == "in-progress",
        f"{_t(row['id'])} is {row['status']}, not in-progress",
    )
    problems.extend(_failure_problems(failure))
    problems.extend(_commit_problems(commit))
    return problems


def _commit_problems(commit: Any) -> list[str]:
    if commit is None or (isinstance(commit, str) and _SHA.match(commit)):
        return []
    return [f"commit must be a git sha of 7 to 40 hex digits, not {commit!r}"]


def _failure_problems(failure: Any) -> list[str]:
    if not isinstance(failure, Mapping):
        return ["failure must be an object with what_failed and next_step"]
    problems = [
        f"failure.{key} is required" for key in FAILURE_REQUIRED if not _text(failure.get(key))
    ]
    exit_code = failure.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        problems.append("failure.exit_code must be an int")
    unknown = sorted(set(failure) - set(FAILURE_REQUIRED) - set(FAILURE_OPTIONAL))
    if unknown:
        problems.append(f"failure has unknown keys: {', '.join(unknown)}")
    return problems


def _reference_problems(
    store: Store, row: sqlite3.Row, accepted: Mapping[str, Any], problems: list[str]
) -> int | None:
    """Every record the checklist names must exist; returns the Experiment's number if any."""
    for n in _ids(accepted.get("claims"), "claim"):
        if not _exists(store, "claims", n):
            problems.append(f"{display_id('C', n)} does not exist")
    for n in _ids(accepted.get("versions"), "version"):
        if not _exists(store, "versions", n):
            problems.append(f"{display_id('V', n)} does not exist")
    # The Orchestrator may approve or reject a proposal before the Task completes, so what counts
    # is who proposed it, not its status now. A Task's holders are its sessions, successors too.
    proposers = {
        f"session:{session['session_id']}"
        for session in store.q(
            "SELECT session_id FROM task_sessions WHERE task_id = ?", (row["id"],)
        )
    }
    for n in _ids(accepted.get("proposed_tasks"), "task"):
        proposed = store.one("SELECT proposed_by, goal_id FROM tasks WHERE id = ?", (n,))
        if n == row["id"]:
            problems.append(f"{_t(n)} is the ideation Task itself")
        elif proposed is None:
            problems.append(f"{_t(n)} does not exist")
        elif proposed["proposed_by"] not in proposers:
            problems.append(f"{_t(n)} was not proposed by this Task")
        elif proposed["goal_id"] != row["goal_id"]:
            problems.append(f"{_t(n)} is not proposed for this Task's Goal")
    experiments = _ids(accepted.get("experiment"), "experiment")
    if not experiments:
        return None
    experiment_n = experiments[0]
    if not _exists(store, "experiments", experiment_n):
        problems.append(f"{display_id('X', experiment_n)} does not exist")
    elif row["experiment_id"] not in (None, experiment_n):
        problems.append(
            f"{_t(row['id'])} runs {display_id('X', row['experiment_id'])}; the checklist names"
            f" {display_id('X', experiment_n)}"
        )
    return experiment_n


def _ids(value: Any, kind: str) -> _list[int]:
    """The well-formed display ids of ``kind`` in a checklist value (others were reported)."""
    items = value if isinstance(value, _list | tuple) else [value]
    found: _list[int] = []
    for item in items:
        if not isinstance(item, str):
            continue
        try:
            item_kind, n = parse_display_id(item)
        except ValueError:
            continue
        if item_kind == kind:
            found.append(n)
    return found


def _reopen(tx: Tx, n: int, reason: str) -> None:
    tx.execute(
        "UPDATE tasks SET status = 'in-progress', outcome = NULL, merge = NULL, updated = ?"
        " WHERE id = ?",
        (tx.at, n),
    )
    _insert_message(tx, n, "to_researcher", "message", f"Reopened: {reason}")


def _abandon(tx: Tx, row: sqlite3.Row, failure: dict[str, Any], extra: dict[str, Any]) -> None:
    n = row["id"]
    tx.execute(
        "UPDATE tasks SET status = 'abandoned', outcome = 'abandoned', failure = ?, held_by = NULL,"
        " run_id = NULL, lease_renewed = NULL, waiting_on = NULL, updated = ? WHERE id = ?",
        (json_field(failure), tx.at, n),
    )
    payload = {"failure": failure, "previous_holder": row["held_by"], "run_id": row["run_id"]}
    tx.event("task.abandoned", _t(n), {**payload, **extra})
