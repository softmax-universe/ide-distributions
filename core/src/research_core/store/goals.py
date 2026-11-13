"""Goals: what the user wants, with an end state, a status and computed progress (design B.3).

Stored fields are the columns of ``goals``. ``read`` adds what tools derive: Tasks and
Experiments by status, the claims tagged with the Goal (tag ``G-<n>``) with their freshness,
the Goal's active directives, ``last_progress`` and ``ideation_rounds_without_progress``.

Progress is a Task under the Goal completing with outcome ``succeeded`` (ideation Tasks
excepted: proposing work is not progress), a claim tagged with the Goal being created, or a
Version created by one of the Goal's Tasks or Experiments. An ideation round is an ideation
Task that reached ``complete``; the rounds without progress are those created after the last
progress, which is what the parking rule (``ideation.park_after_rounds``) counts.

Goal-less maintenance Tasks (amendment A2.2) have no Goal row; ``read(store, None)`` returns
the pseudo-Goal "workspace maintenance" that groups them for the briefing and the panel.

The tasks, experiments, claims and versions tables are queried directly: those record modules
are siblings, not dependencies.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from research_core.store import directives
from research_core.store._common import display_id, fetch_row, json_field, require
from research_core.store.errors import NotFound, StoreError
from research_core.store.registry import (
    EXPERIMENT_STATUSES,
    GOAL,
    GOAL_KINDS,
    GOAL_STATUSES,
    TASK_STATUSES,
    record_from_row,
)

if TYPE_CHECKING:
    from research_core.store.actors import Actor
    from research_core.store.db import Store

WORKSPACE_MAINTENANCE_TITLE = "workspace maintenance"
OPEN_TASK_STATUSES = ("proposed", "unclaimed", "in-progress")
TITLE_MAX_CHARS = 200
# What ``update`` accepts. ``update_for_human`` appends a dated note to ``updates_for_human``,
# which is never rewritten; every other field replaces its value.
UPDATABLE_FIELDS = (
    "title",
    "kind",
    "end_state",
    "reason",
    "description",
    "judgement",
    "tags",
    "where_it_stands",
    "update_for_human",
)
_REQUIRED_TEXT = ("title", "end_state", "reason")

# A claim is the Goal's when its tags JSON array holds the Goal's display id.
_TAGGED_WITH = "EXISTS (SELECT 1 FROM json_each(c.tags) WHERE json_each.value = ?)"


def _is_text(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_tags(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str)
        and all(isinstance(tag, str) and tag.strip() for tag in value)
    )


def _check_fields(problems: list[str], fields: dict[str, Any]) -> None:
    """Problems with the given Goal fields; shared by ``create`` and ``update``."""
    for name in _REQUIRED_TEXT:
        if name in fields:
            require(problems, _is_text(fields[name]), f"{name} is required")
    if "kind" in fields:
        require(problems, fields["kind"] in GOAL_KINDS, "kind must be outcome or ongoing")
    if "title" in fields and isinstance(fields["title"], str):
        require(
            problems,
            len(fields["title"]) <= TITLE_MAX_CHARS,
            f"title is longer than {TITLE_MAX_CHARS} characters",
        )
    for name in ("description", "where_it_stands"):
        if name in fields:
            require(problems, isinstance(fields[name], str), f"{name} must be text")
    if "update_for_human" in fields:
        require(problems, _is_text(fields["update_for_human"]), "update_for_human is required")
    if "judgement" in fields:
        require(
            problems,
            fields["judgement"] is None or _is_text(fields["judgement"]),
            "judgement must be text or null",
        )
    if "tags" in fields:
        require(problems, _is_tags(fields["tags"]), "tags must be a list of non-empty words")


def create(
    store: Store,
    actor: Actor,
    *,
    title: str,
    end_state: str,
    reason: str,
    kind: str = "outcome",
    description: str = "",
    judgement: str | None = None,
    tags: Sequence[str] = (),
) -> dict[str, Any]:
    """A new active Goal; event ``goal.created``."""
    problems: list[str] = []
    _check_fields(
        problems,
        {
            "title": title,
            "kind": kind,
            "end_state": end_state,
            "reason": reason,
            "description": description,
            "judgement": judgement,
            "tags": tags,
        },
    )
    if problems:
        raise StoreError(problems)
    with store.tx(actor) as tx:
        cursor = tx.execute(
            "INSERT INTO goals(title, end_state, reason, status, judgement, tags, description,"
            " created, created_by, updated, kind) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                end_state,
                reason,
                judgement,
                json_field([*tags]),
                description,
                tx.at,
                str(actor),
                tx.at,
                kind,
            ),
        )
        goal_id = cursor.lastrowid
        tx.event("goal.created", display_id("G", goal_id), {"title": title})
    return read(store, goal_id)


def update(store: Store, actor: Actor, goal_id: int, **fields: Any) -> dict[str, Any]:
    """Replace the given fields (``UPDATABLE_FIELDS``); event ``goal.updated``.

    ``update_for_human=`` appends ``[<time>] <note>`` to ``updates_for_human``.
    """
    problems: list[str] = []
    for name in sorted(set(fields) - set(UPDATABLE_FIELDS)):
        problems.append(f"{name} is not a Goal field that update accepts")
    require(problems, fields, "nothing to update: give at least one field")
    _check_fields(problems, fields)
    if problems:
        raise StoreError(problems)
    with store.tx(actor) as tx:
        row = fetch_row(store, "goals", goal_id, display=f"G-{goal_id}")
        assignments: list[str] = []
        params: list[Any] = []
        for name, value in fields.items():
            column = name
            if name == "update_for_human":
                column = "updates_for_human"
                note = f"[{tx.at}] {value.strip()}"
                value = (
                    f"{row['updates_for_human']}\n\n{note}" if row["updates_for_human"] else note
                )
            elif name == "tags":
                value = json_field([*value])
            assignments.append(f"{column} = ?")
            params.append(value)
        tx.execute(
            f"UPDATE goals SET {', '.join(assignments)}, updated = ? WHERE id = ?",
            (*params, tx.at, goal_id),
        )
        tx.event("goal.updated", display_id("G", goal_id), {"fields": sorted(fields)})
    return read(store, goal_id)


def set_status(
    store: Store, actor: Actor, goal_id: int, status: str, status_reason: str
) -> dict[str, Any]:
    """Move the Goal along ``GOAL.transitions`` with a reason; event ``goal.status``."""
    problems: list[str] = []
    require(
        problems,
        status in GOAL_STATUSES,
        f"status must be one of {', '.join(GOAL_STATUSES)}, not {status!r}",
    )
    require(problems, _is_text(status_reason), "status_reason is required: say why")
    with store.tx(actor) as tx:
        row = fetch_row(store, "goals", goal_id, display=f"G-{goal_id}")
        current = row["status"]
        shown = display_id("G", row["id"])
        if status in GOAL_STATUSES and not GOAL.can_transition(current, status):
            allowed = GOAL.transitions.get(current, ())
            if allowed:
                problems.append(f"{shown} is {current}; it can become {' or '.join(allowed)}")
            else:
                problems.append(f"{shown} is {current}; it cannot become {status}")
        if problems:
            raise StoreError(problems)
        tx.execute(
            "UPDATE goals SET status = ?, status_reason = ?, updated = ? WHERE id = ?",
            (status, status_reason, tx.at, goal_id),
        )
        tx.event("goal.status", shown, {"from": current, "to": status, "reason": status_reason})
    return read(store, goal_id)


def read(store: Store, goal_id: int | None) -> dict[str, Any]:
    """The Goal with its computed fields; ``None`` reads the workspace-maintenance pseudo-Goal."""
    if goal_id is None:
        return {
            "id": None,
            "title": WORKSPACE_MAINTENANCE_TITLE,
            "status": "active",
            "tasks": _tasks_by_status(store, None),
        }
    row = fetch_row(store, "goals", goal_id, display=f"G-{goal_id}")
    record = record_from_row(GOAL, row)
    shown = record["id"]
    last_progress = _last_progress(store, goal_id, shown)
    record["tasks"] = _tasks_by_status(store, goal_id)
    record["experiments"] = _experiments_by_status(store, goal_id)
    record["claims"] = _claims_tagged(store, shown)
    record["last_progress"] = last_progress
    record["ideation_rounds_without_progress"] = _rounds_since(store, goal_id, last_progress)
    record["directives"] = directives.list(store, scope=f"goal:{goal_id}")
    return record


def list(store: Store, status: str | None = None) -> list[dict[str, Any]]:
    """Stored fields of every Goal (no computed fields), oldest first, optionally by status."""
    if status is not None and status not in GOAL_STATUSES:
        raise StoreError([f"status must be one of {', '.join(GOAL_STATUSES)}, not {status!r}"])
    rows = store.q(
        "SELECT * FROM goals WHERE (? IS NULL OR status = ?) ORDER BY id", (status, status)
    )
    return [record_from_row(GOAL, row) for row in rows]


def needs_ideation(store: Store) -> list[int]:
    """Ids of active Goals with no open Task, running standalone Experiment or user wait.

    The management wake creates or accepts an ideation Task for each (design §8 wake table).
    """
    rows = store.q(
        "SELECT g.id FROM goals g WHERE g.status = 'active'"
        " AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.goal_id = g.id"
        "   AND t.status IN ('proposed', 'unclaimed', 'in-progress'))"
        " AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.goal_id = g.id"
        "   AND json_extract(t.waiting_on, '$.kind') = 'user')"
        " AND NOT EXISTS (SELECT 1 FROM experiments x WHERE x.goal_id = g.id"
        "   AND x.task_id IS NULL AND x.status = 'running')"
        " ORDER BY g.id"
    )
    return [row["id"] for row in rows]


def ideation_revision(store: Store, goal_id: int) -> int:
    """The latest lifecycle event that can create a new gap in an ongoing Goal's work."""
    return store.one(
        "SELECT COALESCE(MAX(seq), 0) FROM ("
        " SELECT MAX(e.seq) AS seq FROM tasks t JOIN events e ON e.subject='T-' || t.id"
        " WHERE t.goal_id=? AND e.kind IN ('task.completed','task.failed','task.abandoned')"
        " UNION ALL SELECT MAX(e.seq) FROM experiments x JOIN events e ON e.subject='X-' || x.id"
        " WHERE x.goal_id=? AND x.task_id IS NULL"
        " AND e.kind IN ('experiment.closed','experiment.abandoned')"
        " UNION ALL SELECT MAX(seq) FROM events WHERE subject=?"
        " AND kind IN ('goal.created','goal.status'))",
        (goal_id, goal_id, display_id("G", goal_id)),
    )[0]


