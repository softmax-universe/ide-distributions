"""The field registry: one source for validation, the SOUL Records section and tool schemas.

Each ``RecordSpec`` names a record kind's table, display prefix, fields, statuses and the
status transitions the record modules enforce. Stored fields are named after their columns;
computed fields are what tools derive on read (design B.2 "stored vs computed") and are never
columns. Descriptions are one sentence each, from the design (B.3-B.9, B.13, B.14) and the
draft tables it keeps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from research_core.store._common import (
    KIND_PREFIXES,
    display_id,
    display_id_for,
    parse_display_id,
    parse_json,
)

__all__ = [
    "FIELD_TYPES",
    "RECORDS",
    "FieldSpec",
    "RecordSpec",
    "display_id",
    "display_id_for",
    "parse_display_id",
    "record_from_row",
    "spec_for_table",
]

# ``json`` and ``list`` fields are JSON text in the store and parsed on read.
FIELD_TYPES = (
    "id",
    "int",
    "text",
    "markdown",
    "enum",
    "json",
    "list",
    "timestamp",
    "actor",
    "bool",
)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    required: bool
    description: str
    enum: tuple[str, ...] | None = None
    computed: bool = False


@dataclass(frozen=True)
class RecordSpec:
    name: str
    prefix: str
    table: str
    fields: list[FieldSpec]
    statuses: tuple[str, ...]
    transitions: dict[str, tuple[str, ...]]
    description: str
    # The store whose migration defines ``table`` ("coworld" or "workspace").
    store: str = "coworld"

    def field(self, name: str) -> FieldSpec:
        for spec in self.fields:
            if spec.name == name:
                return spec
        raise KeyError(f"{self.name} has no field {name!r}")

    @property
    def stored_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if not f.computed]

    @property
    def computed_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.computed]

    @property
    def required_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.required]

    @property
    def json_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.type in ("json", "list")]

    @property
    def status_field(self) -> FieldSpec | None:
        for name in ("status", "state"):
            try:
                return self.field(name)
            except KeyError:
                continue
        return None

    def can_transition(self, from_status: str, to_status: str) -> bool:
        return to_status in self.transitions.get(from_status, ())


def _f(
    name: str,
    type: str,
    description: str,
    *,
    required: bool = False,
    enum: tuple[str, ...] | None = None,
    computed: bool = False,
) -> FieldSpec:
    return FieldSpec(name, type, required, description, enum, computed)


def _spec(
    name: str,
    table: str,
    description: str,
    fields: list[FieldSpec],
    *,
    statuses: tuple[str, ...] = (),
    transitions: dict[str, tuple[str, ...]] | None = None,
    store: str = "coworld",
) -> RecordSpec:
    return RecordSpec(
        name=name,
        prefix=KIND_PREFIXES.get(name, ""),
        table=table,
        fields=fields,
        statuses=statuses,
        transitions=dict(transitions or {}),
        description=description,
        store=store,
    )


_created = _f("created", "timestamp", "When the record was created, to the microsecond.")
_created_by = _f("created_by", "actor", "The session, user or harness that created it.")
_updated = _f("updated", "timestamp", "When the record last changed.")

GOAL_STATUSES = ("active", "parked", "completed", "abandoned")
TASK_TYPES = ("research", "improvement", "ideation", "maintenance")
TASK_STATUSES = ("proposed", "unclaimed", "in-progress", "complete", "abandoned")
EXPERIMENT_STATUSES = ("proposed", "running", "confirmed", "refuted", "inconclusive", "abandoned")
CLAIM_STATUSES = ("active", "superseded", "refuted")
CHAMPION_STATES = ("pending", "processing", "placed", "rejected", "champion", "none")
CAPABILITY_STATUSES = ("unknown", "ok", "broken")
INTENT_KINDS = ("upload", "submit", "xp", "forum", "wiki", "run", "champion")
# Why a wake's ``ui.tell`` should reach the user now; ``chat_updates.categories`` picks which do.
CHAT_UPDATE_CATEGORIES = ("decision", "outcome", "submission", "failure", "merge")
INTENT_STATES = ("pending", "done", "failed")
WAIT_KINDS = (
    "question",
    "process",
    "xp",
    "credits",
    "permit",
    "submission",
    "platform",
    "user",
    "dependency",
)

GOAL = _spec(
    "goal",
    "goals",
    "What the user wants: an outcome with an end state that can be judged and a status.",
    [
        _f("id", "id", "The Goal's id, shown as G-<n>."),
        _f("title", "text", "Short name of the outcome.", required=True),
        _f(
            "end_state",
            "text",
            "What must be true for the Goal to be complete, stated so it can be judged.",
            required=True,
        ),
        _f(
            "reason",
            "text",
            "Why the Goal exists, usually quoting the user's request.",
            required=True,
        ),
        _f("status", "enum", "active, parked, completed or abandoned.", enum=GOAL_STATUSES),
        _f("status_reason", "text", "Why the status last changed; every status change needs one."),
        _f("judgement", "text", "Instructions for judging the end state."),
        _f("tags", "list", "Keywords from the shared vocabulary."),
        _f("description", "markdown", "The Goal in full, in Markdown."),
        _f(
            "where_it_stands",
            "markdown",
            "Where the Goal stands now, in plain language, replaced on each update.",
        ),
        _f(
            "updates_for_human",
            "markdown",
            "Plain-language progress notes for the user, appended and never rewritten.",
        ),
        _created,
        _created_by,
        _updated,
        _f("tasks", "json", "Tasks by status, with the ids of the open ones.", computed=True),
        _f("experiments", "json", "Experiments by status.", computed=True),
        _f(
            "claims",
            "list",
            "Claims tagged with this Goal, each with its freshness.",
            computed=True,
        ),
        _f(
            "ideation_rounds_without_progress",
            "int",
            "Ideation rounds since the last progress; the parking rule counts these.",
            computed=True,
        ),
        _f(
            "last_progress",
            "timestamp",
            "When a Task, claim or Version last moved the Goal.",
            computed=True,
        ),
        _f("directives", "list", "The active directives scoped to this Goal.", computed=True),
    ],
    statuses=GOAL_STATUSES,
    transitions={
        "active": ("parked", "completed", "abandoned"),
        "parked": ("active", "abandoned"),
    },
)

TASK = _spec(
    "task",
    "tasks",
    "One unit of work under a Goal, with dependencies, a wait state and a type checklist.",
    [
        _f("id", "id", "The Task's id, shown as T-<n>."),
        _f("goal_id", "int", "The parent Goal; only maintenance Tasks have none."),
        _f(
            "type",
            "enum",
            "research, improvement, ideation or maintenance.",
            required=True,
            enum=TASK_TYPES,
        ),
        _f("title", "text", "Short name, for the agents.", required=True),
        _f("description", "markdown", "What to do and deliver.", required=True),
        _f(
            "reasoning",
            "markdown",
            "Why, with references to the Goal, claims and Experiments.",
        ),
        _f("for_human", "text", "One plain sentence that clients show."),
        _f(
            "end_condition",
            "enum",
            "How the Task ends: a deliverable, a number of steps or an effort bound.",
            enum=("deliverable", "steps", "effort"),
        ),
        _f("end_detail", "text", "The detail of the end condition."),
        _f("experiment_id", "int", "The one Experiment an improvement Task runs."),
        _f(
            "status",
            "enum",
            "proposed, unclaimed, in-progress, complete, or abandoned.",
            enum=TASK_STATUSES,
        ),
        _f(
            "priority",
            "enum",
            "normal or high; repair Tasks are high and take the next free Researcher slot.",
            enum=("normal", "high"),
        ),
        _f("proposed_by", "actor", "Who proposed the Task, when a Researcher suggested it."),
        _f(
            "approved_by",
            "actor",
            "Who accepted the Task; for types in approval_required.task_types only the user can.",
        ),
        _f("approved_at", "timestamp", "When the Task was accepted."),
        _f(
            "outcome",
            "enum",
            "On completion: succeeded, failed, inconclusive or abandoned.",
            enum=("succeeded", "failed", "inconclusive", "abandoned"),
        ),
        _f("result", "markdown", "On completion: what came of it."),
        _f(
            "failure",
            "json",
            "{what_failed, error, exit_code, tried, next_step}; required when a Task fails or is "
            "abandoned.",
        ),
        _f(
            "last_error_seen",
            "json",
            "The Session's last failed tool call (command, exit code, error tail), kept by a hook.",
        ),
        _f(
            "waiting_on",
            "json",
            "{kind, detail, ref, since} while blocked; kind is question, process, xp, credits, "
            "permit, submission, platform, user or dependency.",
        ),
        _f("held_by", "text", "The Session holding the Task's lease."),
        _f("lease_epoch", "int", "The lease's epoch, raised at each claim."),
        _f("lease_renewed", "timestamp", "When the holder last renewed the lease."),
        _f("run_id", "text", "The holder's current Hermes run."),
        _f("worktree", "text", "The Task's own git worktree."),
        _f("branch", "text", "The Task's branch, task/<n>."),
        _f(
            "ideation_round",
            "int",
            "Round number for ideation Tasks; the parking rule counts these.",
        ),
        _f("checklist", "json", "The values the completion tool accepted."),
        _f("merge", "text", "merged <sha>, not merged: <reason>, or pending."),
        _f(
            "completed_commit",
            "text",
            "The commit task.complete recorded; the merge uses it, not the branch tip.",
        ),
        _f("capability", "text", "For a repair Task, the capability it restores."),
        _created,
        _created_by,
        _updated,
        _f(
            "depends_on",
            "list",
            "Other Tasks that must complete first; a Task held for a broken capability depends on "
            "its repair Task.",
            computed=True,
        ),
        _f("sessions", "list", "Session rows that worked on the Task.", computed=True),
        _f(
            "messages",
            "list",
            "Messages between the Orchestrator and the Researcher.",
            computed=True,
        ),
        _f(
            "stuck",
            "bool",
            "In progress with no live run and no wait for a few minutes; the watcher resumes it.",
            computed=True,
        ),
    ],
    statuses=TASK_STATUSES,
    transitions={
        "proposed": ("unclaimed", "abandoned"),
        "unclaimed": ("in-progress", "abandoned"),
        "in-progress": ("complete", "unclaimed", "abandoned"),
        "complete": ("in-progress",),
    },
)

VERSION = _spec(
    "version",
    "versions",
    "A policy candidate: a commit of policy code, or a policy the user uploaded elsewhere.",
    [
        _f("id", "id", "The Version's id, shown as V-<n>; tag v/<n> keeps its commit reachable."),
        _f("summary", "text", "What this Version changes, in one sentence.", required=True),
        _f(
            "source",
            "enum",
            "built here from a commit, or adopted from a policy version uploaded outside.",
            enum=("built", "adopted"),
        ),
        _f("commit_sha", "text", "The exact commit, for a built Version."),
        _f("branch", "text", "The branch the commit was built from."),
        _f(
            "artifact",
            "json",
            "{kind, path, digest, size}: the exact policy bytes the build produced.",
        ),
        _f("task_id", "int", "The Task that created it, if any."),
        _f("experiment_id", "int", "The Experiment that created it, if any."),
        _f("uploaded", "json", "{policy_version_id, at}: the platform's id for it, once uploaded."),
        _f("image", "text", "The workspace image digest at build time."),
        _f("policy_name", "text", "The platform policy name it was uploaded under."),
        _created,
        _created_by,
        _f("based_on", "list", "The Versions it was built from.", computed=True),
        _f(
            "submission_status",
            "text",
            "uploaded, submitted, qualifying, competing or champion, from the record and the "
            "platform states.",
            computed=True,
        ),
        _f(
            "experiments",
            "list",
            "Experiments that used it as baseline or candidate.",
            computed=True,
        ),
    ],
)

CLAIM = _spec(
    "claim",
    "claims",
    "A short finding with typed evidence links; freshness and contested state are computed on "
    "read.",
    [
        _f("id", "id", "The claim's id, shown as C-<n>."),
        _f("summary", "text", "The claim in one sentence of at most 40 words.", required=True),
        _f(
            "body",
            "markdown",
            "The claim in full, at most 500 words: detail, numbers, conditions and what would "
            "change our mind.",
        ),
        _f(
            "tags",
            "list",
            "Keywords from the shared vocabulary, including the Goals the claim serves.",
        ),
        _f(
            "status",
            "enum",
            "active, superseded or refuted; retired claims are kept.",
            enum=CLAIM_STATUSES,
        ),
        _f("superseded_by", "int", "When superseded, the claim that replaces it."),
        _f(
            "see_also",
            "list",
            "Other claims worth reading alongside; navigation only, not support.",
        ),
        _created,
        _created_by,
        _updated,
        _f(
            "evidence",
            "list",
            "The typed evidence links: {type, ref, stance, note, added, by}; at least one.",
            computed=True,
        ),
        _f(
            "freshness", "timestamp", "The newest supporting or qualifying evidence.", computed=True
        ),
        _f(
            "contested",
            "bool",
            "Whether contradicting evidence arrived after the claim was last confirmed.",
            computed=True,
        ),
        _f(
            "counts",
            "json",
            "How many links support, contradict and qualify the claim.",
            computed=True,
        ),
        _f("cited_by", "list", "Experiments and claims that cite this claim.", computed=True),
    ],
    statuses=CLAIM_STATUSES,
    transitions={"active": ("superseded", "refuted")},
)

EXPERIMENT = _spec(
    "experiment",
    "experiments",
    "A comparison of Versions whose hypothesis and methods lock at start and end in a verdict.",
    [
        _f("id", "id", "The Experiment's id, shown as X-<n>."),
        _f(
            "summary",
            "text",
            "What is being tested, in one sentence of at most 40 words.",
            required=True,
        ),
        _f("goal_id", "int", "The Goal the Experiment serves, if any."),
        _f("task_id", "int", "The Task that runs it, if any."),
        _f("tags", "list", "Keywords from the shared vocabulary; the lever is a tag."),
        _f(
            "status",
            "enum",
            "proposed, running, then confirmed, refuted or inconclusive; or abandoned.",
            enum=EXPERIMENT_STATUSES,
        ),
        _f(
            "hypothesis",
            "json",
            "{statement, if_true, if_false, motivation}; if_true and if_false must differ.",
            required=True,
        ),
        _f(
            "methods",
            "json",
            "{baseline, candidate, procedure, metrics, decision_rule}; any Version may be the "
            "baseline.",
            required=True,
        ),
        _f("results", "json", "{runs, measurements, interpretation}, filled as runs come in."),
        _f("related_claims", "list", "Claims the Experiment builds on or was checked against."),
        _f(
            "related_experiments",
            "list",
            "Earlier Experiments it follows from, repeats or revises.",
        ),
        _f(
            "design_sha256",
            "text",
            "Hash of hypothesis and methods, fixed when the Experiment starts.",
        ),
        _created,
        _f("closed", "timestamp", "When the verdict was recorded."),
        _f("abandon_reason", "text", "Why it was stopped before a verdict."),
        _created_by,
        _f(
            "runs",
            "list",
            "Its evaluation runs, each with its platform request id and validity.",
            computed=True,
        ),
        _f("cited_by", "list", "Claims that cite this Experiment as evidence.", computed=True),
        _f("versions_tested", "list", "The Versions compared.", computed=True),
    ],
    statuses=EXPERIMENT_STATUSES,
    transitions={
        "proposed": ("running", "abandoned"),
        "running": ("confirmed", "refuted", "inconclusive", "abandoned"),
    },
)


def _directive_fields(shown_as: str, scope: str) -> list[FieldSpec]:
    return [
        _f("id", "id", f"The directive's id, shown as {shown_as}."),
        _f("text", "text", "The user's instruction, word for word.", required=True),
        _f("scope", "text", scope, required=True),
        _f("source", "text", "The message reference the instruction came from.", required=True),
        _f("status", "enum", "active or retired.", enum=("active", "retired")),
        _created,
        _created_by,
        _f("retired", "timestamp", "When it was retired."),
    ]


# Each store numbers its own ``directives`` table, so the two kinds have separate prefixes: an id
# names one directive and says which store holds it.
DIRECTIVE = _spec(
    "directive",
    "directives",
    "A standing instruction on a Goal.",
    _directive_fields("D-<n>", "goal:<n>."),
    statuses=("active", "retired"),
    transitions={"active": ("retired",)},
)

WORKSPACE_DIRECTIVE = _spec(
    "workspace_directive",
    "directives",
    "A standing instruction on the workspace.",
    _directive_fields("W-<n>", "workspace."),
    statuses=("active", "retired"),
    transitions={"active": ("retired",)},
    store="workspace",
)

CHAMPION = _spec(
    "champion",
    "champion",
    "The policy version that represents the Player in a league (never the league's winner).",
    [
        _f("league_id", "text", "The league, the record's key.", required=True),
        _f("division_id", "text", "The division the Player is placed in."),
        _f("membership_id", "text", "The league policy membership id."),
        _f("version_id", "int", "The local Version of the champion, when known."),
        _f("policy_version_id", "text", "The platform's policy version id."),
        _f(
            "state",
            "enum",
            "The platform's path: pending, processing, placed or rejected, then champion; none "
            "when empty.",
            enum=CHAMPION_STATES,
        ),
        _f("since", "timestamp", "When this policy version became the champion."),
        _f("approved_by", "actor", "Who approved the submission."),
        _f("reason", "text", "Why it was submitted."),
        _f("synced", "timestamp", "When the record was last compared with the platform."),
        _f("history", "list", "Earlier champion states, newest first.", computed=True),
    ],
    statuses=CHAMPION_STATES,
)

CAPABILITY = _spec(
    "capability",
    "capabilities",
    "One checked capability: a mixin command, a platform login, a CLI or GitHub access.",
    [
        _f("name", "text", "The capability's name, the record's key.", required=True),
        _f("kind", "text", "What kind of capability: a mixin command, a CLI, a login, a route."),
        _f("status", "enum", "unknown, ok or broken.", enum=CAPABILITY_STATUSES),
        _f("last_check", "timestamp", "When it was last checked."),
        _f("output", "text", "The full output of the last check."),
        _f("maintenance_task_id", "int", "The high-priority maintenance Task repairing it."),
        _f("command", "text", "The command the check runs."),
    ],
    statuses=CAPABILITY_STATUSES,
    transitions={
        "unknown": ("ok", "broken"),
        "ok": ("broken", "unknown"),
        "broken": ("ok", "unknown"),
    },
)

INTENT = _spec(
    "intent",
    "intents",
    "One external action, recorded before the platform call and reconciled before any retry.",
    [
        _f("id", "id", "The intent's id, shown as I-<n>."),
        _f(
            "kind",
            "enum",
            "upload, submit, xp, forum, wiki, run or champion.",
            required=True,
            enum=INTENT_KINDS,
        ),
        _f("subject", "text", "The record the action is for, such as V-7 or X-5."),
        _f(
            "key",
            "text",
            "What makes the action unique: the artifact hash, the Version and league, the request "
            "number, the text hash or the event id.",
            required=True,
        ),
        _f("state", "enum", "pending, done or failed.", enum=INTENT_STATES),
        _f("result", "json", "What the platform answered."),
        _created,
        _f("resolved", "timestamp", "When the result was recorded."),
        _created_by,
    ],
    statuses=INTENT_STATES,
    transitions={"pending": ("done", "failed")},
)

NOTICE = _spec(
    "notice",
    "notices",
    "A message to the user through ui.notify; it informs and never asks.",
    [
        _f("id", "id", "The notice's id, shown as N-<n>."),
        _f("text", "text", "The notice, in plain language.", required=True),
        _f("coworld_id", "text", "The Coworld this notice belongs to; null for workspace news."),
        _f("refs", "list", "Records the notice is about."),
        _f("urgency", "enum", "low, normal or high.", enum=("low", "normal", "high")),
        _created,
        _created_by,
        _f("read_at", "timestamp", "When a client marked it read."),
        _f("read_by", "text", "Which client or person marked it read."),
    ],
    store="workspace",
)

SESSION = _spec(
    "session",
    "sessions",
    "A Hermes Session: a chat, a Task run or a wake; its status is computed from the runs API.",
    [
        _f("id", "text", "The Hermes session id, the record's key.", required=True),
        _f("profile", "text", "The Hermes profile the Session runs in.", required=True),
        _f("kind", "enum", "chat, task or wake.", required=True, enum=("chat", "task", "wake")),
        _f("coworld_id", "text", "The Coworld the Session works in, once one is active."),
        _f("task_id", "int", "The Task a task Session holds, in that Coworld's store."),
        _f("goal_id", "int", "The Goal a wake concerns, if any."),
        _f("event", "text", "For wakes, the event that started it."),
        _f("parent", "text", "The Session that started this one."),
        _f("run_id", "text", "The current Hermes run."),
        _f("attempts", "int", "Runs started for this Session, counting resumes."),
        _f("started", "timestamp", "When the Session started."),
        _f("ended", "timestamp", "When it ended."),
        _f("last_seen", "timestamp", "When it last called a tool."),
        _f("session_context", "json", "The client's session context block."),
        _f("client_capabilities", "json", "What the client said it can show."),
        _f("drained", "bool", "Whether a pause or switch stopped it, to resume later."),
        _f("stopped_by", "text", "The watcher's reason when it stopped the run on purpose."),
        _f("status", "text", "running, waiting or stopped, from GET /v1/runs/{id}.", computed=True),
    ],
    store="workspace",
)

COWORLD = _spec(
    "coworld",
    "coworlds",
    "The Coworld registry: one row per Coworld with its folder, mixin and tool-check status.",
    [
        _f(
            "coworld_id",
            "text",
            "The platform Coworld id, the only stored identifier.",
            required=True,
        ),
        _f("folder", "text", "The workspace folder name, the id with underscores.", required=True),
        _f("mixin_version", "text", "The installed mixin's version."),
        _f("tool_check_status", "text", "Whether its commands passed their last test."),
        _f("set_up_at", "timestamp", "When setup finished with passing tool checks."),
        _f("active", "bool", "Whether this is the active Coworld; at most one is."),
        _f("pending_request", "json", "The user request that setup carries out when it ends."),
        _created,
    ],
    store="workspace",
)

RECORDS: dict[str, RecordSpec] = {
    spec.name: spec
    for spec in (
        GOAL,
        TASK,
        VERSION,
        CLAIM,
        EXPERIMENT,
        DIRECTIVE,
        WORKSPACE_DIRECTIVE,
        CHAMPION,
        CAPABILITY,
        INTENT,
        NOTICE,
        SESSION,
        COWORLD,
    )
}


def spec_for_table(table: str) -> RecordSpec | None:
    for spec in RECORDS.values():
        if spec.table == table:
            return spec
    return None


def record_from_row(spec: RecordSpec, row: Mapping[str, Any]) -> dict[str, Any]:
    """A JSON-ready dict of a row: ``id`` in display form, JSON columns parsed."""
    json_names = {f.name for f in spec.json_fields}
    record: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key == "id" and spec.prefix and value is not None:
            value = display_id(spec.prefix, value)
        elif key in json_names and value is not None:
            value = parse_json(value)
        record[key] = value
    return record
