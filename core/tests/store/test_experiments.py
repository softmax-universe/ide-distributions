"""Experiments: a design that locks at start, runs recorded at once, a verdict at close (B.8)."""

from __future__ import annotations

import hashlib
import json
import sys
import types

import pytest

import research_core.store as store_package
from research_core.store import experiments, versions
from research_core.store.actors import Actor
from research_core.store.errors import LeaseError, NotFound, StoreError

T0 = "2026-10-28T21:04:05.123456Z"
HYPOTHESIS = {
    "statement": "Retreating at 30% health loses fewer units because fights end earlier",
    "if_true": "the candidate's win rate rises by at least two points",
    "if_false": "the win rate stays within noise of the baseline",
    "motivation": "C-1 says early retreats save units; nothing has measured the win-rate effect",
}
ARTIFACT = {"kind": "file", "path": "dist/policy.bin", "digest": "sha256:" + "0" * 64, "size": 2048}
PRIMARY = {"name": "win_rate", "direction": "up", "role": "primary", "min_effect": 0.02}
GUARDRAIL = {"name": "step_ms", "direction": "down", "role": "guardrail", "tolerance": 5}
MEASUREMENTS = [
    {"metric": "win_rate", "baseline": 0.50, "candidate": 0.53, "n": 40, "effect": 0.03},
    {"metric": "step_ms", "baseline": 12.0, "candidate": 12.5, "n": 40, "effect": -0.5},
]


def make_version(store, actor, n=1):
    return versions.create(
        store,
        actor,
        summary=f"policy {n}",
        commit_sha=f"{n:040x}",
        branch="main",
        artifact=ARTIFACT,
        based_on=[],
    )


def make_methods(baseline="V-1", candidate=None, **overrides):
    methods = {
        "baseline": baseline,
        "candidate": candidate,
        "procedure": "Build the retreat change on the task branch; run 40 paired episodes per arm",
        "metrics": [PRIMARY, GUARDRAIL],
        "decision_rule": {
            "text": "confirmed when win_rate rises by at least 0.02 with the interval above zero",
            "n_min": 40,
            "k": 0.5,
        },
    }
    methods.update(overrides)
    return methods


def create(store, actor, **overrides):
    fields = {
        "summary": "Does retreating at 30% health raise the win rate?",
        "hypothesis": HYPOTHESIS,
        "methods": make_methods(),
    }
    fields.update(overrides)
    return experiments.create(store, actor, **fields)


def started(store, actor, **overrides):
    record = create(store, actor, **overrides)
    return experiments.start(store, actor, record["id"])


def add_run(store, actor, experiment_id=1, **overrides):
    fields = {"kind": "xp", "platform_request_id": "xreq_a", "arm": "baseline"}
    fields.update(overrides)
    return experiments.add_run(store, actor, experiment_id, **fields)


def event_kinds(store):
    return [e.kind for e in store.events_after(0)]


def problems_text(exc_info) -> str:
    return "\n".join(exc_info.value.problems)


def insert_task(store, actor) -> None:
    """A goal and an in-progress Task written directly: the tasks module is not needed here."""
    with store.tx(actor) as tx:
        tx.execute(
            "INSERT INTO goals(title, end_state, reason, status, created, created_by, updated)"
            " VALUES ('g', 'e', 'r', 'active', ?, 'harness', ?)",
            (tx.at, tx.at),
        )
        tx.execute(
            "INSERT INTO tasks(goal_id, type, title, description, status, created, created_by,"
            " updated) VALUES (1, 'improvement', 't', 'd', 'in-progress', ?, 'harness', ?)",
            (tx.at, tx.at),
        )
        tx.event("task.created", "T-1", {})


def install_fake_tasks(monkeypatch, calls: list, *, stale_epoch: int):
    """Stand in for ``tasks.require_holder``: refuse a session actor at ``stale_epoch``."""
    fake = types.ModuleType("research_core.store.tasks")

    def require_holder(store, task_id, actor):
        calls.append((task_id, actor))
        if actor.epoch == stale_epoch:
            raise LeaseError([f"T-{task_id} is held at a newer epoch than this Session's"])

    fake.require_holder = require_holder
    monkeypatch.setitem(sys.modules, "research_core.store.tasks", fake)
    monkeypatch.setattr(store_package, "tasks", fake, raising=False)


