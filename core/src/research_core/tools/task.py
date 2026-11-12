"""The ``task`` tool (design B.4, B.12, B.13, C.2): the unit of work both roles act on.

The store module holds every rule; this module maps the flat tool arguments onto it and adds
the two things only the tool can do: ``claim`` creates the Task's worktree and registers the
Session, and ``complete`` checks that a given commit is on the Task branch. Stdlib only: it
runs inside Hermes (A3.1), so the git work goes through ``gitops`` (subprocess).

Messages. ``read`` adds the undelivered messages addressed to the caller's role and ``claim``
adds those for the Researcher. Neither marks them delivered: that is the watcher's, which
steers or resumes the holder's run with them (entry 5).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from research_core import gitops, refs, task_types
from research_core.coordinator import Coordinator
from research_core.store import admissions, experiments, sessions, settings, tasks
from research_core.store.registry import TASK_STATUSES, TASK_TYPES, WAIT_KINDS
from research_core.tools import _args as a
from research_core.tools.base import ActionSpec, ToolCall, ToolContext, ToolError, ToolSpec
from research_core.transitions import NOT_THE_HOLDER, durable_tool, read_command, submit
from research_core.watcher.context import workers

ORCHESTRATOR = frozenset({"orchestrator"})
RESEARCHER = frozenset({"researcher"})
BOTH = frozenset({"orchestrator", "researcher"})

TASK = a.string("The Task, such as T-3.")
YES_NO = ["yes", "no"]
# The kinds a call chooses: task_ask sets a question wait, a platform error a platform wait.
TASK_WAIT_KINDS = [kind for kind in WAIT_KINDS if kind not in ("question", "platform")]
EXPERIMENT = a.string("The Experiment the Task runs, such as X-2.")
# Arguments of ``complete`` that are the checklist entries of the same name
# (the keys of the Task types' checklists, task_types/*.md, besides commit and
# submission_candidate, which are fields of their own).
CHECKLIST_FIELDS = {
    "claims": a.any_of(
        'Claims recorded, such as ["C-3"], or "none: <reason>" where the type allows it.',
        a.STRINGS,
        a.STRING,
    ),
    "experiment": EXPERIMENT,
    "versions": a.strings('Versions tested, such as ["V-4"].'),
    "proposed_tasks": a.strings('An ideation Task\'s proposals, such as ["T-7"].'),
    "summary": a.string("A maintenance Task's summary of the change."),
    "submission_reason": a.string("Why a Version is or is not a submission candidate."),
    "capability": a.string("The capability a repair Task restored."),
}

FAILURE = a.obj(
    "",
    {
        "what_failed": a.string("What failed."),
        "next_step": a.string("What should happen next."),
        "error": a.string("The error message, if any."),
        "exit_code": a.integer("The exit code, if any."),
        "tried": a.string("What was tried already."),
    },
    required=tasks.FAILURE_REQUIRED,
    closed=True,
)
CITES = a.string(
    "The user's message that asked for it; needed for a Task type that requires the user's "
    "approval."
)
CREATE_PARAMS = {
    "goal": a.string("The Goal, such as G-3; only a maintenance Task may have none."),
    "type": a.string("The Task type.", enum=list(TASK_TYPES)),
    "title": a.string("Short name, for the agents."),
    "description": a.string("What to do and deliver, in Markdown."),
    "reasoning": a.string("Why, with references to the Goal, claims and Experiments."),
    "for_human": a.string("One plain sentence that clients show."),
    "end_condition": a.string(
        "How the Task ends. Sending it requires end_detail too.",
        enum=["deliverable", "steps", "effort"],
    ),
    "end_detail": a.string("What done means under end_condition. Required with end_condition."),
    "experiment": a.string("The Experiment an improvement Task runs, such as X-2."),
    "priority": a.string("Default normal.", enum=["normal", "high"]),
    "depends_on": a.strings('Tasks that must complete first, such as ["T-1"].'),
}
# Nothing reads a round number and only an ideation Task may carry one, so it is not offered; a
# chat keeps the schema it started with until the next release's refresh.
CREATE_HIDDEN = {"ideation_round": {"type": "integer"}}


# --- handlers -----------------------------------------------------------------------------


def handle_create(call: ToolCall) -> dict:
    """``create`` (Orchestrator; accepted unless the type is gated) and ``propose`` (both)."""
    store, actor = call.ctx.store, call.ctx.actor
    goal = call.get("goal")
    if goal is None and call.get("type") != "maintenance":
        raise ToolError("goal is required unless type is maintenance")
    fields = {
        "type": call.get("type"),
        "title": call.get("title"),
        "description": call.get("description"),
        "goal_id": None if goal is None else a.record_number(goal, "goal", "goal"),
        "reasoning": call.get("reasoning") or "",
        "for_human": call.get("for_human") or "",
        "end_condition": call.get("end_condition"),
        "end_detail": call.get("end_detail"),
        "priority": call.get("priority") or "normal",
        "depends_on": a.record_numbers(call.get("depends_on"), "task", "depends_on"),
        "ideation_round": call.get("ideation_round") if call.get("type") == "ideation" else None,
    }
    if call.get("experiment") is not None:
        fields["experiment_id"] = a.record_number(
            call.get("experiment"), "experiment", "experiment"
        )
    if call.action == "propose":
        return {"record": tasks.propose(store, actor, **fields)}
    return {"record": tasks.create(store, actor, cites=call.get("cites"), **fields)}


def handle_list(call: ToolCall) -> dict:
    goal = call.get("goal")
    records = tasks.list(
        call.ctx.store,
        status=call.get("status"),
        goal_id=None if goal is None else a.record_number(goal, "goal", "goal"),
        type=call.get("type"),
        held_by=call.get("held_by"),
    )
    # A list is an index, not every Task's full transcript and research plan. ``read`` keeps
    # the complete record; the revision here still protects a subsequent assignment.
    return {
        "records": [
            {
                key: record[key]
                for key in (
                    "id",
                    "title",
                    "for_human",
                    "type",
                    "goal_id",
                    "status",
                    "outcome",
                    "priority",
                    "held_by",
                    "waiting_on",
                    "depends_on",
                    "stuck",
                    "updated",
                )
            }
            for record in records
        ]
    }


def handle_claim(call: ToolCall) -> dict:
    """Take the lease: the worktree on ``task/<n>`` from main, the Session row, the Task.

    The run id comes from the Session row the watcher wrote (A3.7), never from the environment.
    The result carries the type's instructions and the messages waiting for the Researcher.
    """
    ctx = call.ctx
    session_id = ctx.hermes_session_id()
    if not session_id:
        raise ToolError(
            "claim needs the Hermes session id (IDE_SESSION_ID); a Task is held by a Session"
        )
    n = a.record_number(call.get("task"), "task", "task")
    coworld = ctx.coworld()
    if not (coworld.code / ".git").exists():
        raise ToolError(
            "the Coworld's code repository is not set up (code/ is not a git repository);"
            " the setup wake creates it, so end your run"
        )
    with admissions.dispatch_lock(ctx.wstore):
        assignment = ctx.wstore.one(
            "SELECT * FROM task_assignments WHERE coworld_id=? AND task_id=?",
            (coworld.coworld_id, n),
        )
        if assignment is None or assignment["session_id"] != session_id:
            raise ToolError(
                f"this Session has no assignment for T-{n}, and only the Orchestrator assigns "
                "Tasks (task_ask needs a Task you hold); end your run"
            )
        branch = f"task/{n}"
        try:
            worktree = gitops.worktree_add(
                coworld.code, coworld.worktree_for_task(n), branch=branch, base="main"
            )
        except gitops.GitError as error:
            raise ToolError(
                f"could not create the worktree for {branch}: {error}",
                command=error.command,
                exit_code=error.exit_code,
                stderr_tail=error.stderr[-2000:],
            ) from None
        session = sessions.read(ctx.wstore, session_id)
        run_id = (session or {}).get("run_id") or ""
        record = tasks.claim(
            ctx.store, ctx.actor, n, run_id=run_id, worktree=str(worktree), branch=branch
        )
        sessions.register(
            ctx.wstore,
            ctx.actor,
            session_id=session_id,
            profile=assignment["profile"],
            kind="task",
            coworld_id=coworld.coworld_id,
            task_id=n,
            goal_id=record["goal_id"],
        )
    return {
        "record": record,
        "worktree": str(worktree),
        "branch": branch,
        "instructions": task_types.load(record["type"]).body,
        "pending_messages": tasks.pending_messages(ctx.store, n, "to_researcher"),
    }


def _complete_fields(args: Mapping[str, Any]) -> dict[str, Any]:
    """The flat arguments as ``tasks.complete`` takes them: the checklist's are folded into it."""
    checklist = dict(args.get("checklist") or {})
    for key in CHECKLIST_FIELDS:
        if args.get(key) is not None:
            checklist.setdefault(key, args.get(key))
    return {
        "checklist": checklist,
        "commit": args.get("commit"),
        "merge_request": args.get("merge"),
        "merge_reason": args.get("merge_reason") or "",
        "submission_candidate": args.get("submission_candidate"),
        "result": args.get("result"),
        "outcome": args.get("outcome") or "succeeded",
        "failure": args.get("failure"),
    }


