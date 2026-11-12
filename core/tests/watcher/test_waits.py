"""Wait resolution per kind on the fake platform; a resolved wait is an event, not a run."""

from __future__ import annotations

import os
import subprocess

import pytest

from research_core.platform.errors import PlatformError
from research_core.platform.fake import FakePlatform
from research_core.store import experiments, settings, tasks, versions
from research_core.store.actors import Actor
from research_core.store.errors import StoreError
from research_core.watcher import waits
from tests.watcher._support import (
    HARNESS,
    USER,
    events_of,
    make_ctx,
    make_task,
    working_session,
)

HYPOTHESIS = {
    "statement": "Retreating at 30% health loses fewer units because fights end earlier",
    "if_true": "the candidate's win rate rises by at least two points",
    "if_false": "the win rate stays within noise of the baseline",
    "motivation": "C-1 says early retreats save units; nothing has measured the effect",
}
ARTIFACT = {"kind": "file", "path": "dist/policy.bin", "digest": "sha256:" + "0" * 64, "size": 2048}
METHODS = {
    "baseline": "V-1",
    "candidate": None,
    "procedure": "Build the retreat change on the task branch; run 40 paired episodes per arm",
    "metrics": [{"name": "win_rate", "direction": "up", "role": "primary", "min_effect": 0.02}],
    "decision_rule": {"text": "confirmed when win_rate rises by 0.02", "n_min": 40, "k": 0.5},
}


def waiting(ctx, kind, detail="waiting", ref=None):
    """A Task held by a Session whose run ended after it recorded the wait."""
    task, session, run_id = working_session(ctx)
    tasks.wait(
        ctx.store, Actor.session(session["id"]), task["id"], kind=kind, detail=detail, ref=ref
    )
    ctx.hermes.complete(run_id)
    return task, session


def resolved(ctx, task):
    return tasks.read(ctx.store, task["id"])["waiting_on"] is None


def running_experiment(ctx, actor, task_id=None):
    versions.create(
        ctx.store, HARNESS, summary="baseline", commit_sha="0" * 40, branch="main",
        artifact=ARTIFACT, based_on=[],
    )  # fmt: skip
    record = experiments.create(
        ctx.store, actor, summary="does retreating help", hypothesis=HYPOTHESIS, methods=METHODS,
        task_id=task_id,
    )  # fmt: skip
    experiments.start(ctx.store, actor, record["id"])
    return record


ROSTER = [
    {"player": {"policy_ref": "pv_a"}, "slot": 0},
    {"player": {"policy_ref": "pv_b"}, "slot": 1},
]


