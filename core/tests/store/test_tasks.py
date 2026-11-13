"""Tasks: creation rules, approval, waits, messages, completion checklists, review (design B.4).

Goals, claims, Experiments and Versions are inserted as raw rows here: these tests cover the
Task rules alone and must not depend on the sibling record modules.
"""

from __future__ import annotations

import pytest

from research_core.store import settings, tasks
from research_core.store.actors import Actor
from research_core.store.errors import LeaseError, NotFound, StoreError

SHA = "0123abc4567def890123abc4567def8901234567"
GOOD_FAILURE = {
    "what_failed": "the build broke on the new toolchain",
    "error": "nim: command not found",
    "exit_code": 127,
    "tried": "reinstalled the toolchain twice",
    "next_step": "open a repair Task for the nim toolchain",
}


def make_goal(store, actor, title="Reach division 1") -> int:
    with store.tx(actor) as tx:
        cursor = tx.execute(
            "INSERT INTO goals(title, end_state, reason, status, created, created_by, updated)"
            " VALUES (?, ?, 'the user asked', 'active', ?, ?, ?)",
            (title, "the champion sits in division 1", tx.at, str(actor), tx.at),
        )
        return cursor.lastrowid


def make_claim(store, actor, summary="Greedy opening wins early fights") -> int:
    with store.tx(actor) as tx:
        cursor = tx.execute(
            "INSERT INTO claims(summary, status, created, created_by, updated)"
            " VALUES (?, 'active', ?, ?, ?)",
            (summary, tx.at, str(actor), tx.at),
        )
        return cursor.lastrowid


def make_experiment(store, actor, task_id=None) -> int:
    with store.tx(actor) as tx:
        cursor = tx.execute(
            "INSERT INTO experiments(summary, task_id, status, created, created_by)"
            " VALUES ('does X beat the champion', ?, 'proposed', ?, ?)",
            (task_id, tx.at, str(actor)),
        )
        return cursor.lastrowid


def make_version(store, actor) -> int:
    with store.tx(actor) as tx:
        cursor = tx.execute(
            "INSERT INTO versions(summary, source, commit_sha, artifact, created, created_by)"
            " VALUES ('tuned opening', 'built', ?, '{}', ?, ?)",
            (SHA, tx.at, str(actor)),
        )
        return cursor.lastrowid


def make_task(store, actor, goal, **fields) -> dict:
    fields.setdefault("type", "research")
    fields.setdefault("title", "Study the opening")
    fields.setdefault("description", "Read the replays and record what wins the first fight.")
    return tasks.create(store, actor, goal_id=goal, **fields)


def claim(store, actor, task, n=1, **fields) -> dict:
    fields.setdefault("run_id", f"run-{n}")
    fields.setdefault("worktree", f"/w/task-{n}")
    fields.setdefault("branch", f"task/{n}")
    return tasks.claim(store, actor, task, **fields)


def claimed_task(store, creator, holder, goal, **fields) -> dict:
    task = make_task(store, creator, goal, **fields)
    return claim(store, holder, task["id"])


def kinds(store) -> list[str]:
    return [event.kind for event in store.events_after(0)]


@pytest.fixture
def goal(coworld_store, harness) -> int:
    return make_goal(coworld_store, harness)


@pytest.fixture
def researcher() -> Actor:
    return Actor.session("api_1761690300_researcher1")


@pytest.fixture
def user() -> Actor:
    return Actor.user("james")


# --- creation -----------------------------------------------------------------------------


def test_create_unclaimed_is_approved_by_its_creator(coworld_store, session_actor, goal):
    task = make_task(coworld_store, session_actor, goal, reasoning="G-1 needs it", priority="high")
    assert task["id"] == "T-1"
    assert task["goal_id"] == goal
    assert task["status"] == "unclaimed"
    assert task["type"] == "research"
    assert task["priority"] == "high"
    assert task["approved_by"] == str(session_actor)
    assert task["approved_at"] == "2026-10-28T21:04:05.123456Z"
    assert task["proposed_by"] is None
    assert task["created_by"] == str(session_actor)
    assert task["lease_epoch"] == 0
    assert task["depends_on"] == []
    assert task["sessions"] == []
    assert task["messages"] == []
    assert task["stuck"] is False
    assert "note" not in task
    events = coworld_store.events_after(0)
    assert [(e.kind, e.subject) for e in events] == [("task.created", "T-1")]
    assert events[0].payload["status"] == "unclaimed"
    assert events[0].payload["type"] == "research"


def test_create_reports_every_problem_at_once(coworld_store, harness):
    with pytest.raises(StoreError) as info:
        tasks.create(
            coworld_store,
            harness,
            goal_id=99,
            type="chore",
            title="",
            description="x",
            priority="urgent",
            end_condition="deliverable",
            status="done",
        )
    problems = info.value.problems
    assert any("type" in p and "chore" in p for p in problems)
    assert any("title" in p for p in problems)
    assert any("G-99 does not exist" in p for p in problems)
    assert any("priority" in p for p in problems)
    assert any("end_detail" in p for p in problems)
    assert any("status" in p for p in problems)
    assert len(problems) == 6
    assert coworld_store.one("SELECT count(*) FROM tasks")[0] == 0


def test_display_ids_are_accepted_for_goal_and_task(coworld_store, harness, goal):
    task = make_task(coworld_store, harness, f"G-{goal}")
    assert tasks.read(coworld_store, "T-1")["id"] == task["id"]
    with pytest.raises(StoreError, match="not a Task id"):
        tasks.read(coworld_store, "G-1")
    with pytest.raises(NotFound, match="T-9 does not exist"):
        tasks.read(coworld_store, 9)