def progress_since_round(store: Store, goal_id: int, round: int) -> bool:
    """Whether the Goal progressed after its ideation Task of round ``round`` was created."""
    fetch_row(store, "goals", goal_id, display=f"G-{goal_id}")
    started = store.one(
        "SELECT MIN(created) FROM tasks"
        " WHERE goal_id = ? AND type = 'ideation' AND ideation_round = ?",
        (goal_id, round),
    )[0]
    if started is None:
        raise NotFound([f"G-{goal_id} has no ideation round {round}"])
    last_progress = _last_progress(store, goal_id, display_id("G", goal_id))
    return last_progress is not None and last_progress > started


# --- computed fields ---------------------------------------------------------------------------


def _tasks_by_status(store: Store, goal_id: int | None) -> dict[str, Any]:
    """``{<status>: count, ..., "open": ["T-1", ...]}``; ``None`` means the goal-less Tasks."""
    where = "goal_id IS NULL" if goal_id is None else "goal_id = ?"
    params = () if goal_id is None else (goal_id,)
    rows = store.q(f"SELECT id, status FROM tasks WHERE {where} ORDER BY id", params)
    summary: dict[str, Any] = {status: 0 for status in TASK_STATUSES}
    open_ids = []
    for row in rows:
        summary[row["status"]] += 1
        if row["status"] in OPEN_TASK_STATUSES:
            open_ids.append(display_id("T", row["id"]))
    summary["open"] = open_ids
    return summary