def test_create_is_proposed_with_normalised_references(coworld_store, session_actor):
    make_version(coworld_store, session_actor)
    make_version(coworld_store, session_actor, 2)
    record = create(
        coworld_store,
        session_actor,
        methods=make_methods(baseline=1, candidate="V-2"),
        tags=("retreat",),
    )
    assert record["id"] == "X-1"
    assert record["status"] == "proposed"
    assert record["summary"] == "Does retreating at 30% health raise the win rate?"
    assert record["hypothesis"] == HYPOTHESIS
    assert record["methods"] == make_methods(baseline="V-1", candidate="V-2")
    assert record["tags"] == ["retreat"]
    assert record["results"] == {}
    assert record["runs"] == []
    assert record["cited_by"] == []
    assert record["related_claims"] == [] and record["related_experiments"] == []
    assert record["design_sha256"] is None
    assert record["goal_id"] is None and record["task_id"] is None
    assert record["created"] == T0 and record["closed"] is None
    assert record["created_by"] == "session:api_1761690245_0badcafe"
    assert record["versions_tested"] == [
        {"id": "V-1", "role": "baseline"},
        {"id": "V-2", "role": "candidate"},
    ]
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "experiment.created" and event.subject == "X-1"
    assert event.payload["summary"] == record["summary"]
    assert coworld_store.unmarked_changes() == []


def test_create_reports_every_problem_at_once(coworld_store, session_actor):
    make_version(coworld_store, session_actor)
    bad_hypothesis = {**HYPOTHESIS, "if_false": HYPOTHESIS["if_true"], "lever": "retreat"}
    bad_methods = make_methods(
        baseline="V-9",
        metrics=[
            {"name": "win_rate", "direction": "sideways", "role": "guardrail"},
            {"name": "win_rate", "direction": "up", "role": "performance", "bogus": 1},
        ],
        decision_rule={"text": "", "n_min": 0, "k": -1},
    )
    with pytest.raises(StoreError) as exc_info:
        experiments.create(
            coworld_store,
            session_actor,
            summary="",
            goal_id=7,
            task_id="T-3",
            tags="retreat",
            hypothesis=bad_hypothesis,
            methods=bad_methods,
            related_claims=["C-4"],
            related_experiments=["X-2"],
        )
    text = problems_text(exc_info)
    for fragment in (
        "summary is required",
        "hypothesis.if_true and if_false must differ",
        "hypothesis.lever is not a hypothesis field",
        "methods.baseline: V-9 does not exist",
        "methods.metrics[0].direction must be up or down",
        "methods.metrics[1].bogus is not a metric field",
        "methods.metrics needs at least one primary metric",
        "methods.metrics names must be unique (win_rate repeats)",
        "methods.decision_rule.text is required",
        "methods.decision_rule.n_min must be a positive integer",
        "goal_id: G-7 does not exist",
        "task_id: T-3 does not exist",
        "tags must be a list of words",
        "related_claims: C-4 does not exist",
        "related_experiments: X-2 does not exist",
    ):
        assert fragment in text, fragment
    assert coworld_store.q("SELECT * FROM experiments") == []


def test_create_refuses_a_long_summary_and_a_candidate_equal_to_the_baseline(
    coworld_store, session_actor
):
    make_version(coworld_store, session_actor)
    with pytest.raises(StoreError) as exc_info:
        create(
            coworld_store,
            session_actor,
            summary=" ".join(["word"] * 41),
            methods=make_methods(candidate="V-1"),
        )
    assert exc_info.value.problems == [
        "summary must be at most 40 words (41 given)",
        "methods.candidate must differ from methods.baseline",
    ]
    with pytest.raises(StoreError) as exc_info:
        experiments.create(coworld_store, session_actor, summary="x", hypothesis=None, methods=[])
    assert exc_info.value.problems == [
        "hypothesis must be an object with statement, if_true, if_false and motivation",
        "methods must be an object with baseline, candidate, procedure, metrics and decision_rule",
    ]