def handle_complete(call: ToolCall) -> dict:
    """Finish the Task. The checklist is the flat arguments of that name; a ``commit`` must be
    reachable from the Task branch."""
    ctx, store = call.ctx, call.ctx.store
    n = a.record_number(call.get("task"), "task", "task")
    commit = call.get("commit")
    if commit is not None:
        branch = tasks.read(store, n)["branch"]
        if not branch or not gitops.commit_on_branch(ctx.coworld().code, commit, branch):
            raise ToolError(
                f"commit {commit} is not on the Task branch {branch or '(none: not claimed)'};"
                " commit in the Task's worktree and pass that sha"
            )
    return {"record": tasks.complete(store, ctx.actor, n, **_complete_fields(call.args))}


def handle(call: ToolCall) -> dict:
    """Every action that takes one Task and maps straight onto the store."""
    ctx, store, actor = call.ctx, call.ctx.store, call.ctx.actor
    n = a.record_number(call.get("task"), "task", "task")
    if call.action == "read":
        direction = "to_orchestrator" if ctx.role == "orchestrator" else "to_researcher"
        return {
            "record": tasks.read(store, n),
            "pending_messages": tasks.pending_messages(store, n, direction),
        }
    if call.action == "approve":
        return {"record": tasks.approve(store, actor, n, cites=call.get("cites"))}
    if call.action == "link":
        deps = a.record_numbers(call.get("depends_on"), "task", "depends_on")
        return {"record": tasks.link(store, actor, n, deps)}
    if call.action == "answer":
        record = tasks.answer(
            store, actor, n, text=call.get("text"), question_id=call.get("question_id")
        )
        return {"record": record}
    if call.action == "message":
        record = tasks.message(store, actor, n, text=call.get("text"), direction="to_researcher")
        return {"record": record}
    if call.action == "abandon":
        return {"record": tasks.abandon(store, actor, n, failure=call.get("failure"))}
    if call.action == "review":
        record = tasks.review(
            store,
            actor,
            n,
            accept=call.get("accept"),
            merge=call.get("merge"),
            reason=call.get("reason"),
        )
        return {"record": record}
    if call.action == "reject":
        return {"record": tasks.reject(store, actor, n, next_step=call.get("next_step"))}
    if call.action == "wait":
        ref = call.get("ref")
        if call.get("kind") == "user" and not (
            ref in settings.SETTINGS or refs.parse(str(ref)).record_kind == "task"
        ):
            raise ToolError(
                "a user wait needs ref: the setting or Task (T-n) the user's answer changes; "
                "nothing else ends it. For a decision, use task_ask"
            )
        if call.get("kind") == "xp":
            ref = xp_wait_refs(store, n, ref)
            tracked = {
                row[0]
                for row in store.q(
                    "SELECT item.value FROM task_xp_waits, json_each(request_ids) item"
                    " WHERE task_id=?",
                    (n,),
                )
            }
            ref = [request_id for request_id in ref if request_id not in tracked]
            if not ref:
                return {
                    "record": tasks.read(store, n),
                    "next": "AIDE already follows those requests.",
                }
        record = tasks.wait(
            store,
            actor,
            n,
            kind=call.get("kind"),
            detail=call.get("detail"),
            ref=ref,
        )
        return {"record": record}
    if call.action == "ask":
        return {"record": tasks.ask(store, actor, n, question=call.get("question"))}
    if call.action == "release":
        return Coordinator.request_release(ctx, n, call.get("reason"))
    if call.action == "propose_submission":
        version = a.record_number(call.get("version"), "version", "version")
        record = tasks.propose_submission(
            store, actor, n, version_id=version, reason=call.get("reason")
        )
        return {"record": record}
    raise AssertionError(call.action)