def _experiments_by_status(store: Store, goal_id: int) -> dict[str, int]:
    counts = dict.fromkeys(EXPERIMENT_STATUSES, 0)
    rows = store.q(
        "SELECT status, COUNT(*) AS n FROM experiments WHERE goal_id = ? GROUP BY status",
        (goal_id,),
    )
    for row in rows:
        counts[row["status"]] = row["n"]
    return counts


def _claims_tagged(store: Store, shown: str) -> list[dict[str, Any]]:
    """Claims tagged with the Goal; freshness is the newest supporting or qualifying evidence."""
    rows = store.q(
        "SELECT c.id, c.summary, c.status,"
        " (SELECT MAX(e.added) FROM claim_evidence e WHERE e.claim_id = c.id"
        "   AND e.stance IN ('supports', 'qualifies')) AS freshness"
        f" FROM claims c WHERE {_TAGGED_WITH} ORDER BY c.id",
        (shown,),
    )
    return [
        {
            "id": display_id("C", row["id"]),
            "summary": row["summary"],
            "status": row["status"],
            "freshness": row["freshness"],
        }
        for row in rows
    ]


def _last_progress(store: Store, goal_id: int, shown: str) -> str | None:
    """The newest of: a non-ideation Task succeeding, a tagged claim, a Version of the Goal."""
    task_at = store.one(
        "SELECT MAX(updated) FROM tasks WHERE goal_id = ? AND status = 'complete'"
        " AND outcome = 'succeeded' AND type <> 'ideation'",
        (goal_id,),
    )[0]
    claim_at = store.one(f"SELECT MAX(c.created) FROM claims c WHERE {_TAGGED_WITH}", (shown,))[0]
    version_at = store.one(
        "SELECT MAX(v.created) FROM versions v"
        " LEFT JOIN tasks t ON t.id = v.task_id"
        " LEFT JOIN experiments x ON x.id = v.experiment_id"
        " WHERE t.goal_id = ? OR x.goal_id = ?",
        (goal_id, goal_id),
    )[0]
    times = [at for at in (task_at, claim_at, version_at) if at is not None]
    return max(times) if times else None


def _rounds_since(store: Store, goal_id: int, last_progress: str | None) -> int:
    """Completed ideation rounds created after ``last_progress`` (all of them when None)."""
    return store.one(
        "SELECT COUNT(*) FROM tasks WHERE goal_id = ? AND type = 'ideation'"
        " AND status = 'complete' AND (? IS NULL OR created > ?)",
        (goal_id, last_progress, last_progress),
    )[0]
