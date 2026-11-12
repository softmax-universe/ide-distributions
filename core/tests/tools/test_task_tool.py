"""The ``task`` tool: every action through run_tool on a workspace with a real code repository."""

from __future__ import annotations

import json

import pytest

import research_core.tools.goal  # noqa: F401  (registers the tool)
import research_core.tools.settings  # noqa: F401
import research_core.tools.task  # noqa: F401
from research_core import gitops
from research_core.store import claims, experiments, sessions, settings, versions
from research_core.store.actors import Actor
from research_core.tools import catalog
from research_core.tools.base import run_tool

GOAL = {"action": "create", "title": "Beat the house", "end_state": "rank first", "reason": "asked"}
EVIDENCE = [{"type": "doc", "ref": "README.md#setup", "stance": "supports", "note": "n"}]
ARTIFACT = {"kind": "file", "path": "dist/policy.bin", "digest": "sha256:" + "0" * 64, "size": 2048}
FAILURE = {"what_failed": "the build", "next_step": "fix the toolchain", "exit_code": 2}


@pytest.fixture
def code_repo(ctx_factory):
    """The active Coworld's code repository with one commit on main."""
    return gitops.init_repo(ctx_factory().coworld().code)


@pytest.fixture
def goal(call):
    call("goal", GOAL)
    return "G-1"


def task_args(**overrides):
    args = {
        "action": "create",
        "goal": "G-1",
        "type": "research",
        "title": "Measure the opening",
        "description": "Run 20 episodes and record what the scouts do.",
    }
    args.update(overrides)
    return args


def claim(call, task="T-1", session_id="r1"):
    from unittest.mock import patch

    record = call("task", {"action": "read", "task": task})["record"]
    if record["status"] == "unclaimed":
        with patch("research_core.coordinator.mint_session_id", return_value=session_id):
            assigned = call(
                "task",
                {
                    "action": "assign",
                    "task": task,
                    "task_updated": record["updated"],
                    "profile": "researcher",
                    "assignment_id": task,
                    "context": "Tool scenario",
                    "rationale": "Research work",
                },
            )
        assert assigned["ok"], assigned
    return call("task", {"action": "claim", "task": task}, role="researcher", session_id=session_id)


def as_researcher(call, args, session_id="r1"):
    return call("task", args, role="researcher", session_id=session_id)


# --- creation, approval, dependencies -----------------------------------------------------


def test_create_is_approved_by_its_creator(call, goal):
    created = call("task", task_args())
    assert created["ok"] is True
    record = created["record"]
    assert record["id"] == "T-1" and record["status"] == "unclaimed"
    assert record["approved_by"] == "session:s1" and record["goal_id"] == 1


def test_create_needs_a_goal_unless_maintenance(call, goal):
    refused = call("task", task_args(goal=None))
    assert refused["ok"] is False and "goal is required" in refused["error"]
    maintenance = call(
        "task", task_args(goal=None, type="maintenance", title="Fix the build", priority="high")
    )
    assert maintenance["ok"] is True and maintenance["record"]["goal_id"] is None


def test_create_reports_every_problem(call, goal):
    refused = call("task", {"action": "create", "type": "research"})
    assert refused["ok"] is False
    assert {"title is required for create", "description is required for create"} <= set(
        refused["problems"]
    )


def test_propose_then_approve_and_reject(call, goal):
    proposed = as_researcher(call, task_args(action="propose"))
    assert proposed["ok"] is True
    assert proposed["record"]["status"] == "proposed"
    assert proposed["record"]["proposed_by"] == "session:r1"

    approved = call("task", {"action": "approve", "task": "T-1"})
    assert approved["record"]["status"] == "unclaimed"
    assert approved["record"]["approved_by"] == "session:s1"

    as_researcher(call, task_args(action="propose", title="Another idea"))
    rejected = call("task", {"action": "reject", "task": "T-2", "next_step": "not this round"})
    assert rejected["record"]["status"] == "abandoned"
    assert rejected["record"]["failure"] == {
        "what_failed": "rejected by session:s1",
        "next_step": "not this round",
    }


def test_gated_type_needs_the_users_word(call, goal):
    gate = call(
        "settings",
        {
            "action": "update",
            "key": "approval_required.task_types",
            "value": ["improvement"],
            "cites": "msg-1",
        },
    )
    assert gate["ok"] is True

    created = call("task", task_args(type="improvement"))
    assert created["record"]["status"] == "proposed"
    assert created["record"]["note"].startswith("stored as proposed")

    refused = call("task", {"action": "approve", "task": "T-1"})
    assert refused["ok"] is False and "only the user approves" in refused["error"]

    approved = call("task", {"action": "approve", "task": "T-1", "cites": "msg-9"})
    assert approved["record"]["status"] == "unclaimed"
    assert approved["record"]["approved_by"] == "user via session:s1 citing msg-9"