def test_only_maintenance_may_have_no_goal_and_a_capability(coworld_store, harness, goal):
    repair = tasks.create(
        coworld_store,
        harness,
        type="maintenance",
        title="Repair the build",
        description="Reinstall the toolchain and rerun the check.",
        capability="build",
        priority="high",
    )
    assert repair["goal_id"] is None
    assert repair["capability"] == "build"
    with pytest.raises(StoreError) as info:
        tasks.create(coworld_store, harness, type="research", title="t", description="d")
    assert any("goal_id is required" in p for p in info.value.problems)
    with pytest.raises(StoreError) as info:
        make_task(coworld_store, harness, goal, capability="build")
    assert any("capability" in p and "maintenance" in p for p in info.value.problems)


def test_improvement_carries_at_most_one_existing_experiment(coworld_store, harness, goal):
    experiment = make_experiment(coworld_store, harness)
    with pytest.raises(StoreError) as info:
        make_task(coworld_store, harness, goal, experiment_id=experiment)
    assert any("improvement" in p for p in info.value.problems)
    with pytest.raises(StoreError) as info:
        make_task(coworld_store, harness, goal, type="improvement", experiment_id=7)
    assert any("X-7 does not exist" in p for p in info.value.problems)
    task = make_task(coworld_store, harness, goal, type="improvement", experiment_id=experiment)
    assert task["experiment_id"] == experiment


def test_at_most_one_open_ideation_task_per_goal(coworld_store, harness, goal):
    first = make_task(coworld_store, harness, goal, type="ideation", ideation_round=1)
    with pytest.raises(StoreError) as info:
        make_task(coworld_store, harness, goal, type="ideation", ideation_round=2)
    assert any("open ideation Task" in p and first["id"] in p for p in info.value.problems)
    other_goal = make_goal(coworld_store, harness, "Another goal")
    make_task(coworld_store, harness, other_goal, type="ideation")
    tasks.abandon(coworld_store, harness, first["id"], failure=GOOD_FAILURE)
    second = make_task(coworld_store, harness, goal, type="ideation", ideation_round=2)
    assert second["ideation_round"] == 2
    with pytest.raises(StoreError) as info:
        make_task(coworld_store, harness, goal, ideation_round=3)
    assert any("ideation_round" in p for p in info.value.problems)


def test_depends_on_at_creation_must_exist(coworld_store, harness, goal):
    first = make_task(coworld_store, harness, goal)
    with pytest.raises(StoreError) as info:
        make_task(coworld_store, harness, goal, depends_on=[first["id"], "T-9"])
    assert any("T-9 does not exist" in p for p in info.value.problems)
    second = make_task(coworld_store, harness, goal, depends_on=[first["id"]])
    assert second["depends_on"] == [first["id"]]


# --- proposals and approval -----------------------------------------------------------------


def test_propose_records_the_proposer_and_emits_task_proposed(coworld_store, researcher, goal):
    task = tasks.propose(
        coworld_store, researcher, goal_id=goal, type="research", title="t", description="d"
    )
    assert task["status"] == "proposed"
    assert task["proposed_by"] == str(researcher)
    assert task["approved_by"] is None
    assert kinds(coworld_store) == ["task.proposed"]


def test_approve_moves_a_proposal_to_unclaimed(coworld_store, session_actor, researcher, goal):
    task = tasks.propose(
        coworld_store, researcher, goal_id=goal, type="research", title="t", description="d"
    )
    approved = tasks.approve(coworld_store, session_actor, task["id"])
    assert approved["status"] == "unclaimed"
    assert approved["approved_by"] == str(session_actor)
    assert approved["approved_at"] == "2026-10-28T21:04:05.123456Z"
    assert kinds(coworld_store) == ["task.proposed", "task.approved"]
    with pytest.raises(StoreError, match="already approved"):
        tasks.approve(coworld_store, session_actor, task["id"])


def test_approval_setting_lets_only_the_user_or_a_citing_session_approve(
    coworld_store, session_actor, researcher, user, goal
):
    settings.update(
        coworld_store, user, "approval_required.task_types", ["improvement"], cites="message:1"
    )

    def proposal() -> dict:
        return tasks.propose(
            coworld_store, researcher, goal_id=goal, type="improvement", title="t", description="d"
        )

    first = proposal()
    with pytest.raises(StoreError) as info:
        tasks.approve(coworld_store, session_actor, first["id"])
    problem = info.value.problems[0]
    assert "approval_required.task_types" in problem
    assert "improvement" in problem
    assert "user request" in problem and "overrides" in problem
    assert tasks.read(coworld_store, first["id"])["status"] == "proposed"

    by_user = tasks.approve(coworld_store, user, first["id"])
    assert by_user["status"] == "unclaimed"
    assert by_user["approved_by"] == "user:james"

    second = proposal()
    cited = tasks.approve(coworld_store, session_actor, second["id"], cites="message:42")
    assert cited["status"] == "unclaimed"
    assert cited["approved_by"].startswith(f"user via {session_actor}")
    assert "message:42" in cited["approved_by"]
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "task.approved"
    assert event.payload["cites"] == "message:42"

    # A type the setting does not name needs no citation.
    plain = tasks.propose(
        coworld_store, researcher, goal_id=goal, type="research", title="t", description="d"
    )
    assert tasks.approve(coworld_store, session_actor, plain["id"])["status"] == "unclaimed"