def xp_wait_refs(store, task_id: int, ref: Any) -> list[str]:
    """The request ids an xp wait watches: those given, or by default the open requests of the
    Task's Experiments.

    Every id given must be one an Experiment run records. The watcher polls each id until it is
    terminal, so an id the platform never issued (a typo, or a list sent as JSON text such as
    '["xreq_1"]') used to keep the Task asleep for good; it is refused here instead, naming the
    ids that would work.
    """
    open_ids = experiments.open_requests(store, task_id)
    if ref in (None, "", []):
        if not open_ids:
            raise ToolError(
                f"T-{task_id}'s Experiments have no open XP requests, so nothing would end this"
                " wait; xp.status {experiment} reads finished ones, or give ref: the request ids"
            )
        return open_ids
    given = [str(item) for item in (ref if isinstance(ref, list) else [ref])]
    recorded = experiments.recorded_requests(store, given)
    unknown = [item for item in given if item not in recorded]
    if unknown:
        raise ToolError(
            f"no Experiment run records the XP request {', '.join(map(repr, unknown))}; give ref"
            " as a list of request ids from xp.request, or omit ref to wait on"
            f" T-{task_id}'s open requests ({', '.join(open_ids) or 'none'})",
            open_requests=open_ids,
        )
    return given


def execute(call: ToolCall) -> dict:
    if call.ctx.role == "researcher" and call.action in {
        "complete",
        "wait",
        "ask",
        "release",
        "propose_submission",
    }:
        n = a.record_number(call.get("task"), "task", "task")
        tasks.require_holder(call.ctx.store, n, call.ctx.actor)
        assignment = call.ctx.wstore.one(
            "SELECT a.*,execution.status FROM task_assignments a"
            " JOIN client_admissions execution ON execution.op_id=a.current_attempt_id"
            " WHERE a.session_id=? AND a.task_id=?",
            (call.ctx.hermes_session_id(), n),
        )
        accepted_attempt = call.ctx.env.get("IDE_TRANSITION_ATTEMPT_ID")
        if assignment is None or (
            assignment["current_attempt_id"] != accepted_attempt
            if accepted_attempt
            else assignment["status"] not in {"queued", "running"}
        ):
            raise ToolError(NOT_THE_HOLDER)
    if call.action == "workers":
        return {"kind": "available_workers", "workers": workers(call.ctx.env)}
    if call.action == "assign":
        n = a.record_number(call.get("task"), "task", "task")
        return Coordinator.reserve(
            call.ctx,
            task_id=n,
            task_updated=call.get("task_updated")
            or tasks.revision_for_assignment(call.ctx.store, call.ctx.hermes_session_id(), n),
            profile=call.get("profile"),
            # The command's id repeats on recovery and differs for a later assignment of the Task.
            assignment_id=call.get("assignment_id") or call.ctx.env["IDE_TRANSITION_ID"],
            context=call.get("context"),
            rationale=call.get("rationale"),
        )
    if call.action in ("create", "propose"):
        return handle_create(call)
    if call.action == "list":
        return handle_list(call)
    if call.action == "claim":
        return handle_claim(call)
    if call.action == "complete":
        return handle_complete(call)
    return handle(call)