def test_link_and_list_filters(call, goal):
    call("task", task_args())
    call("task", task_args(title="Second", type="improvement"))
    linked = call("task", {"action": "link", "task": "T-2", "depends_on": ["T-1"]})
    assert linked["record"]["depends_on"] == ["T-1"]

    assert [t["id"] for t in call("task", {"action": "list"})["records"]] == ["T-1", "T-2"]
    by_type = call("task", {"action": "list", "type": "improvement"}, role="researcher")
    assert [t["id"] for t in by_type["records"]] == ["T-2"]
    by_goal = call("task", {"action": "list", "goal": "G-1", "status": "unclaimed"})
    assert len(by_goal["records"]) == 2
    assert call("task", {"action": "list", "held_by": "r1"})["records"] == []


# --- claiming ---------------------------------------------------------------------------------


def test_claim_makes_the_worktree_and_registers_the_session(call, ctx_factory, goal, code_repo):
    call("task", task_args())
    claimed = claim(call)
    assert claimed["ok"] is True, claimed
    record = claimed["record"]
    assert record["status"] == "in-progress" and record["held_by"] == "r1"
    assert record["lease_epoch"] == 1 and record["branch"] == "task/1"
    assert claimed["branch"] == "task/1"
    assert claimed["worktree"].endswith("worktrees/task-1")
    assert "Research Task" in claimed["instructions"]
    assert claimed["pending_messages"] == []

    worktree = ctx_factory().coworld().worktree_for_task(1)
    assert worktree.is_dir() and gitops.current_branch(worktree) == "task/1"

    session = sessions.read(ctx_factory().wstore, "r1")
    assert session["kind"] == "task" and session["task_id"] == 1
    assert session["coworld_id"] == "cow_fake"


def test_claim_takes_the_run_id_from_the_session_row(call, ctx_factory, goal, code_repo):
    call("task", task_args())
    wstore = ctx_factory().wstore
    sessions.register(
        wstore,
        Actor.harness(),
        coworld_id="cow_fake",
        session_id="r1",
        profile="researcher",
        kind="task",
        task_id=1,
    )
    sessions.set_run(wstore, Actor.harness(), "r1", "run-42")
    claimed = claim(call)
    assert claimed["record"]["run_id"] == "run-42"