def test_create_unclaimed_under_the_setting_is_stored_proposed(
    coworld_store, session_actor, user, goal
):
    settings.update(
        coworld_store, user, "approval_required.task_types", ["improvement"], cites="message:1"
    )
    task = make_task(coworld_store, session_actor, goal, type="improvement")
    assert task["status"] == "proposed"
    assert task["proposed_by"] == str(session_actor)
    assert task["approved_by"] is None
    assert "approval_required.task_types" in task["note"]
    assert kinds(coworld_store) == ["settings.updated", "task.proposed"]

    cited = make_task(coworld_store, session_actor, goal, type="improvement", cites="message:5")
    assert cited["status"] == "unclaimed"
    assert "message:5" in cited["approved_by"]
    assert "note" not in cited

    by_user = make_task(coworld_store, user, goal, type="improvement")
    assert by_user["status"] == "unclaimed"
    assert by_user["approved_by"] == "user:james"


# --- update and link ------------------------------------------------------------------------


def test_update_only_in_proposed_or_unclaimed_and_clears_approval(
    coworld_store, session_actor, researcher, user, goal
):
    task = make_task(coworld_store, user, goal)
    edited = tasks.update(coworld_store, session_actor, task["id"], title="Study the midgame")
    assert edited["title"] == "Study the midgame"
    # The editor's own approval replaces the user's; nothing is gated for research.
    assert edited["approved_by"] == str(session_actor)
    assert edited["status"] == "unclaimed"
    assert "note" not in edited

    settings.update(
        coworld_store, user, "approval_required.task_types", ["research"], cites="message:1"
    )
    cleared = tasks.update(coworld_store, session_actor, task["id"], description="More detail.")
    assert cleared["status"] == "unclaimed"
    assert cleared["approved_by"] is None
    assert "approval" in cleared["note"]
    assert tasks.dispatchable(coworld_store) == []
    with pytest.raises(StoreError, match="not approved"):
        claim(coworld_store, researcher, task["id"])
    reapproved = tasks.approve(coworld_store, user, task["id"])
    assert reapproved["approved_by"] == "user:james"

    with pytest.raises(StoreError) as info:
        tasks.update(coworld_store, user, task["id"], title="", colour="blue")
    assert any("title" in p for p in info.value.problems)
    assert any("colour" in p for p in info.value.problems)
    with pytest.raises(StoreError, match="no fields"):
        tasks.update(coworld_store, user, task["id"])

    claim(coworld_store, researcher, task["id"])
    with pytest.raises(StoreError, match="in-progress"):
        tasks.update(coworld_store, user, task["id"], title="Too late")

    proposed = tasks.propose(
        coworld_store, researcher, goal_id=goal, type="research", title="t", description="d"
    )
    still = tasks.update(coworld_store, user, proposed["id"], title="Better title")
    assert still["status"] == "proposed" and still["approved_by"] is None


def test_link_adds_dependencies_and_refuses_cycles(coworld_store, harness, goal):
    a = make_task(coworld_store, harness, goal, title="a")
    b = make_task(coworld_store, harness, goal, title="b")
    c = make_task(coworld_store, harness, goal, title="c")
    assert tasks.link(coworld_store, harness, b["id"], [a["id"]])["depends_on"] == ["T-1"]
    assert tasks.link(coworld_store, harness, c["id"], [b["id"]])["depends_on"] == ["T-2"]
    with pytest.raises(StoreError) as info:
        tasks.link(coworld_store, harness, a["id"], [c["id"], a["id"], "T-9"])
    problems = info.value.problems
    assert any("cycle" in p for p in problems)
    assert any("itself" in p for p in problems)
    assert any("T-9 does not exist" in p for p in problems)
    # Linking twice is harmless.
    assert tasks.link(coworld_store, harness, b["id"], [a["id"]])["depends_on"] == ["T-1"]
    assert coworld_store.one("SELECT count(*) FROM task_deps")[0] == 2


# --- claims, waits and messages --------------------------------------------------------------


def test_claim_requires_approval_and_complete_dependencies(
    coworld_store, harness, researcher, goal
):
    dep = make_task(coworld_store, harness, goal, title="dep")
    task = tasks.propose(
        coworld_store, researcher, goal_id=goal, type="research", title="t", description="d"
    )
    tasks.link(coworld_store, harness, task["id"], [dep["id"]])
    with pytest.raises(StoreError) as info:
        claim(coworld_store, researcher, task["id"])
    problems = info.value.problems
    assert any("proposed" in p for p in problems)
    assert any("not approved" in p for p in problems)
    assert any("T-1" in p and "complete" in p for p in problems)
    tasks.approve(coworld_store, harness, task["id"])
    with pytest.raises(StoreError) as info:
        claim(coworld_store, researcher, task["id"])
    assert info.value.problems == [f"{task['id']} waits for T-1 to complete"]

    claim(coworld_store, researcher, dep["id"], n=1)
    tasks.complete(
        coworld_store,
        researcher,
        dep["id"],
        checklist={"claims": "none: nothing new"},
        merge_request="no",
        merge_reason="no code changes",
        result="nothing to report",
    )
    claimed = claim(coworld_store, researcher, task["id"], n=2)
    assert claimed["status"] == "in-progress"


def test_claim_sets_the_lease_and_a_session_row(coworld_store, harness, researcher, goal):
    task = make_task(coworld_store, harness, goal)
    claimed = claim(coworld_store, researcher, task["id"])
    assert claimed["status"] == "in-progress"
    assert claimed["held_by"] == researcher.id
    assert claimed["lease_epoch"] == 1
    assert claimed["lease_renewed"] == "2026-10-28T21:04:05.123456Z"
    assert claimed["run_id"] == "run-1"
    assert claimed["worktree"] == "/w/task-1"
    assert claimed["branch"] == "task/1"
    assert claimed["sessions"] == [
        {
            "session_id": researcher.id,
            "kind": "task",
            "epoch": 1,
            "started": "2026-10-28T21:04:05.123456Z",
        }
    ]
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "task.claimed"
    assert event.payload["session"] == researcher.id
    assert event.payload["epoch"] == 1
    with pytest.raises(StoreError, match="only a session"):
        claim(coworld_store, harness, make_task(coworld_store, harness, goal)["id"])
    with pytest.raises(StoreError) as info:
        tasks.claim(coworld_store, researcher, task["id"], run_id=None, worktree="", branch="")
    assert len(info.value.problems) == 2