def test_a_proposed_experiment_may_be_incomplete_until_start(coworld_store, session_actor):
    make_version(coworld_store, session_actor)
    record = experiments.create(
        coworld_store,
        session_actor,
        summary="Draft: retreat threshold",
        hypothesis={"statement": "Retreating earlier saves units"},
        methods={"baseline": "V-1"},
    )
    assert record["status"] == "proposed"
    assert experiments.missing_design_parts(record["hypothesis"], record["methods"]) == [
        "hypothesis.if_true is missing",
        "hypothesis.if_false is missing",
        "hypothesis.motivation is missing",
        "methods.procedure is missing",
        "methods.metrics is missing",
        "methods.decision_rule is missing",
    ]
    with pytest.raises(StoreError) as exc_info:
        experiments.start(coworld_store, session_actor, "X-1")
    assert exc_info.value.problems == [
        "hypothesis.if_true is missing",
        "hypothesis.if_false is missing",
        "hypothesis.motivation is missing",
        "methods.procedure is missing",
        "methods.metrics is missing",
        "methods.decision_rule is missing",
    ]
    assert experiments.read(coworld_store, 1)["status"] == "proposed"
    experiments.update(
        coworld_store, session_actor, 1, hypothesis=HYPOTHESIS, methods=make_methods()
    )
    assert experiments.start(coworld_store, session_actor, 1)["status"] == "running"