def dispatch(call: ToolCall) -> dict:
    if call.action == "command":
        return read_command(call.ctx, call.get("command_id"))
    if call.action not in {"read", "list", "workers"} and call.ctx.coordinator_instance is None:
        return submit(call)
    result = execute(call)
    remember_seen_revisions(call.ctx, result)
    return result


def remember_seen_revisions(ctx: ToolContext, result: dict) -> None:
    """Note the Task revisions this result shows the Orchestrator; ``assign`` checks them."""
    records = result.get("records") or ([result["record"]] if "record" in result else [])
    session_id = ctx.hermes_session_id()
    if ctx.role == "orchestrator" and session_id and records:
        tasks.note_seen(ctx.store, ctx.actor, session_id, records)


# --- the spec ---------------------------------------------------------------------------------

SPEC = durable_tool(
    ToolSpec(
        name="task",
        description=(
            "Tasks: units of work under a Goal with a type checklist, held by one Researcher "
            "Session, with waits and messages."
        ),
        handler=dispatch,
        roles=BOTH,
        executor="inproc",
        actions=(
            ActionSpec(
                "command",
                "Read the result of a call that came back pending.",
                {"command_id": a.string("The command_id that pending call returned.")},
                ("command_id",),
                BOTH,
            ),
            ActionSpec(
                "workers",
                "Inspect available research workers before choosing an assignment.",
                {},
                (),
                ORCHESTRATOR,
            ),
            ActionSpec(
                "assign",
                "Give an accepted Task whose dependencies are done to the chosen worker.",
                {
                    "task": TASK,
                    "profile": a.string(
                        "A profile returned by task.workers; choose by capabilities."
                    ),
                    "context": a.string("The context and instructions this worker needs."),
                    "rationale": a.string("Why this worker fits the task and dependencies."),
                },
                ("task", "profile", "context", "rationale"),
                ORCHESTRATOR,
                hidden_params={
                    "task_updated": {"type": "string"},
                    "assignment_id": {"type": "string"},
                },
            ),
            ActionSpec(
                "create",
                "Create an accepted Task (stored as proposed when its type needs the user's "
                "approval and no cites is given).",
                {
                    **CREATE_PARAMS,
                    "cites": CITES,
                },
                ("type", "title", "description"),
                ORCHESTRATOR,
                example_fields=("goal",),
                hidden_params=CREATE_HIDDEN,
            ),
            ActionSpec(
                "propose",
                "Suggest a Task for the Orchestrator to approve or reject.",
                CREATE_PARAMS,
                ("type", "title", "description"),
                RESEARCHER,
                example_fields=("goal",),
                own_tool=True,
            ),
            ActionSpec(
                "approve",
                "Accept a proposed Task so it can be assigned.",
                {"task": TASK, "cites": CITES},
                ("task",),
                ORCHESTRATOR,
            ),
            ActionSpec(
                "link",
                "Make a Task wait for others to complete.",
                {"task": TASK, "depends_on": a.strings('Tasks it depends on, such as ["T-1"].')},
                ("task", "depends_on"),
                ORCHESTRATOR,
            ),
            ActionSpec(
                "answer",
                "Answer a Researcher's question; the Task's question wait resolves.",
                {
                    "task": TASK,
                    "text": a.string("The answer."),
                    "question_id": a.integer("Which question, when more than one is open."),
                },
                ("task", "text"),
                ORCHESTRATOR,
            ),
            ActionSpec(
                "message",
                "Send the Researcher a note: the watcher delivers it to a live run, resumes a "
                "stopped run with it, or holds it until a wait resolves. A Task with no holder "
                "(unclaimed or ended) never gets it: use ui.notify.",
                {"task": TASK, "text": a.string("The note.")},
                ("task", "text"),
                ORCHESTRATOR,
            ),
            ActionSpec(
                "abandon",
                "Drop an open Task, held or not; a held run is stopped by the watcher.",
                {"task": TASK, "failure": {**FAILURE, "description": "Why it is dropped."}},
                ("task", "failure"),
                ORCHESTRATOR,
                own_tool=True,
            ),
            ActionSpec(
                "review",
                "Judge a succeeded Task: accept (and merge yes|no) or send it back.",
                {
                    "task": TASK,
                    "accept": a.boolean("true to accept, false to reopen with the reason."),
                    "merge": a.string(
                        "When accepting: yes merges the commit the Task recorded, no leaves it "
                        "unmerged.",
                        enum=YES_NO,
                    ),
                    "reason": a.string("The verdict's reason."),
                },
                ("task", "accept", "reason"),
                ORCHESTRATOR,
                example_fields=("merge",),
            ),
            ActionSpec(
                "reject",
                "Turn down a proposed Task.",
                {"task": TASK, "next_step": a.string("What to do instead.")},
                ("task", "next_step"),
                ORCHESTRATOR,
            ),
            ActionSpec(
                "claim",
                "Take the Task: its worktree is created and the instructions returned.",
                {"task": TASK},
                ("task",),
                RESEARCHER,
            ),
            ActionSpec(
                "wait",
                "Record what the Task waits on, then end the run; the watcher resumes it.",
                {
                    "task": TASK,
                    "kind": a.string("What kind of thing it waits on.", enum=TASK_WAIT_KINDS),
                    "detail": a.string("What exactly it waits for."),
                    "ref": a.any_of(
                        "What to watch: a pid (process), request ids (xp; omit for the Task's "
                        "open requests), a policy version id "
                        "(submission), a route (permit), a setting key or Task (user; required: "
                        "nothing else ends a user wait).",
                        a.STRING,
                        a.INTEGER,
                        a.STRINGS,
                    ),
                },
                ("task", "kind", "detail"),
                RESEARCHER,
                own_tool=True,
            ),
            ActionSpec(
                "ask",
                "Ask the Orchestrator a question and wait for the answer.",
                {"task": TASK, "question": a.string("The question.")},
                ("task", "question"),
                RESEARCHER,
                own_tool=True,
            ),
            ActionSpec(
                "complete",
                "Finish the Task with its type's checklist (succeeded) or a failure record.",
                {
                    "task": TASK,
                    "outcome": a.string("Default succeeded.", enum=["succeeded", "failed"]),
                    **CHECKLIST_FIELDS,
                    "commit": a.string("The commit to merge; it must be on the Task branch."),
                    "submission_candidate": a.string(
                        "Whether a tested Version should be submitted.", enum=YES_NO
                    ),
                    "merge": a.string(
                        "yes asks to merge commit, no needs merge_reason; required when succeeded.",
                        enum=YES_NO,
                    ),
                    "merge_reason": a.string("Why nothing is merged."),
                    "result": a.string("What came of the Task; required when succeeded."),
                    "failure": {**FAILURE, "description": "Required when outcome is failed."},
                },
                ("task",),
                RESEARCHER,
                example_fields=("commit", "merge", "result"),
                # A command accepted before the release that removed it replays with it.
                hidden_params={"checklist": {"type": "object"}},
                # In the grouped schema 102 of 280 calls in a week (2026-11) were refused: they
                # sent task.list's fields (held_by, status, type, goal) and left out task.
                own_tool=True,
            ),
            ActionSpec(
                "release",
                "Give the Task back unfinished because you must not continue, such as when a "
                "directive forbids the only path; your run is stopped, then it goes back to "
                "scheduling. If you tried and it did not work, use task_complete with outcome "
                "failed.",
                {"task": TASK, "reason": a.string("Why you must not continue.")},
                ("task", "reason"),
                RESEARCHER,
                own_tool=True,
            ),
            ActionSpec(
                "propose_submission",
                "Propose a Version for submission; the Orchestrator applies the rules.",
                {
                    "task": TASK,
                    "version": a.string("The Version, such as V-4."),
                    "reason": a.string("Why it should be submitted."),
                },
                ("task", "version", "reason"),
                RESEARCHER,
                own_tool=True,
            ),
            ActionSpec(
                "read",
                "Read a Task with its messages; adds the messages waiting for you.",
                {"task": TASK},
                ("task",),
            ),
            ActionSpec(
                "list",
                "List Task summaries (id, title, status, holder, waits and revision), filtered "
                "by status, Goal, type or holder. Use read for a Task's full plan and messages.",
                {
                    "status": a.string("Only Tasks in this status.", enum=list(TASK_STATUSES)),
                    "goal": a.string("Only this Goal's Tasks."),
                    "type": a.string("Only Tasks of this type.", enum=list(TASK_TYPES)),
                    "held_by": a.string("Only Tasks this session id holds."),
                },
            ),
        ),
    )
)