def test_xp_wait_resolves_when_every_request_is_terminal(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.platform.configure(polls_to_complete=2)
    body = {"coworld_id": "cow_fake", "roster": ROSTER, "num_episodes": 2, "title": "ide:X-1:1:a"}
    first = ctx.platform.xp_create(body)
    second = ctx.platform.xp_create({**body, "title": "ide:X-1:2:a"})
    experiment = running_experiment(ctx, HARNESS)
    run = experiments.add_run(
        ctx.store, HARNESS, experiment["id"], kind="xp", platform_request_id=first.id, arm="a"
    )
    task, session = waiting(ctx, "xp", "2 requests", ref=[first.id, second.id])

    pending = waits.run(ctx)
    assert pending["resolved"] == [] and pending["waiting"] == [{"task": "T-1", "kind": "xp"}]
    assert not resolved(ctx, task)

    done = waits.run(ctx)
    assert done["resolved"][0]["kind"] == "xp" and first.id in done["resolved"][0]["note"]
    assert resolved(ctx, task)
    event = events_of(ctx.store, "task.wait_resolved")[-1]
    assert event["payload"]["kind"] == "xp" and event["payload"]["ref"] == [first.id, second.id]
    assert ctx.hermes.steered == {} and len(ctx.hermes.runs) == 1, "the handler resumes, not this"
    updated = experiments.read(ctx.store, experiment["id"])["runs"][0]
    assert updated["id"] == run["id"] and updated["state"] == "completed"
    assert updated["episodes_completed"] == 2


def test_xp_wait_skips_run_updates_the_store_refuses_to_the_watcher(tmp_path):
    ctx = make_ctx(tmp_path)
    request = ctx.platform.xp_create(
        {"coworld_id": "cow_fake", "roster": ROSTER, "num_episodes": 1}
    )
    task, session, run_id = working_session(ctx)
    holder = Actor.session(session["id"])
    experiment = running_experiment(ctx, holder, task_id=task["id"])
    experiments.add_run(
        ctx.store, holder, experiment["id"], kind="xp", platform_request_id=request.id, arm="a"
    )
    tasks.wait(ctx.store, holder, task["id"], kind="xp", detail="1 request", ref=request.id)
    ctx.hermes.complete(run_id)

    report = waits.run(ctx)

    assert report["resolved"][0]["kind"] == "xp" and report["errors"] == []
    assert experiments.read(ctx.store, experiment["id"])["runs"][0]["state"] is None


def test_a_need_above_the_accounts_maximum_cannot_sleep_forever(tmp_path):
    ctx = make_ctx(tmp_path)
    full = ctx.platform.credits()
    task, session = waiting(ctx, "credits", f"XP for X-5: need {full.max * 10:g} credits")
    assert waits.run(ctx)["resolved"][0]["task"] == task["id"]


def test_credits_wait_needs_the_balance_the_detail_names(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.platform.configure(credits=0)
    task, session = waiting(ctx, "credits", "need 3 credits for the next request")
    assert waits.run(ctx)["waiting"] == [{"task": "T-1", "kind": "credits"}]
    ctx.platform.configure(credits=2)
    assert waits.run(ctx)["resolved"] == []
    ctx.platform.configure(credits=3)
    report = waits.run(ctx)
    assert report["resolved"][0]["note"] == "credits balance 3 covers the need (3)"
    assert resolved(ctx, task)


class PermitStub:
    """A platform whose GET answers from a table; a missing route is a 404."""

    def __init__(self):
        self.routes = {}

    def request(self, method, path, **kwargs):
        if path not in self.routes:
            raise PlatformError("no such ask", route=f"{method} {path}", status=404)
        return self.routes[path]


def test_permit_wait_resolves_on_an_answered_ask(tmp_path):
    ctx = make_ctx(tmp_path)
    stub = PermitStub()
    ctx.platform = stub
    task, session = waiting(ctx, "permit", "asked for a seat", ref="/v2/asks/ask_1")
    assert waits.run(ctx)["waiting"] == [{"task": "T-1", "kind": "permit"}], "404: not yet"
    stub.routes["/v2/asks/ask_1"] = {"id": "ask_1", "status": "pending"}
    assert waits.run(ctx)["resolved"] == []
    stub.routes["/v2/asks/ask_1"] = {"id": "ask_1", "status": "approved", "answer": "yes"}
    report = waits.run(ctx)
    assert report["resolved"][0]["note"] == "permit /v2/asks/ask_1 answered: answer=yes"
    assert resolved(ctx, task)


def test_submission_wait_resolves_when_placed(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.platform.configure(polls_to_complete=2)
    policy = ctx.platform.upload_file_policy(name="p", content=b"policy")
    ctx.platform.submit(league_id="league_fake", policy_version_id=policy.id)
    task, session = waiting(ctx, "submission", "submitted V-1", ref=policy.id)
    assert waits.run(ctx)["resolved"] == []
    report = waits.run(ctx)
    assert report["resolved"][0]["note"].endswith("placed") and resolved(ctx, task)


def test_platform_wait_resolves_when_the_route_answers_again(tmp_path):
    ctx = make_ctx(tmp_path)
    task, session = waiting(ctx, "platform", "GET /v2/leagues 503", ref="req_1")
    down = FakePlatform(tmp_path / "fake-platform.json", env={"IDE_FAKE_FAULTS": "platform=down"})
    ctx.platform = down
    assert waits.run(ctx)["waiting"] == [{"task": "T-1", "kind": "platform"}]
    ctx.platform = FakePlatform(tmp_path / "fake-platform.json", env={})
    assert waits.run(ctx)["resolved"][0]["note"] == "GET /v2/leagues answers again"
    assert resolved(ctx, task)


def test_platform_wait_on_a_write_route_probes_whoami(tmp_path):
    ctx = make_ctx(tmp_path)
    task, session = waiting(ctx, "platform", "POST /v2/experience-requests 503")
    assert waits.run(ctx)["resolved"][0]["note"] == "the platform answers again (whoami)"


def test_process_wait_resolves_when_the_pid_is_gone(tmp_path):
    ctx = make_ctx(tmp_path)
    task, session = waiting(ctx, "process", "evaluate running", ref=os.getpid())
    assert waits.run(ctx)["waiting"] == [{"task": "T-1", "kind": "process"}]
    child = subprocess.Popen(["true"])
    child.wait()
    tasks.wait(
        ctx.store,
        Actor.session(session["id"]),
        task["id"],
        kind="process",
        detail="x",
        ref=child.pid,
    )
    assert waits.run(ctx)["resolved"][0]["note"] == f"process {child.pid} has ended"


def test_question_wait_resolves_on_an_undelivered_answer(tmp_path):
    ctx = make_ctx(tmp_path)
    task, session, run_id = working_session(ctx)
    holder = Actor.session(session["id"])
    asked = tasks.ask(ctx.store, holder, task["id"], question="Which seed?")
    question_id = asked["waiting_on"]["ref"]
    # The wait points at another question, so tasks.answer leaves it; the resolver sees the answer.
    tasks.wait(ctx.store, holder, task["id"], kind="question", detail="Which seed?", ref=999)
    assert waits.run(ctx)["resolved"] == []
    tasks.answer(ctx.store, HARNESS, task["id"], text="Seed 7.", question_id=question_id)
    report = waits.run(ctx)
    assert report["resolved"][0]["note"] == "answered: Seed 7." and resolved(ctx, task)


def test_user_wait_resolves_on_a_setting_or_an_approval(tmp_path):
    ctx = make_ctx(tmp_path)
    task, session = waiting(ctx, "user", "needs max_researchers", ref="max_researchers")
    assert waits.run(ctx)["resolved"] == []
    settings.update(ctx.wstore, USER, "max_researchers", 3, cites="msg:1")
    assert waits.run(ctx)["resolved"][0]["note"] == "setting max_researchers was set to 3"

    proposed = make_task(ctx, status="proposed", title="Needs approval")
    holder = Actor.session(session["id"])
    tasks.wait(ctx.store, holder, task["id"], kind="user", detail="approve", ref=proposed["id"])
    assert waits.run(ctx)["resolved"] == []
    tasks.approve(ctx.store, USER, proposed["id"])
    assert waits.run(ctx)["resolved"][0]["note"] == f"{proposed['id']} approved by user:u1"


def test_dependency_wait_resolves_when_every_dependency_succeeded(tmp_path):
    ctx = make_ctx(tmp_path)
    dependency = make_task(ctx, title="First")
    make_task(ctx, title="Second", depends_on=[dependency["id"]])
    # A claim is refused while the dependency is open, so the Task is held by hand here.
    with ctx.store.tx(HARNESS) as tx:
        tx.execute(
            "UPDATE tasks SET status = 'in-progress', held_by = 's', lease_epoch = 1,"
            " waiting_on = ? WHERE id = 2",
            ('{"kind": "dependency", "detail": "T-1", "ref": "T-1"}',),
        )
    assert waits.run(ctx)["resolved"] == []
    with ctx.store.tx(HARNESS) as tx:
        tx.execute("UPDATE tasks SET status = 'complete', outcome = 'failed' WHERE id = 1")
    assert waits.run(ctx)["resolved"][0]["note"] == "dependencies ended: T-1 failed"


def test_a_dependency_wait_names_the_task_it_waits_on(tmp_path):
    ctx = make_ctx(tmp_path)
    other = make_task(ctx, title="Theirs")
    task, session = waiting(ctx, "dependency", "their result", ref=other["id"])
    assert waits.run(ctx)["resolved"] == []
    assert not resolved(ctx, task)
    with ctx.store.tx(HARNESS) as tx:
        tx.execute("UPDATE tasks SET status = 'complete', outcome = 'succeeded' WHERE id = 1")
    assert waits.run(ctx)["resolved"][0]["note"] == "dependencies ended: T-1 succeeded"


def test_a_dependency_wait_on_nothing_is_refused(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(StoreError, match="dependency wait names the Task"):
        waiting(ctx, "dependency", "something")


def test_platform_errors_are_reported_and_leave_the_wait(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    task, session = waiting(ctx, "xp", "x", ref=["xreq_1"])

    def down(_):
        raise PlatformError("service unavailable", route="GET", status=503)

    monkeypatch.setattr(ctx.platform, "xp_get", down)
    report = waits.run(ctx)
    assert report["errors"][0]["task"] == "T-1" and "unavailable" in report["errors"][0]["error"]
    assert not resolved(ctx, task)


def test_an_xp_request_the_platform_does_not_know_ends_the_wait(tmp_path):
    """A 404 never turns terminal: the wait used to poll it for good (a list sent as JSON text
    became one such id). Waits stored before the task tool refused such ids end this way."""
    ctx = make_ctx(tmp_path)
    request = ctx.platform.xp_create(
        {"coworld_id": "cow_fake", "roster": ROSTER, "num_episodes": 1}
    )
    ctx.platform.configure(polls_to_complete=1)
    task, session = waiting(ctx, "xp", "x", ref=f'["{request.id}"]')
    report = waits.run(ctx)
    assert resolved(ctx, task) and report["errors"] == []
    assert (
        report["resolved"][0]["note"] == f'XP requests ["{request.id}"] not found on the platform'
    )

    task, session = waiting(ctx, "xp", "x", ref=[request.id, "xreq_gone"])
    note = waits.run(ctx)["resolved"][0]["note"]
    assert note == f"XP requests {request.id} completed, xreq_gone not found on the platform"