def test_start_locks_the_design(coworld_store, session_actor):
    make_version(coworld_store, session_actor)
    record = started(coworld_store, session_actor)
    canonical = json.dumps(
        {"hypothesis": HYPOTHESIS, "methods": make_methods()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert record["status"] == "running"
    assert record["design_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert record["design_sha256"] == experiments.design_sha256(HYPOTHESIS, make_methods())
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "experiment.started"
    assert event.payload == {"design_sha256": record["design_sha256"]}
    with pytest.raises(StoreError) as exc_info:
        experiments.update(coworld_store, session_actor, "X-1", summary="A new summary")
    assert exc_info.value.problems == [
        "X-1 is running; its design is locked (abandon it and create a new Experiment that"
        " lists it in related_experiments)"
    ]
    with pytest.raises(StoreError) as exc_info:
        experiments.start(coworld_store, session_actor, "X-1")
    assert exc_info.value.problems == ["X-1 is running; only a proposed Experiment can start"]
    assert experiments.read(coworld_store, 1)["summary"] == record["summary"]


def test_update_while_proposed_replaces_the_given_sections(coworld_store, session_actor):
    make_version(coworld_store, session_actor)
    make_version(coworld_store, session_actor, 2)
    create(coworld_store, session_actor)
    new_hypothesis = {**HYPOTHESIS, "statement": "Retreating at 40% health ends fights earlier"}
    record = experiments.update(
        coworld_store,
        session_actor,
        1,
        summary="Does retreating at 40% health raise the win rate?",
        hypothesis=new_hypothesis,
        tags=["retreat", "health"],
        related_experiments=[],
    )
    assert record["summary"] == "Does retreating at 40% health raise the win rate?"
    assert record["hypothesis"] == new_hypothesis
    assert record["tags"] == ["retreat", "health"]
    assert record["methods"] == make_methods()
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "experiment.updated"
    assert event.payload == {"fields": ["hypothesis", "related_experiments", "summary", "tags"]}
    record = experiments.update(
        coworld_store, session_actor, 1, methods=make_methods(candidate="V-2")
    )
    assert record["methods"]["candidate"] == "V-2"
    with pytest.raises(StoreError) as exc_info:
        experiments.update(coworld_store, session_actor, 1)
    assert exc_info.value.problems == ["nothing to update"]
    with pytest.raises(StoreError) as exc_info:
        experiments.update(
            coworld_store,
            session_actor,
            1,
            hypothesis={**HYPOTHESIS, "if_false": HYPOTHESIS["if_true"]},
            methods={"baseline": "V-7"},
        )
    assert exc_info.value.problems == [
        "hypothesis.if_true and if_false must differ, or the Experiment cannot tell us anything",
        "methods.baseline: V-7 does not exist",
    ]
    with pytest.raises(NotFound):
        experiments.update(coworld_store, session_actor, 9, summary="x")


def test_abandon_keeps_the_record_and_the_replacement_names_it(coworld_store, session_actor):
    make_version(coworld_store, session_actor)
    started(coworld_store, session_actor)
    with pytest.raises(StoreError) as exc_info:
        experiments.abandon(coworld_store, session_actor, "X-1", reason=" ")
    assert exc_info.value.problems == ["reason is required"]
    record = experiments.abandon(
        coworld_store, session_actor, "X-1", reason="the guardrail metric was the wrong one"
    )
    assert record["status"] == "abandoned"
    assert record["abandon_reason"] == "the guardrail metric was the wrong one"
    assert record["closed"] == T0
    assert record["design_sha256"] is not None
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "experiment.abandoned"
    assert event.payload == {"reason": "the guardrail metric was the wrong one"}
    with pytest.raises(StoreError) as exc_info:
        experiments.abandon(coworld_store, session_actor, 1, reason="again")
    assert exc_info.value.problems == [
        "X-1 is abandoned; only a proposed or running Experiment can be abandoned"
    ]
    with pytest.raises(StoreError) as exc_info:
        add_run(coworld_store, session_actor)
    assert exc_info.value.problems == ["X-1 is abandoned; runs are added only while it is running"]
    replacement = create(coworld_store, session_actor, related_experiments=["X-1"])
    assert replacement["id"] == "X-2" and replacement["related_experiments"] == ["X-1"]
    with pytest.raises(StoreError) as exc_info:
        create(coworld_store, session_actor, related_experiments=["X-9"])
    assert exc_info.value.problems == ["related_experiments: X-9 does not exist"]
    assert [row["id"] for row in coworld_store.q("SELECT id FROM experiments ORDER BY id")] == [
        1,
        2,
    ]
    # A proposed Experiment can be abandoned too; the design hash stays unset.
    abandoned = experiments.abandon(coworld_store, session_actor, 2, reason="superseded by X-1")
    assert abandoned["status"] == "abandoned" and abandoned["design_sha256"] is None


def test_add_run_only_while_running(coworld_store, session_actor):
    make_version(coworld_store, session_actor)
    create(coworld_store, session_actor)
    with pytest.raises(StoreError) as exc_info:
        add_run(coworld_store, session_actor)
    assert exc_info.value.problems == ["X-1 is proposed; runs are added only while it is running"]
    experiments.start(coworld_store, session_actor, 1)
    with pytest.raises(StoreError) as exc_info:
        add_run(
            coworld_store,
            session_actor,
            kind="",
            platform_request_id=" ",
            arm=None,
            seed="7",
            episodes_total=0,
        )
    assert exc_info.value.problems == [
        "kind is required",
        "platform_request_id is required",
        "arm is required",
        "seed must be an integer",
        "episodes_total must be a positive integer",
    ]
    run = add_run(coworld_store, session_actor, seed=7, episodes_total=40)
    assert run == {
        "id": 1,
        "experiment_id": "X-1",
        "kind": "xp",
        "platform_request_id": "xreq_a",
        "arm": "baseline",
        "seed": 7,
        "validity": "valid",
        "state": None,
        "episodes_total": 40,
        "episodes_completed": None,
        "results": None,
        "created": T0,
        "updated": T0,
    }
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "experiment.run" and event.subject == "X-1"
    assert event.payload == {
        "run_id": 1,
        "action": "added",
        "kind": "xp",
        "platform_request_id": "xreq_a",
        "arm": "baseline",
        "seed": 7,
    }
    second = add_run(coworld_store, session_actor, platform_request_id="xreq_b", arm="candidate")
    assert experiments.read(coworld_store, 1)["runs"] == [run, second]
    with pytest.raises(NotFound):
        add_run(coworld_store, session_actor, experiment_id="X-5")


def test_update_run_records_progress_and_validity(coworld_store, session_actor, clock):
    make_version(coworld_store, session_actor)
    started(coworld_store, session_actor)
    run = add_run(coworld_store, session_actor, arm="candidate", episodes_total=40)
    with pytest.raises(StoreError) as exc_info:
        experiments.update_run(
            coworld_store,
            session_actor,
            run["id"],
            state="",
            episodes_completed=-1,
            results=["not", "a", "dict"],
            validity="broken",
        )
    assert exc_info.value.problems == [
        "state must be text",
        "episodes_completed must be a whole number",
        "results must be an object",
        "validity must be valid or invalid: <reason>",
    ]
    with pytest.raises(StoreError) as exc_info:
        experiments.update_run(coworld_store, session_actor, run["id"])
    assert exc_info.value.problems == ["nothing to update"]
    clock.advance(30)
    updated = experiments.update_run(
        coworld_store,
        session_actor,
        run["id"],
        state="completed",
        episodes_completed=40,
        results={"win_rate": 0.55},
        validity="invalid: the opponent pool changed mid-run",
    )
    assert updated["state"] == "completed"
    assert updated["episodes_completed"] == 40
    assert updated["results"] == {"win_rate": 0.55}
    assert updated["validity"] == "invalid: the opponent pool changed mid-run"
    assert updated["created"] == T0 and updated["updated"] == "2026-10-28T21:04:35.123456Z"
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "experiment.run" and event.subject == "X-1"
    assert event.payload == {
        "run_id": 1,
        "action": "updated",
        "fields": ["episodes_completed", "results", "state", "validity"],
    }
    revalidated = experiments.update_run(coworld_store, session_actor, 1, validity="valid")
    assert revalidated["validity"] == "valid"
    with pytest.raises(NotFound):
        experiments.update_run(coworld_store, session_actor, 99, state="x")


def test_close_requirements_are_reported_together(coworld_store, session_actor):
    make_version(coworld_store, session_actor)
    started(coworld_store, session_actor)
    with pytest.raises(StoreError) as exc_info:
        experiments.close(
            coworld_store,
            session_actor,
            1,
            verdict="maybe",
            measurements=[],
            interpretation="",
            reason_codes="n_short",
        )
    assert exc_info.value.problems == [
        "X-1 has no runs; an Experiment closes only after at least one run: record runs with"
        " xp.request, or experiment.abandon {experiment: X-1, reason} when none will run",
        "verdict must be confirmed, refuted or inconclusive",
        "measurements must be a non-empty list of {metric, baseline, candidate, n, effect}",
        "interpretation is required",
        "reason_codes must be a list of codes",
    ]
    add_run(coworld_store, session_actor)
    with pytest.raises(StoreError) as exc_info:
        experiments.close(
            coworld_store,
            session_actor,
            1,
            verdict="confirmed",
            measurements=[{"metric": "elo", "baseline": "x", "candidate": 1, "n": 0}, "bad"],
            interpretation="fine",
            reason_codes=["ok", 3],
        )
    assert exc_info.value.problems == [
        "measurements[0].metric 'elo' is not one of the Experiment's metrics (step_ms, win_rate)",
        "measurements[0].baseline must be a number",
        "measurements[0].n must be a positive integer",
        "measurements[0].effect is missing",
        "measurements[1] must be an object",
        "primary metric win_rate has no measurement",
        "reason_codes must be a list of codes",
    ]
    assert experiments.read(coworld_store, 1)["status"] == "running"
    with pytest.raises(NotFound):
        experiments.close(
            coworld_store,
            session_actor,
            4,
            verdict="refuted",
            measurements=MEASUREMENTS,
            interpretation="x",
            reason_codes=[],
        )


@pytest.mark.parametrize("verdict", ["confirmed", "refuted", "inconclusive"])
def test_close_records_the_verdict_and_freezes_the_runs(
    coworld_store, session_actor, clock, verdict
):
    make_version(coworld_store, session_actor)
    started(coworld_store, session_actor)
    add_run(coworld_store, session_actor)
    clock.advance(3600)
    measurements = [{**MEASUREMENTS[0], "ci": [0.01, 0.05]}, MEASUREMENTS[1]]
    record = experiments.close(
        coworld_store,
        session_actor,
        "X-1",
        verdict=verdict,
        measurements=measurements,
        interpretation="The win rate rose three points with the interval above zero.",
        reason_codes=["primary_not_significant:win_rate"] if verdict != "confirmed" else [],
    )
    assert record["status"] == verdict
    assert record["closed"] == "2026-10-28T22:04:05.123456Z"
    assert record["results"] == {
        "verdict": verdict,
        "measurements": measurements,
        "interpretation": "The win rate rose three points with the interval above zero.",
        "reason_codes": [] if verdict == "confirmed" else ["primary_not_significant:win_rate"],
    }
    event = coworld_store.events_after(0)[-1]
    assert event.kind == "experiment.closed" and event.subject == "X-1"
    assert event.payload == {"verdict": verdict, "reason_codes": record["results"]["reason_codes"]}
    assert experiments.read(coworld_store, 1, section="results") == {
        "id": "X-1",
        "status": verdict,
        "results": {**record["results"], "runs": record["runs"]},
    }
    with pytest.raises(StoreError) as exc_info:
        experiments.close(
            coworld_store,
            session_actor,
            1,
            verdict=verdict,
            measurements=measurements,
            interpretation="again",
            reason_codes=[],
        )
    assert exc_info.value.problems == [
        f"X-1 is {verdict}; an Experiment closes only while it is running"
    ]
    with pytest.raises(StoreError) as exc_info:
        experiments.update_run(coworld_store, session_actor, 1, state="late")
    assert exc_info.value.problems == [f"X-1 is {verdict}; runs change only while it is running"]
    with pytest.raises(StoreError):
        experiments.abandon(coworld_store, session_actor, 1, reason="too late")


def test_read_sections_cited_by_and_versions_tested(coworld_store, session_actor, harness):
    make_version(coworld_store, session_actor)
    make_version(coworld_store, session_actor, 2)
    create(coworld_store, session_actor, methods=make_methods(candidate="V-2"))
    versions.create(
        coworld_store,
        session_actor,
        summary="built for X-1",
        commit_sha="c" * 40,
        branch="task/1",
        artifact=ARTIFACT,
        based_on=["V-1"],
        experiment_id="X-1",
    )
    # A claim citing the Experiment, written directly: the claims module is not needed here.
    with coworld_store.tx(harness) as tx:
        tx.execute(
            "INSERT INTO claims(summary, body, tags, status, see_also, created, created_by,"
            " updated) VALUES ('Retreating early saves units', '', '[]', 'active', '[]', ?,"
            " 'harness', ?)",
            (tx.at, tx.at),
        )
        for ref, stance, note in (
            ("X-1", "supports", "the confirmed run"),
            ("X-1#results", "qualifies", ""),
            ("X-10", "supports", "another experiment"),
        ):
            tx.execute(
                'INSERT INTO claim_evidence(claim_id, type, ref, stance, note, added, "by")'
                " VALUES (1, 'experiment', ?, ?, ?, ?, 'harness')",
                (ref, stance, note, tx.at),
            )
        tx.event("claim.created", "C-1", {})
    record = experiments.read(coworld_store, "X-1")
    assert record["cited_by"] == [
        {"claim": "C-1", "ref": "X-1", "stance": "supports", "note": "the confirmed run"},
        {"claim": "C-1", "ref": "X-1#results", "stance": "qualifies", "note": ""},
    ]
    assert record["versions_tested"] == [
        {"id": "V-1", "role": "baseline"},
        {"id": "V-2", "role": "candidate"},
        {"id": "V-3", "role": "built"},
    ]
    assert experiments.read(coworld_store, 1, section="hypothesis") == {
        "id": "X-1",
        "status": "proposed",
        "hypothesis": HYPOTHESIS,
    }
    assert experiments.read(coworld_store, 1, section="methods") == {
        "id": "X-1",
        "status": "proposed",
        "methods": make_methods(candidate="V-2"),
    }
    assert experiments.read(coworld_store, 1, section="results") == {
        "id": "X-1",
        "status": "proposed",
        "results": {"runs": []},
    }
    with pytest.raises(StoreError) as exc_info:
        experiments.read(coworld_store, 1, section="runs")
    assert exc_info.value.problems == [
        "section must be hypothesis, methods or results (not 'runs')"
    ]
    with pytest.raises(NotFound):
        experiments.read(coworld_store, 99)
    with pytest.raises(StoreError) as exc_info:
        experiments.read(coworld_store, "G-1")
    assert exc_info.value.problems == ["'G-1' is not an Experiment reference (X-<n>)"]


def test_search_matches_words_and_survives_query_operators(coworld_store, session_actor):
    make_version(coworld_store, session_actor)
    create(coworld_store, session_actor, tags=["retreat"])
    # The second Experiment shares no words with the first beyond "the" (FTS indexes the
    # hypothesis and methods too, and splits win_rate into two words).
    experiments.create(
        coworld_store,
        session_actor,
        summary="Does a wider formation hold the lane longer?",
        hypothesis={},
        methods={"baseline": "V-1"},
        tags=["formation"],
    )
    assert [r["id"] for r in experiments.search(coworld_store, "retreating")] == ["X-1"]
    assert [r["id"] for r in experiments.search(coworld_store, "formation")] == ["X-2"]
    assert [r["id"] for r in experiments.search(coworld_store, '"win rate* (')] == ["X-1"]
    # Every word is required, so a stray operator word such as OR is a word to match too.
    assert experiments.search(coworld_store, "retreating OR formation") == []
    assert experiments.search(coworld_store, "retreating formation") == []
    assert experiments.search(coworld_store, "--- ( ) *") == []
    assert experiments.search(coworld_store, "") == []
    assert len(experiments.search(coworld_store, "the", limit=1)) == 1
    hit = experiments.search(coworld_store, "formation")[0]
    assert hit == {
        "id": "X-2",
        "summary": "Does a wider formation hold the lane longer?",
        "status": "proposed",
        "tags": ["formation"],
        "created": T0,
        "closed": None,
    }
    # Abandoned Experiments stay searchable: a failed test is the record that stops a repeat.
    experiments.abandon(coworld_store, session_actor, 2, reason="not worth it")
    assert experiments.search(coworld_store, "formation")[0]["status"] == "abandoned"


def test_fts_match_quotes_every_word(coworld_store):
    assert experiments.fts_match('win rate" OR (NEAR x*)') == '"win" "rate" "OR" "NEAR" "x"'
    assert experiments.fts_match("   ") is None


def test_task_scoped_writes_require_the_task_holder(
    coworld_store, session_actor, harness, monkeypatch
):
    make_version(coworld_store, session_actor)
    insert_task(coworld_store, harness)
    record = started(coworld_store, session_actor, task_id="T-1")
    assert record["task_id"] == 1
    calls: list = []
    install_fake_tasks(monkeypatch, calls, stale_epoch=1)
    stale = Actor.session(session_actor.id, epoch=1)
    with pytest.raises(LeaseError):
        add_run(coworld_store, stale)
    assert coworld_store.q("SELECT * FROM experiment_runs") == []
    run = add_run(coworld_store, session_actor)
    with pytest.raises(LeaseError):
        experiments.update_run(coworld_store, stale, run["id"], state="x")
    with pytest.raises(LeaseError):
        experiments.close(
            coworld_store,
            stale,
            1,
            verdict="inconclusive",
            measurements=MEASUREMENTS,
            interpretation="x",
            reason_codes=["n_short:1<40"],
        )
    assert calls == [(1, stale), (1, session_actor), (1, stale), (1, stale)]
    assert experiments.read(coworld_store, 1)["status"] == "running"
    assert experiments.read(coworld_store, 1)["runs"][0]["state"] is None
    # Without a Task there is no holder to check.
    started(coworld_store, session_actor)
    add_run(coworld_store, stale, experiment_id=2)
    assert len(calls) == 4