def test_claim_again_by_the_holder_renews(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    again = claim(call)
    assert again["ok"] is True and again["record"]["lease_epoch"] == 1


def test_claim_from_a_second_session_is_refused(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    refused = claim(call, session_id="r2")
    assert refused["ok"] is False and "no assignment" in refused["error"]


def test_claim_needs_a_session_and_a_code_repository(call, ctx_factory, goal):
    call("task", task_args())
    no_session = json.loads(
        run_tool(
            "task",
            {"action": "claim", "task": "T-1", "operation_id": "no-session"},
            ctx_factory("researcher", ""),
        )
    )
    assert no_session["ok"] is False and "session" in no_session["error"]
    no_repo = claim(call)
    assert no_repo["ok"] is False and "code repository" in no_repo["error"]


def test_claim_rejects_unapproved_or_blocked_tasks(call, goal, code_repo):
    as_researcher(call, task_args(action="propose"))
    refused = claim(call)
    assert refused["ok"] is False and "no assignment" in refused["error"]


def test_orchestrator_cannot_claim_and_researcher_cannot_create(call, goal):
    denied = call("task", task_args(), role="researcher")
    assert denied["ok"] is False and "not available to the researcher" in denied["error"]
    denied = call("task", {"action": "claim", "task": "T-1"})
    assert denied["ok"] is False and "not available to the orchestrator" in denied["error"]


# --- waits, questions and messages --------------------------------------------------------


def test_a_task_actions_own_tool_goes_through_the_same_transition(call, goal, code_repo):
    """``transitions`` reads the stored arguments' ``action``, so the own tool must supply it."""
    call("task", task_args())
    claim(call)
    waiting = call(
        "task_wait",
        {"task": "T-1", "kind": "process", "detail": "the build", "ref": 4242},
        role="researcher",
        session_id="r1",
    )
    assert waiting["record"]["waiting_on"]["kind"] == "process"


def test_a_user_wait_needs_a_ref_that_can_resolve_it(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    wait = {"action": "wait", "task": "T-1", "kind": "user", "detail": "the user's call"}
    for bad in ({}, {"ref": "the user"}, {"ref": "G-1"}):
        refused = as_researcher(call, wait | bad)
        assert refused["ok"] is False and "a user wait needs ref" in refused["error"]
    for good in ("max_researchers", "T-1"):
        assert as_researcher(call, wait | {"ref": good})["ok"] is True


def test_wait_ask_answer_and_message(call, goal, code_repo):
    call("task", task_args())
    claim(call)

    waiting = as_researcher(
        call,
        {"action": "wait", "task": "T-1", "kind": "process", "detail": "build", "ref": 4242},
    )
    assert waiting["record"]["waiting_on"]["kind"] == "process"
    assert waiting["record"]["waiting_on"]["ref"] == 4242

    asked = as_researcher(call, {"action": "ask", "task": "T-1", "question": "Which seeds?"})
    assert asked["record"]["waiting_on"]["kind"] == "question"

    seen = call("task", {"action": "read", "task": "T-1"})
    assert [m["text"] for m in seen["pending_messages"]] == ["Which seeds?"]

    answered = call("task", {"action": "answer", "task": "T-1", "text": "Seeds 1 to 10."})
    assert answered["record"]["waiting_on"] is None

    noted = call("task", {"action": "message", "task": "T-1", "text": "Also log the timing."})
    assert noted["ok"] is True
    inbox = as_researcher(call, {"action": "read", "task": "T-1"})
    assert [m["text"] for m in inbox["pending_messages"]] == [
        "Seeds 1 to 10.",
        "Also log the timing.",
    ]
    # Reading never marks delivery: the watcher does, when it hands the text to a run.
    assert [m["text"] for m in seen["pending_messages"]] == ["Which seeds?"]


def task_experiment_with_runs(ctx_factory, states: dict[str, str | None]) -> None:
    """X-1 for T-1 (held by r1), with one XP run per request id in the given platform state."""
    ctx, holder = ctx_factory(), Actor.session("r1")
    versions.create(
        ctx.store, holder, summary="baseline", commit_sha="0" * 40, branch="main",
        artifact={"kind": "file", "path": "p", "digest": "0" * 64, "size": 1}, based_on=[],
    )  # fmt: skip
    record = experiments.create(
        ctx.store,
        holder,
        summary="does retreating help",
        task_id=1,
        hypothesis={
            "statement": "s because m",
            "if_true": "t",
            "if_false": "f",
            "motivation": "m",
        },
        methods={
            "baseline": "V-1",
            "procedure": "paired seeds",
            "metrics": [{"name": "score", "direction": "up", "role": "primary"}],
            "decision_rule": {"text": "score up", "n_min": 2},
        },
    )
    experiments.start(ctx.store, holder, record["id"])
    for request_id, state in states.items():
        run = experiments.add_run(
            ctx.store, holder, record["id"], kind="xp", platform_request_id=request_id, arm="a"
        )
        if state is not None:
            experiments.update_run(ctx.store, holder, run["id"], state=state)


def test_an_xp_wait_without_ref_waits_on_the_tasks_open_requests(
    call, ctx_factory, goal, code_repo
):
    call("task", task_args())
    claim(call)
    task_experiment_with_runs(
        ctx_factory, {"xreq_1": "running", "xreq_2": None, "xreq_3": "completed"}
    )
    wait = {"action": "wait", "task": "T-1", "kind": "xp", "detail": "X-1's requests"}
    waiting = as_researcher(call, wait)
    assert waiting["record"]["waiting_on"]["ref"] == ["xreq_1", "xreq_2"]


def test_an_xp_wait_refuses_request_ids_no_run_records(call, ctx_factory, goal, code_repo):
    """A list sent as JSON text is one id the platform never issued: the watcher would poll it
    for good, so the wait is refused and names the ids that work."""
    call("task", task_args())
    claim(call)
    task_experiment_with_runs(ctx_factory, {"xreq_1": "running"})
    wait = {"action": "wait", "task": "T-1", "kind": "xp", "detail": "X-1's requests"}
    for bad in ('["xreq_1"]', ["xreq_1", "xreq_9"]):
        refused = as_researcher(call, wait | {"ref": bad})
        assert refused["ok"] is False and "no Experiment run records" in refused["error"]
        assert "omit ref to wait on T-1's open requests (xreq_1)" in refused["error"]
    assert as_researcher(call, wait | {"ref": "xreq_1"})["record"]["waiting_on"]["ref"] == [
        "xreq_1"
    ]


def test_an_xp_wait_with_nothing_open_is_refused(call, ctx_factory, goal, code_repo):
    call("task", task_args())
    claim(call)
    task_experiment_with_runs(ctx_factory, {"xreq_1": "completed"})
    wait = {"action": "wait", "task": "T-1", "kind": "xp", "detail": "X-1's requests"}
    refused = as_researcher(call, wait)
    assert refused["ok"] is False and "no open XP requests" in refused["error"]


def test_wait_from_a_non_holder_is_refused(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    refused = as_researcher(
        call, {"action": "wait", "task": "T-1", "kind": "xp", "detail": "d"}, session_id="r2"
    )
    assert refused["ok"] is False and "does not hold it" in refused["error"]


def test_release(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    released = as_researcher(call, {"action": "release", "task": "T-1", "reason": "out of time"})
    assert released["kind"] == "release_pending"
    assert released["record"]["status"] == "in-progress"
    assert released["record"]["held_by"] == "r1"


# --- completion and review ------------------------------------------------------------------


def complete_args(**overrides):
    args = {
        "action": "complete",
        "task": "T-1",
        "merge": "no",
        "merge_reason": "nothing to merge",
        "result": "The scouts open left in 12 of 20 episodes.",
    }
    args.update(overrides)
    return args


def test_a_rejection_needs_no_merge_choice(call, ctx_factory, goal, code_repo):
    call("task", task_args())
    claim(call)
    claims.write(
        ctx_factory().store,
        Actor.session("r1"),
        summary="Scouts open left.",
        body="12 of 20 episodes.",
        tags=["opening"],
        evidence=EVIDENCE,
    )
    assert as_researcher(call, complete_args(claims=["C-1"]))["ok"] is True
    rejected = call("task", {"action": "review", "task": "T-1", "accept": False, "reason": "no"})
    assert rejected["ok"] is True, rejected
    assert rejected["record"]["status"] != "complete"


def test_complete_research_task_and_review(call, ctx_factory, goal, code_repo):
    call("task", task_args())
    claim(call)
    claims.write(
        ctx_factory().store,
        Actor.session("r1"),
        summary="Scouts open left.",
        body="12 of 20 episodes.",
        tags=["opening"],
        evidence=EVIDENCE,
    )
    done = as_researcher(call, complete_args(claims=["C-1"]))
    assert done["ok"] is True, done
    record = done["record"]
    assert record["status"] == "complete" and record["outcome"] == "succeeded"
    assert record["checklist"]["claims"] == ["C-1"]
    assert record["checklist"]["merge_request"] == "no"

    reviewed = call(
        "task", {"action": "review", "task": "T-1", "accept": True, "merge": "no", "reason": "ok"}
    )
    assert reviewed["record"]["merge"] == "not merged: ok"


def test_complete_accepts_none_with_a_reason(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    done = as_researcher(call, complete_args(claims="none: nothing found"))
    assert done["ok"] is True, done
    assert done["record"]["checklist"]["claims"] == "none: nothing found"


def test_complete_still_takes_the_nested_checklist_of_an_earlier_release(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    done = as_researcher(call, complete_args(checklist={"claims": "none: nothing found"}))
    assert done["ok"] is True, done
    assert done["record"]["checklist"]["claims"] == "none: nothing found"


def test_complete_names_the_missing_checklist_items(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    refused = as_researcher(call, complete_args())
    assert refused["ok"] is False
    assert any(p.startswith("checklist: claims:") for p in refused["problems"])


def test_complete_with_a_commit_on_the_branch(call, ctx_factory, goal, code_repo):
    call("task", task_args(type="maintenance", goal=None, title="Fix build"))
    claimed = claim(call)
    worktree = ctx_factory().coworld().worktree_for_task(1)
    (worktree / "fix.txt").write_text("fixed\n")
    sha = gitops.commit_all(worktree, "Fix the build")
    done = as_researcher(
        call,
        complete_args(
            commit=sha, merge="yes", merge_reason=None, summary="Fixed the build", result="fixed"
        ),
    )
    assert done["ok"] is True, done
    assert done["record"]["completed_commit"] == sha
    assert claimed["branch"] == "task/1"
    reviewed = call(
        "task", {"action": "review", "task": "T-1", "accept": True, "merge": "yes", "reason": "ok"}
    )
    assert reviewed["record"]["merge"] == "pending"


def test_complete_refuses_a_commit_off_the_branch(call, goal, code_repo):
    call("task", task_args(type="maintenance", goal=None, title="Fix build"))
    claim(call)
    (code_repo / "elsewhere.txt").write_text("on main\n")
    on_main = gitops.commit_all(code_repo, "A commit on main after the claim")
    refused = as_researcher(
        call, complete_args(commit=on_main, merge="yes", summary="s", result="r")
    )
    assert refused["ok"] is False
    assert "not on the Task branch task/1" in refused["error"]


def test_complete_with_outcome_failed(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    failed = as_researcher(
        call, {"action": "complete", "task": "T-1", "outcome": "failed", "failure": FAILURE}
    )
    assert failed["ok"] is True, failed
    assert failed["record"]["outcome"] == "failed" and failed["record"]["failure"] == FAILURE
    missing = as_researcher(call, {"action": "complete", "task": "T-1", "outcome": "failed"})
    assert missing["ok"] is False and "failure" in missing["error"]


def test_ideation_completion_needs_two_to_five_proposals(call, goal, code_repo):
    call("task", task_args(type="ideation", title="Ideas"))
    claim(call)
    as_researcher(call, task_args(action="propose", title="Idea one"))
    refused = as_researcher(call, complete_args(proposed_tasks=["T-2"], result="one idea"))
    assert refused["ok"] is False and "at least 2" in refused["error"]
    as_researcher(call, task_args(action="propose", title="Idea two"))
    done = as_researcher(call, complete_args(proposed_tasks=["T-2", "T-3"], result="two ideas"))
    assert done["ok"] is True, done


def test_review_rejection_reopens(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    as_researcher(call, complete_args(claims="none: nothing yet"))
    reopened = call(
        "task",
        {"action": "review", "task": "T-1", "accept": False, "merge": "no", "reason": "too thin"},
    )
    assert reopened["record"]["status"] == "in-progress"
    inbox = as_researcher(call, {"action": "read", "task": "T-1"})
    assert inbox["pending_messages"][-1]["text"] == "Reopened: too thin"


def test_propose_submission(call, ctx_factory, goal, code_repo):
    call("task", task_args(type="improvement"))
    claim(call)
    versions.create(
        ctx_factory().store,
        Actor.harness(),
        summary="policy 1",
        commit_sha="a" * 40,
        branch="main",
        artifact=ARTIFACT,
        based_on=[],
    )
    proposed = as_researcher(
        call,
        {"action": "propose_submission", "task": "T-1", "version": "V-1", "reason": "beats it"},
    )
    assert proposed["ok"] is True, proposed
    assert "Proposes V-1 for submission" in proposed["record"]["messages"][-1]["text"]
    missing = as_researcher(
        call, {"action": "propose_submission", "task": "T-1", "version": "V-9", "reason": "x"}
    )
    assert missing["ok"] is False and "V-9 does not exist" in missing["error"]


def test_abandon_a_held_task(call, goal, code_repo):
    call("task", task_args())
    claim(call)
    abandoned = call("task", {"action": "abandon", "task": "T-1", "failure": FAILURE})
    assert abandoned["record"]["status"] == "abandoned"
    assert abandoned["record"]["held_by"] is None
    assert abandoned["record"]["failure"] == FAILURE
    refused = call("task", {"action": "abandon", "task": "T-1", "failure": {"what_failed": "x"}})
    assert refused["ok"] is False and "failure.next_step is required" in refused["problems"]


def test_results_never_show_the_store(call, ctx_factory, goal, code_repo):
    call("task", task_args())
    for result in (claim(call), call("task", {"action": "read", "task": "T-1"})):
        text = json.dumps(result)
        assert ".state" not in text  # the worktree path is wanted; the store path never


def test_missing_task_is_a_clean_refusal(call, goal):
    refused = call("task", {"action": "read", "task": "T-9"})
    assert refused["ok"] is False and "T-9" in refused["error"]
    bad = call("task", {"action": "read", "task": "G-1"})
    assert bad["ok"] is False and "task reference" in bad["error"]


def test_settings_gate_reads_the_coworld_store(call, ctx_factory, goal):
    gate = {"action": "update", "key": "approval_required.task_types", "cites": "m"}
    call("settings", {**gate, "value": ["ideation"]})
    assert settings.get(ctx_factory().store, "approval_required.task_types") == ["ideation"]


def test_a_proposal_offers_no_ideation_round():
    """The store allows it only on an ideation Task, and a round is set when that is created."""
    spec = catalog.TOOLS["task"]
    assert "ideation_round" not in spec.accepted(spec.action("propose"))
    assert "ideation_round" in spec.accepted(spec.action("create"))