def test_claim_kind_change_marks_an_orchestrator_change(coworld_store, session_actor):
    task = tasks.create(
        coworld_store, session_actor, type="maintenance", title="Fix config", description="d"
    )
    claimed = tasks.claim(
        coworld_store,
        session_actor,
        task["id"],
        run_id=None,
        worktree="/w/orch-1",
        branch="orch/1",
        kind="change",
    )
    assert claimed["sessions"][0]["kind"] == "change"
    with pytest.raises(StoreError, match="kind"):
        tasks.claim(
            coworld_store,
            session_actor,
            task["id"],
            run_id=None,
            worktree="/w",
            branch="b",
            kind="sprint",
        )


def test_release_returns_the_task_to_unclaimed(coworld_store, harness, researcher, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    tasks.wait(coworld_store, researcher, task["id"], kind="xp", detail="waiting for runs")
    with pytest.raises(StoreError, match="reason"):
        tasks.release(coworld_store, researcher, task["id"], reason=" ")
    released = tasks.release(coworld_store, researcher, task["id"], reason="out of ideas")
    assert released["status"] == "unclaimed"
    assert released["held_by"] is None
    assert released["run_id"] is None
    assert released["lease_renewed"] is None
    assert released["waiting_on"] is None
    assert released["lease_epoch"] == 1
    assert kinds(coworld_store)[-1] == "task.released"
    assert coworld_store.events_after(0)[-1].payload["reason"] == "out of ideas"
    with pytest.raises(LeaseError):
        tasks.release(coworld_store, researcher, task["id"], reason="again")
    again = claim(coworld_store, researcher, task["id"], n=2)
    assert again["lease_epoch"] == 2


def test_wait_kinds_are_validated_and_resolve_clears(coworld_store, harness, researcher, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    with pytest.raises(StoreError) as info:
        tasks.wait(coworld_store, researcher, task["id"], kind="coffee", detail="")
    assert any("coffee" in p and "question" in p for p in info.value.problems)
    assert any("detail" in p for p in info.value.problems)
    with pytest.raises(StoreError, match="not waiting"):
        tasks.resolve_wait(coworld_store, harness, task["id"], note="nothing")

    waiting = tasks.wait(
        coworld_store, researcher, task["id"], kind="xp", detail="XP request", ref=["xreq_1"]
    )
    assert waiting["waiting_on"] == {
        "kind": "xp",
        "detail": "XP request",
        "ref": ["xreq_1"],
        "since": "2026-10-28T21:04:05.123456Z",
    }
    assert coworld_store.events_after(0)[-1].kind == "task.waiting"
    assert coworld_store.events_after(0)[-1].payload["kind"] == "xp"

    resolved = tasks.resolve_wait(coworld_store, harness, task["id"], note="request finished")
    assert resolved["waiting_on"] is None
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "task.wait_resolved"
    assert event.payload == {"kind": "xp", "ref": ["xreq_1"], "note": "request finished"}


def test_wait_needs_an_in_progress_task(coworld_store, harness, researcher, goal):
    task = make_task(coworld_store, harness, goal)
    with pytest.raises(LeaseError):
        tasks.wait(coworld_store, researcher, task["id"], kind="xp", detail="x")


def test_ask_answer_roundtrip(coworld_store, harness, researcher, session_actor, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    asked = tasks.ask(coworld_store, researcher, task["id"], question="Which league?")
    question = asked["messages"][0]
    assert question["direction"] == "to_orchestrator"
    assert question["kind"] == "question"
    assert question["text"] == "Which league?"
    assert question["created_by"] == str(researcher)
    assert question["delivered"] is None
    assert asked["waiting_on"]["kind"] == "question"
    assert asked["waiting_on"]["ref"] == question["id"]
    assert coworld_store.events_after(0)[-1].kind == "task.asked"

    pending = tasks.pending_messages(coworld_store, task["id"], "to_orchestrator")
    assert [m["id"] for m in pending] == [question["id"]]
    assert tasks.mark_delivered(coworld_store, harness, [question["id"]]) == 1
    assert tasks.pending_messages(coworld_store, task["id"], "to_orchestrator") == []
    assert tasks.mark_delivered(coworld_store, harness, [question["id"]]) == 0

    answered = tasks.answer(coworld_store, session_actor, task["id"], text="The open league")
    assert answered["waiting_on"] is None
    answer = answered["messages"][-1]
    assert (answer["direction"], answer["kind"]) == ("to_researcher", "answer")
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "task.answered"
    assert event.payload == {"message_id": answer["id"], "question_id": question["id"]}
    assert [m["id"] for m in tasks.pending_messages(coworld_store, task["id"], "to_researcher")]
    with pytest.raises(StoreError, match="no open question"):
        tasks.answer(coworld_store, session_actor, task["id"], text="Again")
    with pytest.raises(StoreError, match="is not a question"):
        tasks.answer(coworld_store, session_actor, task["id"], text="x", question_id=answer["id"])


def test_message_directions(coworld_store, harness, researcher, session_actor, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    note = tasks.message(
        coworld_store, session_actor, task["id"], text="Mind the credits", direction="to_researcher"
    )
    assert note["messages"][-1]["kind"] == "message"
    reply = tasks.message(
        coworld_store, researcher, task["id"], text="Will do", direction="to_orchestrator"
    )
    assert len(reply["messages"]) == 2
    assert kinds(coworld_store)[-2:] == ["task.message", "task.message"]
    with pytest.raises(LeaseError):
        tasks.message(
            coworld_store, session_actor, task["id"], text="x", direction="to_orchestrator"
        )
    with pytest.raises(StoreError, match="direction"):
        tasks.message(coworld_store, harness, task["id"], text="x", direction="sideways")
    with pytest.raises(StoreError, match="direction"):
        tasks.pending_messages(coworld_store, task["id"], "sideways")


# --- completion -----------------------------------------------------------------------------


def test_complete_requires_holder_and_checklist(coworld_store, harness, researcher, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    with pytest.raises(LeaseError):
        tasks.complete(
            coworld_store, harness, task["id"], checklist={}, merge_request="no", result="r"
        )
    with pytest.raises(StoreError) as info:
        tasks.complete(coworld_store, researcher, task["id"], checklist={}, merge_request="yes")
    problems = info.value.problems
    assert any(p.startswith("checklist: claims") for p in problems)
    assert any("merge yes needs the commit" in p for p in problems)
    assert any("result" in p for p in problems)
    assert len(problems) == 4  # with the outcome failed way out
    assert tasks.read(coworld_store, task["id"])["status"] == "in-progress"


def test_complete_checks_referenced_records_exist(coworld_store, harness, researcher, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    tasks.wait(coworld_store, researcher, task["id"], kind="process", detail="build")
    with pytest.raises(StoreError) as info:
        tasks.complete(
            coworld_store,
            researcher,
            task["id"],
            checklist={"claims": ["C-9", "C-10"]},
            merge_request="no",
            merge_reason="no code",
            result="r",
        )
    assert info.value.problems == ["C-9 does not exist", "C-10 does not exist"]
    make_claim(coworld_store, researcher)
    done = tasks.complete(
        coworld_store,
        researcher,
        task["id"],
        checklist={"claims": ["C-1"]},
        commit=SHA,
        merge_request="yes",
        result="One claim about the opening.",
    )
    assert done["status"] == "complete"
    assert done["outcome"] == "succeeded"
    assert done["result"] == "One claim about the opening."
    assert done["completed_commit"] == SHA
    assert done["checklist"] == {
        "claims": ["C-1"],
        "commit": SHA,
        "merge_request": "yes",
        "merge_reason": "",
    }
    assert done["merge"] is None
    assert done["waiting_on"] is None
    assert done["held_by"] == researcher.id
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "task.completed"
    assert event.payload["commit"] == SHA
    assert event.payload["merge_request"] == "yes"
    with pytest.raises(StoreError, match="complete, not in-progress"):
        tasks.complete(
            coworld_store,
            researcher,
            task["id"],
            checklist={"claims": ["C-1"]},
            merge_request="no",
            merge_reason="x",
            result="r",
        )


def test_complete_improvement_checks_experiment_versions_and_candidate(
    coworld_store, harness, researcher, goal
):
    experiment = make_experiment(coworld_store, harness)
    task = claimed_task(
        coworld_store, harness, researcher, goal, type="improvement", experiment_id=experiment
    )
    make_claim(coworld_store, harness)
    version = make_version(coworld_store, harness)
    other = make_experiment(coworld_store, harness)
    checklist = {
        "claims": ["C-1"],
        "experiment": f"X-{other}",
        "versions": [f"V-{version}", "V-9"],
        "submission_reason": "beats the champion",
    }
    with pytest.raises(StoreError) as info:
        tasks.complete(
            coworld_store,
            researcher,
            task["id"],
            checklist=checklist,
            commit=SHA,
            merge_request="yes",
            result="r",
        )
    problems = info.value.problems
    assert any("submission_candidate" in p for p in problems)
    assert any("V-9 does not exist" in p for p in problems)
    assert any(f"X-{experiment}" in p and f"X-{other}" in p for p in problems)
    checklist["experiment"] = f"X-{experiment}"
    checklist["versions"] = [f"V-{version}"]
    done = tasks.complete(
        coworld_store,
        researcher,
        task["id"],
        checklist=checklist,
        commit=SHA,
        merge_request="yes",
        submission_candidate="yes",
        result="r",
    )
    assert done["checklist"]["submission_candidate"] == "yes"
    assert coworld_store.events_after(0)[-1].payload["submission_candidate"] == "yes"


def test_complete_improvement_records_the_experiment_when_unset(
    coworld_store, harness, researcher, goal
):
    task = claimed_task(coworld_store, harness, researcher, goal, type="improvement")
    experiment = make_experiment(coworld_store, harness, task_id=1)
    make_claim(coworld_store, harness)
    version = make_version(coworld_store, harness)
    done = tasks.complete(
        coworld_store,
        researcher,
        task["id"],
        checklist={
            "claims": ["C-1"],
            "experiment": f"X-{experiment}",
            "versions": [f"V-{version}"],
            "submission_candidate": "no",
            "submission_reason": "no improvement",
        },
        merge_request="no",
        merge_reason="nothing worth merging",
        result="r",
    )
    assert done["experiment_id"] == experiment


def test_complete_ideation_validates_proposals_for_the_goal(
    coworld_store, harness, researcher, goal
):
    task = claimed_task(coworld_store, harness, researcher, goal, type="ideation", ideation_round=1)
    other_goal = make_goal(coworld_store, harness, "other")
    proposals = [
        tasks.propose(
            coworld_store, researcher, goal_id=goal, type="research", title=f"p{i}", description="d"
        )["id"]
        for i in range(2)
    ]
    elsewhere = tasks.propose(
        coworld_store, researcher, goal_id=other_goal, type="research", title="e", description="d"
    )["id"]
    approved = make_task(coworld_store, harness, goal, title="approved already")["id"]

    def finish(ids):
        return tasks.complete(
            coworld_store,
            researcher,
            task["id"],
            checklist={"proposed_tasks": ids},
            merge_request="no",
            merge_reason="ideas only",
            result="r",
        )

    with pytest.raises(StoreError) as info:
        finish([proposals[0], elsewhere, approved, task["id"]])
    problems = info.value.problems
    assert any(elsewhere in p and "Goal" in p for p in problems)
    assert any(approved in p and "proposed" in p for p in problems)
    assert any(task["id"] in p and "itself" in p for p in problems)
    assert finish([proposals[0]])["status"] == "complete"


def test_complete_ideation_counts_proposals_the_orchestrator_already_decided(
    coworld_store, harness, researcher, goal
):
    """A management wake decides a proposal while the Researcher still writes the next one."""
    task = claimed_task(coworld_store, harness, researcher, goal, type="ideation", ideation_round=1)
    proposals = [
        tasks.propose(
            coworld_store, researcher, goal_id=goal, type="research", title=f"p{i}", description="d"
        )["id"]
        for i in range(3)
    ]
    tasks.approve(coworld_store, harness, proposals[0])
    tasks.reject(coworld_store, harness, proposals[1], next_step="not this")

    done = tasks.complete(
        coworld_store,
        researcher,
        task["id"],
        checklist={"proposed_tasks": proposals},
        merge_request="no",
        merge_reason="ideas only",
        result="r",
    )

    assert done["status"] == "complete"


def test_complete_maintenance_requires_commit_summary_and_capability(
    coworld_store, harness, researcher
):
    repair = tasks.create(
        coworld_store,
        harness,
        type="maintenance",
        title="Repair build",
        description="d",
        capability="build",
    )
    claim(coworld_store, researcher, repair["id"])
    with pytest.raises(StoreError) as info:
        tasks.complete(
            coworld_store,
            researcher,
            repair["id"],
            checklist={"summary": "fixed"},
            merge_request="no",
            merge_reason="x",
            result="r",
        )
    problems = info.value.problems
    assert any("commit" in p for p in problems)
    assert any("capability" in p for p in problems)
    done = tasks.complete(
        coworld_store,
        researcher,
        repair["id"],
        checklist={"summary": "fixed the toolchain", "capability": "build"},
        commit=SHA,
        merge_request="yes",
        result="r",
    )
    assert done["checklist"]["commit"] == SHA
    assert done["completed_commit"] == SHA


def test_merge_request_no_needs_a_reason_and_commit_is_a_sha(
    coworld_store, harness, researcher, goal
):
    task = claimed_task(coworld_store, harness, researcher, goal)
    with pytest.raises(StoreError) as info:
        tasks.complete(
            coworld_store,
            researcher,
            task["id"],
            checklist={"claims": "none: nothing"},
            commit="not-a-sha",
            merge_request="no",
            result="r",
        )
    problems = info.value.problems
    assert any("merge_reason" in p for p in problems)
    assert any("commit" in p and "sha" in p for p in problems)
    with pytest.raises(StoreError, match="merge must be"):
        tasks.complete(
            coworld_store,
            researcher,
            task["id"],
            checklist={"claims": "none: nothing"},
            merge_request="maybe",
            result="r",
        )


def test_complete_with_outcome_failed_records_the_failure(coworld_store, harness, researcher, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    with pytest.raises(StoreError, match="failure"):
        tasks.complete(coworld_store, researcher, task["id"], outcome="failed")
    with pytest.raises(StoreError) as info:
        tasks.complete(
            coworld_store,
            researcher,
            task["id"],
            outcome="failed",
            failure={"what_failed": "x", "exit_code": "1", "bogus": 1},
        )
    problems = info.value.problems
    assert any("next_step" in p for p in problems)
    assert any("exit_code" in p for p in problems)
    assert any("bogus" in p for p in problems)
    with pytest.raises(StoreError, match="outcome"):
        tasks.complete(coworld_store, researcher, task["id"], outcome="inconclusive")
    with pytest.raises(LeaseError):
        tasks.complete(coworld_store, harness, task["id"], outcome="failed", failure=GOOD_FAILURE)

    failed = tasks.complete(
        coworld_store,
        researcher,
        task["id"],
        outcome="failed",
        failure={"what_failed": "the build broke", "next_step": "repair the toolchain"},
        commit=SHA,
    )
    assert failed["status"] == "complete"
    assert failed["outcome"] == "failed"
    assert failed["failure"] == {
        "what_failed": "the build broke",
        "next_step": "repair the toolchain",
    }
    assert failed["completed_commit"] == SHA
    assert failed["checklist"] is None
    assert kinds(coworld_store)[-1] == "task.failed"
    assert coworld_store.events_after(0)[-1].payload["failure"]["what_failed"] == "the build broke"


def test_fail_is_the_failed_path(coworld_store, harness, researcher, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    failed = tasks.fail(coworld_store, researcher, task["id"], failure=GOOD_FAILURE)
    assert failed["outcome"] == "failed"
    assert failed["failure"] == GOOD_FAILURE
    assert kinds(coworld_store)[-1] == "task.failed"


# --- review, reopen, merge, abandon --------------------------------------------------------


def finished(store, harness, researcher, goal, n=1) -> dict:
    task = make_task(store, harness, goal)
    claim(store, researcher, task["id"], n=n)
    return tasks.complete(
        store,
        researcher,
        task["id"],
        checklist={"claims": "none: nothing"},
        commit=SHA,
        merge_request="yes",
        result="done",
    )


def test_review_accept_sets_merge_pending_or_not_merged(
    coworld_store, harness, researcher, session_actor, goal
):
    task = finished(coworld_store, harness, researcher, goal)
    with pytest.raises(StoreError) as info:
        tasks.review(
            coworld_store, session_actor, task["id"], accept=True, merge="later", reason=""
        )
    assert len(info.value.problems) == 2
    accepted = tasks.review(
        coworld_store, session_actor, task["id"], accept=True, merge="yes", reason="solid"
    )
    assert accepted["merge"] == "pending"
    assert accepted["status"] == "complete"
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "task.reviewed"
    assert event.payload == {"accept": True, "merge": "yes", "reason": "solid"}

    other = finished(coworld_store, harness, researcher, make_goal(coworld_store, harness), n=2)
    declined = tasks.review(
        coworld_store, session_actor, other["id"], accept=True, merge="no", reason="docs only"
    )
    assert declined["merge"] == "not merged: docs only"

    merged = tasks.set_merge(coworld_store, harness, task["id"], f"merged {SHA}")
    assert merged["merge"] == f"merged {SHA}"
    assert coworld_store.events_after(0)[-1].kind == "task.merged"
    assert coworld_store.events_after(0)[-1].payload["commit"] == SHA
    before = len(kinds(coworld_store))
    tasks.set_merge(coworld_store, harness, other["id"], "not merged: conflict in main.py")
    assert len(kinds(coworld_store)) == before
    with pytest.raises(StoreError, match="merge"):
        tasks.set_merge(coworld_store, harness, task["id"], "sort of merged")


def test_review_queues_a_merge_again_after_it_failed(
    coworld_store, harness, researcher, session_actor, goal
):
    # merge-conflicts tells the Orchestrator to do this once a fix has merged.
    task = finished(coworld_store, harness, researcher, goal)
    tasks.review(coworld_store, session_actor, task["id"], accept=True, merge="yes", reason="ok")
    tasks.set_merge(coworld_store, harness, task["id"], "not merged: merge conflict in a.py")
    again = tasks.review(
        coworld_store, session_actor, task["id"], accept=True, merge="yes", reason="fixed"
    )
    assert again["merge"] == "pending"


def test_review_reopen_same_task(coworld_store, harness, researcher, session_actor, goal):
    task = finished(coworld_store, harness, researcher, goal)
    reopened = tasks.review(
        coworld_store,
        session_actor,
        task["id"],
        accept=False,
        merge="no",
        reason="the claim needs evidence",
    )
    assert reopened["status"] == "in-progress"
    assert reopened["outcome"] is None
    assert reopened["merge"] is None
    assert reopened["held_by"] == researcher.id
    assert reopened["lease_epoch"] == 1
    assert reopened["messages"][-1]["direction"] == "to_researcher"
    assert "the claim needs evidence" in reopened["messages"][-1]["text"]
    assert kinds(coworld_store)[-2:] == ["task.reviewed", "task.reopened"]
    with pytest.raises(StoreError, match="complete"):
        tasks.review(coworld_store, session_actor, task["id"], accept=True, merge="yes", reason="r")
    # The same holder finishes again without a new claim.
    done = tasks.complete(
        coworld_store,
        researcher,
        task["id"],
        checklist={"claims": "none: still nothing"},
        merge_request="no",
        merge_reason="no code",
        result="done again",
    )
    assert done["status"] == "complete"
    with pytest.raises(StoreError, match="reason"):
        tasks.reopen(coworld_store, session_actor, task["id"], reason="")
    again = tasks.reopen(coworld_store, session_actor, task["id"], reason="one more look")
    assert again["status"] == "in-progress"
    assert kinds(coworld_store)[-1] == "task.reopened"


def test_review_needs_a_succeeded_task(coworld_store, harness, researcher, session_actor, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    tasks.fail(coworld_store, researcher, task["id"], failure=GOOD_FAILURE)
    with pytest.raises(StoreError, match="failed"):
        tasks.review(coworld_store, session_actor, task["id"], accept=True, merge="no", reason="r")


def test_abandon_requires_failure_fields(coworld_store, harness, researcher, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    with pytest.raises(StoreError) as info:
        tasks.abandon(coworld_store, harness, task["id"], failure={"error": "boom"})
    assert info.value.problems == [
        "failure.what_failed is required",
        "failure.next_step is required",
    ]
    with pytest.raises(StoreError, match="failure must be"):
        tasks.abandon(coworld_store, harness, task["id"], failure="gave up")
    minimal = {"what_failed": "no progress", "next_step": "drop it"}
    abandoned = tasks.abandon(coworld_store, harness, task["id"], failure=minimal)
    assert abandoned["status"] == "abandoned"
    assert abandoned["outcome"] == "abandoned"
    assert abandoned["failure"] == minimal
    assert abandoned["held_by"] is None
    assert abandoned["run_id"] is None
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "task.abandoned"
    assert event.payload["previous_holder"] == researcher.id
    assert event.payload["run_id"] == "run-1"
    with pytest.raises(StoreError, match="open Task"):
        tasks.abandon(coworld_store, harness, task["id"], failure=minimal)
    for status in ("proposed", "unclaimed"):
        other = make_task(coworld_store, harness, goal, status=status)
        assert tasks.abandon(coworld_store, harness, other["id"], failure=minimal)["status"] == (
            "abandoned"
        )


def test_reject_abandons_a_proposed_task(coworld_store, harness, researcher, user, goal):
    proposal = tasks.propose(
        coworld_store, researcher, goal_id=goal, type="research", title="t", description="d"
    )
    with pytest.raises(StoreError, match="next_step"):
        tasks.reject(coworld_store, user, proposal["id"], next_step="")
    rejected = tasks.reject(coworld_store, user, proposal["id"], next_step="not now")
    assert rejected["status"] == "abandoned"
    assert rejected["failure"] == {"what_failed": "rejected by the user", "next_step": "not now"}
    assert kinds(coworld_store)[-1] == "task.abandoned"
    by_session = tasks.propose(
        coworld_store, researcher, goal_id=goal, type="research", title="t2", description="d"
    )
    rejected = tasks.reject(coworld_store, harness, by_session["id"], next_step="dup of T-1")
    assert rejected["failure"]["what_failed"] == "rejected by harness"
    unclaimed = make_task(coworld_store, harness, goal)
    with pytest.raises(StoreError, match="proposed"):
        tasks.reject(coworld_store, user, unclaimed["id"], next_step="x")


def test_propose_submission_needs_the_holder_and_an_existing_version(
    coworld_store, harness, researcher, goal
):
    task = claimed_task(coworld_store, harness, researcher, goal, type="improvement")
    with pytest.raises(StoreError, match="V-3 does not exist"):
        tasks.propose_submission(coworld_store, researcher, task["id"], version_id=3, reason="r")
    version = make_version(coworld_store, harness)
    with pytest.raises(LeaseError):
        tasks.propose_submission(coworld_store, harness, task["id"], version_id=version, reason="r")
    proposed = tasks.propose_submission(
        coworld_store, researcher, task["id"], version_id=f"V-{version}", reason="beats champion"
    )
    assert proposed["messages"][-1]["direction"] == "to_orchestrator"
    assert f"V-{version}" in proposed["messages"][-1]["text"]
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "task.proposed_submission"
    assert event.payload["version"] == f"V-{version}"
    assert event.payload["reason"] == "beats champion"


# --- reads, lists, dispatch ------------------------------------------------------------------


def test_dispatchable_order(coworld_store, harness, researcher, clock, goal):
    normal_old = make_task(coworld_store, harness, goal, title="normal old")
    clock.advance(1)
    blocked = make_task(
        coworld_store, harness, goal, title="blocked", depends_on=[normal_old["id"]]
    )
    clock.advance(1)
    make_task(coworld_store, harness, goal, title="high", priority="high")
    clock.advance(1)
    make_task(coworld_store, harness, goal, title="normal new")
    tasks.propose(
        coworld_store, researcher, goal_id=goal, type="research", title="p", description="d"
    )
    settings.update(
        coworld_store, Actor.user("u"), "approval_required.task_types", ["research"], cites="m"
    )
    make_task(coworld_store, researcher, goal, title="needs approval")
    assert [t["title"] for t in tasks.dispatchable(coworld_store)] == [
        "high",
        "normal old",
        "normal new",
    ]
    claim(coworld_store, researcher, normal_old["id"])
    assert [t["title"] for t in tasks.dispatchable(coworld_store)] == ["high", "normal new"]
    tasks.fail(coworld_store, researcher, normal_old["id"], failure=GOOD_FAILURE)
    assert blocked["title"] not in [t["title"] for t in tasks.dispatchable(coworld_store)]


def test_read_computes_stuck(coworld_store, harness, researcher, clock, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    assert tasks.read(coworld_store, task["id"])["stuck"] is False
    clock.advance(299)
    assert tasks.read(coworld_store, task["id"])["stuck"] is False
    clock.advance(2)
    assert tasks.read(coworld_store, task["id"])["stuck"] is True
    assert tasks.read(coworld_store, task["id"], live_run=True)["stuck"] is False
    tasks.renew_lease(coworld_store, researcher, task["id"])
    assert tasks.read(coworld_store, task["id"])["stuck"] is False
    clock.advance(301)
    tasks.wait(coworld_store, researcher, task["id"], kind="user", detail="approval")
    assert tasks.read(coworld_store, task["id"])["stuck"] is False
    waiting = make_task(coworld_store, harness, goal, title="never claimed")
    assert tasks.read(coworld_store, waiting["id"])["stuck"] is False


def test_list_filters(coworld_store, harness, researcher, goal):
    other_goal = make_goal(coworld_store, harness, "other")
    a = make_task(coworld_store, harness, goal, title="a")
    make_task(coworld_store, harness, other_goal, title="b", type="ideation")
    claim(coworld_store, researcher, a["id"])
    assert [t["title"] for t in tasks.list(coworld_store)] == ["a", "b"]
    assert [t["title"] for t in tasks.list(coworld_store, status="in-progress")] == ["a"]
    assert [t["title"] for t in tasks.list(coworld_store, goal_id=f"G-{other_goal}")] == ["b"]
    assert [t["title"] for t in tasks.list(coworld_store, type="ideation")] == ["b"]
    assert [t["title"] for t in tasks.list(coworld_store, held_by=researcher.id)] == ["a"]
    assert tasks.list(coworld_store, held_by="nobody") == []
    with pytest.raises(StoreError, match="status"):
        tasks.list(coworld_store, status="open")


def test_set_run_and_set_last_error(coworld_store, harness, researcher, goal):
    task = claimed_task(coworld_store, harness, researcher, goal)
    before = len(kinds(coworld_store))
    assert tasks.set_run(coworld_store, harness, task["id"], "run-2")["run_id"] == "run-2"
    error = {"command": "make check", "exit_code": 2, "error": "tail"}
    with pytest.raises(LeaseError):
        tasks.set_last_error(coworld_store, harness, task["id"], error)
    with pytest.raises(StoreError, match="error"):
        tasks.set_last_error(coworld_store, researcher, task["id"], "boom")
    assert (
        tasks.set_last_error(coworld_store, researcher, task["id"], error)["last_error_seen"]
        == error
    )
    assert tasks.renew_lease(coworld_store, researcher, task["id"])["lease_renewed"]
    # Bookkeeping writes emit no events.
    assert len(kinds(coworld_store)) == before
    assert coworld_store.unmarked_changes() == []
