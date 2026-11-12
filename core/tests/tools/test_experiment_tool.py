"""The experiment tool: lifecycle through the tool, the computed verdict, the explicit verdict."""

from __future__ import annotations

import pytest

import research_core.tools.experiment as experiment_tool
import research_core.tools.goal  # noqa: F401  (registers the goal tool for the Goal-ref test)
from research_core.store import experiments, versions

ARTIFACT = {"kind": "file", "path": "dist/policy.bin", "digest": "sha256:" + "0" * 64, "size": 2048}
HYPOTHESIS = {
    "statement": "Retreating at 30% health loses fewer units because fights end earlier",
    "if_true": "the candidate's win rate rises by at least two points",
    "if_false": "the win rate stays within noise of the baseline",
    "motivation": "Nothing has measured the win-rate effect of early retreats",
}
PRIMARY = {"name": "win_rate", "direction": "up", "role": "primary", "min_effect": 0.02}
GUARDRAIL = {"name": "step_ms", "direction": "down", "role": "guardrail", "tolerance": 5}
MEASUREMENTS = [
    {"metric": "win_rate", "baseline": 0.50, "candidate": 0.53, "n": 40, "effect": 0.03},
    {"metric": "step_ms", "baseline": 12.0, "candidate": 12.5, "n": 40, "effect": -0.5},
]
N_MIN = 12


def methods(baseline="V-1", candidate="V-2", n_min=N_MIN, **overrides):
    value = {
        "baseline": baseline,
        "candidate": candidate,
        "procedure": "Run one paired episode per seed on each arm",
        "metrics": [PRIMARY, GUARDRAIL],
        "decision_rule": {"text": "win_rate up by 0.02 with the CI above zero", "n_min": n_min},
    }
    value.update(overrides)
    return value


def create_args(**overrides):
    args = {
        "action": "create",
        "summary": "Does retreating at 30% health raise the win rate?",
        "hypothesis": HYPOTHESIS,
        "methods": methods(),
        "tags": ["retreat"],
    }
    args.update(overrides)
    return args


def row(policy, seed, win_rate, seats=(0, 1), step_ms=12.0, game_version="65", **extra):
    return {
        "episode_id": f"{policy}:{seed}:{','.join(map(str, seats))}",
        "game_version": game_version,
        "policy_version": policy,
        "seed": seed,
        "seats": list(seats),
        "opponents": ["pv-opp"],
        "metrics": {"win_rate": win_rate, "step_ms": step_ms},
        **extra,
    }


@pytest.fixture
def two_versions(ctx_factory):
    """V-1 and V-2 in the active Coworld's store."""
    ctx = ctx_factory()
    for n in (1, 2):
        versions.create(
            ctx.store,
            ctx.actor,
            summary=f"policy {n}",
            commit_sha=f"{n:040x}",
            branch="main",
            artifact=ARTIFACT,
            based_on=[],
        )


@pytest.fixture
def running(call, two_versions):
    """X-1, started, as a Researcher's Experiment (no Task, so no lease is involved)."""
    created = call("experiment", create_args(), role="researcher")
    assert created["ok"] is True, created
    started = call("experiment", {"action": "start", "experiment": "X-1"}, role="researcher")
    assert started["record"]["status"] == "running", started
    return started["record"]


@pytest.fixture
def add_run(ctx_factory):
    """Records a run on X-1 the way xp.request does when the platform accepts a request."""
    ctx = ctx_factory("researcher")

    def add(**fields):
        return experiments.add_run(ctx.store, ctx.actor, "X-1", **fields)

    return add


def record_runs(add_run, call, rows_by_arm: dict[str, list[dict]]):
    """One run per row per arm, its rows stored through update_run; returns the run ids."""
    run_ids = []
    for arm, rows in rows_by_arm.items():
        for index, data in enumerate(rows):
            added = add_run(
                kind="xp", platform_request_id=f"xreq_{arm}_{index}", arm=arm, seed=data["seed"]
            )
            assert added["arm"] == arm
            updated = call(
                "experiment",
                {
                    "action": "update_run",
                    "run": added["id"],
                    "state": "completed",
                    "episodes_completed": 1,
                    "results": {"rows": [data]},
                },
                role="researcher",
            )
            assert updated["ok"] is True, updated
            run_ids.append(added["id"])
    return run_ids


# --- lifecycle -----------------------------------------------------------------------------


def test_create_read_update_start(call, two_versions):
    created = call("experiment", create_args(methods=methods(candidate=None)))
    assert created["ok"] is True, created
    record = created["record"]
    assert record["id"] == "X-1" and record["status"] == "proposed"
    assert record["methods"]["baseline"] == "V-1" and record["runs"] == []

    hypothesis = call("experiment", {"action": "read", "experiment": 1, "section": "hypothesis"})
    assert hypothesis["record"] == {"id": "X-1", "status": "proposed", "hypothesis": HYPOTHESIS}
    results = call("experiment", {"action": "read", "experiment": "X-1", "section": "results"})
    assert results["record"]["results"] == {"runs": []}
    bad_section = call("experiment", {"action": "read", "experiment": "X-1", "section": "runs"})
    assert bad_section["ok"] is False and "section must be one of" in bad_section["error"]

    updated = call(
        "experiment",
        {"action": "update", "experiment": "X-1", "methods": methods(candidate=2), "tags": ["a"]},
    )
    assert updated["ok"] is True, updated
    assert updated["record"]["methods"]["candidate"] == "V-2"
    assert updated["record"]["tags"] == ["a"]

    started = call("experiment", {"action": "start", "experiment": "X-1"})
    assert started["record"]["status"] == "running"
    assert started["record"]["design_sha256"]

    locked = call("experiment", {"action": "update", "experiment": "X-1", "summary": "Changed?"})
    assert locked["ok"] is False and "design is locked" in locked["error"]


def test_start_refusal_lists_every_missing_part(call, two_versions):
    partial = {"statement": HYPOTHESIS["statement"], "if_true": "x", "if_false": "y"}
    created = call(
        "experiment",
        create_args(hypothesis=partial, methods={"baseline": "V-1", "metrics": [PRIMARY]}),
    )
    assert created["ok"] is True, created
    started = call("experiment", {"action": "start", "experiment": "X-1"})
    assert started["ok"] is False
    assert {
        "hypothesis.motivation is missing",
        "methods.procedure is missing",
        "methods.decision_rule is missing",
    } <= set(started["problems"])


def test_create_refs_goal_and_task_and_rejects_wrong_kinds(call, two_versions):
    goal = call(
        "goal",
        {"action": "create", "title": "t", "end_state": "e", "reason": "r"},
    )
    assert goal["ok"] is True
    created = call("experiment", create_args(goal="G-1"))
    assert created["record"]["goal_id"] == 1
    call("experiment", create_args())
    # A Goal's Experiments, listed without words to search for.
    of_goal = call("experiment", {"action": "search", "goal": "G-1"})
    assert [r["id"] for r in of_goal["records"]] == ["X-1"]
    assert [r["id"] for r in call("experiment", {"action": "search"})["records"]] == ["X-2", "X-1"]
    wrong = call("experiment", create_args(goal="T-1"))
    assert wrong["ok"] is False and "goal must be a goal reference" in wrong["error"]


def test_bad_experiment_reference_is_a_clean_refusal(call, two_versions):
    missing = call("experiment", {"action": "read", "experiment": "X-99"})
    assert missing["ok"] is False and "X-99 does not exist" in missing["error"]
    wrong_kind = call("experiment", {"action": "start", "experiment": "G-1"})
    assert wrong_kind["ok"] is False and "experiment reference" in wrong_kind["error"]
    no_run = call("experiment", {"action": "update_run", "run": 7, "state": "done"})
    assert no_run["ok"] is False and "run 7 does not exist" in no_run["error"]


def test_search_and_abandon(call, two_versions):
    call("experiment", create_args())
    found = call("experiment", {"action": "search", "query": "retreating win rate"})
    assert [r["id"] for r in found["records"]] == ["X-1"]
    assert call("experiment", {"action": "search", "query": "zeppelin"})["records"] == []
    listed = call("experiment", {"action": "search"})
    assert listed["ok"] is True and [r["id"] for r in listed["records"]] == ["X-1"]

    abandoned = call("experiment", {"action": "abandon", "experiment": "X-1", "reason": "moot"})
    assert abandoned["record"]["status"] == "abandoned"
    assert abandoned["record"]["abandon_reason"] == "moot"
    again = call("experiment", {"action": "abandon", "experiment": "X-1", "reason": "moot"})
    assert again["ok"] is False and "only a proposed or running" in again["error"]


def test_researcher_may_use_every_action(call, two_versions):
    spec = experiment_tool.SPEC
    names = {action.name for action in spec.actions}
    assert names == {
        "create",
        "read",
        "search",
        "update",
        "start",
        "update_run",
        "close",
        "abandon",
    }
    assert {action.name for action in spec.actions_for("researcher")} == names
    assert spec.executor == "workspace"
    assert call("experiment", create_args(), role="researcher")["ok"] is True


# --- runs and the computed verdict -----------------------------------------------------------


def test_update_run(add_run, call, running):
    added = add_run(
        kind="xp", platform_request_id="xreq_1", arm="baseline", seed=7, episodes_total=1
    )
    run = added
    assert run["id"] == 1 and run["experiment_id"] == "X-1" and run["validity"] == "valid"

    updated = call(
        "experiment",
        {"action": "update_run", "run": 1, "validity": "invalid: the platform lost the replay"},
        role="researcher",
    )
    assert updated["record"]["validity"] == "invalid: the platform lost the replay"
    read = call("experiment", {"action": "read", "experiment": "X-1", "section": "results"})
    assert [r["id"] for r in read["record"]["results"]["runs"]] == [1]

    nothing = call("experiment", {"action": "update_run", "run": 1}, role="researcher")
    assert nothing["ok"] is False and "nothing to update" in nothing["error"]


def test_computed_close_confirms_a_clear_margin(add_run, call, running):
    seeds = range(N_MIN)
    record_runs(
        add_run,
        call,
        {
            "baseline": [row("V-1", s, 0.50 + (0.01 if s % 2 else -0.01)) for s in seeds],
            "candidate": [row("V-2", s, 0.60 + (0.01 if s % 2 else -0.01)) for s in seeds],
        },
    )
    closed = call(
        "experiment", {"action": "close", "experiment": "X-1", "interpretation": "A clear win."}
    )
    assert closed["ok"] is True, closed
    record = closed["record"]
    assert record["status"] == "confirmed"
    results = record["results"]
    assert results["verdict"] == "confirmed" and results["reason_codes"] == []
    assert results["interpretation"] == "A clear win."
    by_metric = {m["metric"]: m for m in results["measurements"]}
    assert set(by_metric) == {"win_rate", "step_ms"}
    win = by_metric["win_rate"]
    assert win["n"] == N_MIN and win["passed"] is True
    assert abs(win["effect"] - 0.10) < 1e-9 and win["effect"] == win["mean_signed_diff"]
    assert abs(win["baseline"] - 0.50) < 1e-9 and abs(win["candidate"] - 0.60) < 1e-9
    assert win["ci"][0] > 0 and win["threshold"] == 0.02
    assert by_metric["step_ms"]["ci"] is None and by_metric["step_ms"]["passed"] is True
    assert closed["evaluation"]["pairs"] == N_MIN
    assert closed["evaluation"]["rows"]["kept"] == 2 * N_MIN


def test_computed_close_with_two_seeds_is_inconclusive(add_run, call, running):
    record_runs(
        add_run,
        call,
        {
            "baseline": [row("V-1", s, 0.50) for s in range(2)],
            "candidate": [row("V-2", s, 0.70) for s in range(2)],
        },
    )
    closed = call("experiment", {"action": "close", "experiment": "X-1"})
    assert closed["ok"] is True, closed
    assert closed["record"]["status"] == "inconclusive"
    results = closed["record"]["results"]
    assert results["reason_codes"] == [f"n_short:2<{N_MIN}"]
    assert results["interpretation"].startswith("inconclusive")
    assert "n_short" in results["interpretation"]


def test_computed_close_refutes_a_guardrail_breach(add_run, call, running):
    seeds = range(N_MIN)
    record_runs(
        add_run,
        call,
        {
            "baseline": [row("V-1", s, 0.50, step_ms=12.0) for s in seeds],
            "candidate": [
                row("V-2", s, 0.60 + (0.01 if s % 2 else -0.01), step_ms=20.0) for s in seeds
            ],
        },
    )
    closed = call("experiment", {"action": "close", "experiment": "X-1"})
    assert closed["ok"] is True, closed
    assert closed["record"]["status"] == "refuted"
    assert closed["record"]["results"]["reason_codes"] == ["guardrail_breach:step_ms"]


def test_computed_close_counts_only_the_runs_own_seat(add_run, call, running):
    """Rows of seat 1 are the opponent's, stored before xp.status kept seats apart. Averaging
    them in measured a mix of us and the opponent (here 0.50 against 0.60, not 0.40 against
    0.55), so only seat 0 counts and the rest are reported as dropped."""
    seeds = range(N_MIN)
    baseline, candidate = [], []
    for s in seeds:
        baseline += [row("V-1", s, 0.40, seats=(0,)), row("V-1", s, 0.60, seats=(1,))]
        candidate += [row("V-2", s, 0.55, seats=(0,)), row("V-2", s, 0.65, seats=(1,))]
    # Two rows per run here: a run holds whatever rows its request produced.
    for arm, rows in (("baseline", baseline), ("candidate", candidate)):
        for s in seeds:
            added = add_run(kind="xp", platform_request_id=f"xreq_{arm}_{s}", arm=arm, seed=s)
            call(
                "experiment",
                {
                    "action": "update_run",
                    "run": added["id"],
                    "state": "completed",
                    "results": {"rows": [r for r in rows if r["seed"] == s]},
                },
                role="researcher",
            )
    closed = call("experiment", {"action": "close", "experiment": "X-1"})
    assert closed["ok"] is True, closed
    assert closed["evaluation"]["pairs"] == N_MIN
    assert closed["evaluation"]["rows"]["dropped"]["other_seats"] == 2 * N_MIN
    win = next(m for m in closed["record"]["results"]["measurements"] if m["metric"] == "win_rate")
    assert abs(win["baseline"] - 0.40) < 1e-9 and abs(win["candidate"] - 0.55) < 1e-9
    assert closed["record"]["status"] == "confirmed"


def test_computed_close_leaves_out_runs_marked_invalid(add_run, call, running):
    run_ids = record_runs(
        add_run,
        call,
        {
            "baseline": [row("V-1", s, 0.50) for s in range(N_MIN)],
            "candidate": [row("V-2", s, 0.60) for s in range(N_MIN)],
        },
    )
    extra = record_runs(add_run, call, {"candidate": [row("V-2", N_MIN, 0.0)]})
    update = {"action": "update_run", "run": extra[0], "validity": "invalid: wrong seat"}
    assert call("experiment", update, role="researcher")["ok"] is True
    closed = call("experiment", {"action": "close", "experiment": "X-1"})
    assert closed["ok"] is True, closed
    assert closed["evaluation"]["invalid_runs"] == extra and extra[0] not in run_ids
    assert closed["evaluation"]["rows"]["kept"] == 2 * N_MIN


def test_computed_close_with_only_invalid_runs_is_refused(add_run, call, running):
    [only] = record_runs(add_run, call, {"baseline": [row("V-1", 1, 0.5)]})
    update = {"action": "update_run", "run": only, "validity": "invalid: wrong seat"}
    call("experiment", update, role="researcher")
    refused = call("experiment", {"action": "close", "experiment": "X-1"})
    assert refused["ok"] is False and refused["context"]["invalid_runs"] == [only]


def test_computed_close_waits_until_every_runs_rows_are_final(add_run, call, running):
    """A close is final: read early, it would miss rows on their way and say n_short for good."""
    run_id = add_run(kind="xp", platform_request_id="xreq_1", arm="baseline")["id"]
    close = {"action": "close", "experiment": "X-1"}

    def update(**fields):
        update_run = {"action": "update_run", "run": run_id, **fields}
        assert call("experiment", update_run, role="researcher")["ok"] is True

    for fields, reason in (
        ({}, "request not started"),
        ({"state": "running"}, "request running"),
        ({"state": "completed"}, "finished but not evaluated; xp.status stores its rows"),
        ({"validity": "invalid: evaluate failed (exit 1)"}, "evaluate failed"),
    ):
        if fields:
            update(**fields)
        refused = call("experiment", close)
        assert refused["ok"] is False and "rows are not final" in refused["error"]
        assert refused["context"]["unsettled_runs"][str(run_id)].startswith(reason)
    update(validity="invalid: wrong roster")
    refused = call("experiment", close)
    assert refused["problems"] == [
        "no evaluation rows recorded yet; run xp.status first or close with an explicit verdict"
    ]
    assert call("experiment", {"action": "read", "experiment": 1})["record"]["status"] == "running"


def test_computed_close_refuses_when_no_rows_pair(add_run, call, running):
    """Rows that name a policy outside the design never pair; closing would waste the record."""
    record_runs(
        add_run,
        call,
        {"baseline": [row("V-1", s, 0.5) for s in range(3)]},
    )
    stray = add_run(kind="xp", platform_request_id="xreq_stray", arm="candidate", seed=0)
    call(
        "experiment",
        {
            "action": "update_run",
            "run": stray["id"],
            "state": "completed",
            "results": {"rows": [row("pv-elsewhere", 0, 0.9)]},
        },
        role="researcher",
    )
    refused = call("experiment", {"action": "close", "experiment": "X-1"})
    assert refused["ok"] is False
    assert "no baseline/candidate pair" in refused["error"]
    assert refused["context"]["rows_by_policy"] == {"V-1": 3, "pv-elsewhere": 1}


def test_computed_close_drops_failed_rows_and_reports_it(add_run, call, running):
    seeds = range(N_MIN + 1)
    baseline = [row("V-1", s, 0.50) for s in seeds]
    candidate = [row("V-2", s, 0.60 + (0.01 if s % 2 else -0.01)) for s in seeds]
    candidate[-1] = row("V-2", N_MIN, 0.0, status="failed", reason="crashed")
    candidate[-1]["metrics"] = {}
    record_runs(add_run, call, {"baseline": baseline, "candidate": candidate})
    closed = call("experiment", {"action": "close", "experiment": "X-1"})
    assert closed["ok"] is True, closed
    assert closed["evaluation"]["pairs"] == N_MIN
    assert closed["evaluation"]["rows"]["dropped"]["failed"] == 1


def test_malformed_stored_rows_are_named(add_run, call, running):
    added = add_run(kind="xp", platform_request_id="xreq_1", arm="baseline")
    call(
        "experiment",
        {
            "action": "update_run",
            "run": added["id"],
            "state": "completed",
            "results": {"rows": [{"episode_id": "e1", "seed": "seven"}, "not a row"]},
        },
        role="researcher",
    )
    refused = call("experiment", {"action": "close", "experiment": "X-1"})
    assert refused["ok"] is False
    problems = refused["problems"]
    assert any("run 1 results.rows[0]" in p and "seed" in p for p in problems)
    assert any("run 1 results.rows[1] must be an object" in p for p in problems)


def test_computed_close_takes_the_version_built_for_it_as_candidate(
    add_run, call, ctx_factory, two_versions
):
    """methods.candidate may stay empty at start; the Version built for X-1 is the candidate."""
    created = call("experiment", create_args(methods=methods(candidate=None)), role="researcher")
    assert created["ok"] is True, created
    started = call("experiment", {"action": "start", "experiment": 1}, role="researcher")
    assert started["record"]["status"] == "running", started
    ctx = ctx_factory()
    built = versions.create(
        ctx.store,
        ctx.actor,
        summary="the retreat change",
        commit_sha="c" * 40,
        branch="task/1",
        artifact=ARTIFACT,
        based_on=["V-1"],
        experiment_id=1,
    )
    assert built["id"] == "V-3"
    seeds = range(N_MIN)
    record_runs(
        add_run,
        call,
        {
            "baseline": [row("V-1", s, 0.50) for s in seeds],
            "candidate": [row("V-3", s, 0.60 + (0.01 if s % 2 else -0.01)) for s in seeds],
        },
    )
    closed = call("experiment", {"action": "close", "experiment": "X-1"})
    assert closed["ok"] is True, closed
    assert closed["record"]["status"] == "confirmed"
    assert closed["record"]["versions_tested"] == [
        {"id": "V-1", "role": "baseline"},
        {"id": "V-3", "role": "built"},
    ]


def test_computed_close_without_any_candidate_is_refused(call, two_versions):
    call("experiment", create_args(methods=methods(candidate=None)))
    call("experiment", {"action": "start", "experiment": 1})
    refused = call("experiment", {"action": "close", "experiment": "X-1"})
    assert refused["ok"] is False
    assert (
        "X-1 has no candidate" in refused["error"] and "0 Versions were built" in refused["error"]
    )


# --- the explicit verdict --------------------------------------------------------------------


def test_explicit_close_records_the_reason(add_run, call, running):
    record_runs(add_run, call, {"baseline": [row("V-1", 0, 0.5)]})
    closed = call(
        "experiment",
        {
            "action": "close",
            "experiment": "X-1",
            "verdict": "refuted",
            "measurements": MEASUREMENTS,
            "interpretation": "The margin is within noise and the guardrail slipped.",
            "reason": "judged by hand: the platform lost half the candidate episodes",
        },
    )
    assert closed["ok"] is True, closed
    assert closed["record"]["status"] == "refuted"
    results = closed["record"]["results"]
    assert results["reason_codes"] == [
        "explicit:judged by hand: the platform lost half the candidate episodes"
    ]
    assert results["measurements"] == MEASUREMENTS
    assert "evaluation" not in closed


def test_explicit_close_with_reason_codes_keeps_them(add_run, call, running):
    record_runs(add_run, call, {"baseline": [row("V-1", 0, 0.5)]})
    closed = call(
        "experiment",
        {
            "action": "close",
            "experiment": "X-1",
            "verdict": "inconclusive",
            "measurements": MEASUREMENTS,
            "interpretation": "Too few valid episodes.",
            "reason_codes": ["n_short:4<40"],
        },
    )
    assert closed["ok"] is True, closed
    assert closed["record"]["results"]["reason_codes"] == ["n_short:4<40"]


def test_explicit_close_names_every_missing_part(call, running):
    refused = call("experiment", {"action": "close", "experiment": "X-1", "verdict": "confirmed"})
    assert refused["ok"] is False
    assert set(refused["problems"]) == {
        "measurements are required with an explicit verdict",
        "interpretation is required with an explicit verdict",
        "reason or reason_codes is required with an explicit verdict",
    }
    bad = call(
        "experiment",
        {
            "action": "close",
            "experiment": "X-1",
            "verdict": "maybe",
            "measurements": MEASUREMENTS,
            "interpretation": "x",
            "reason": "y",
        },
    )
    assert bad["ok"] is False and "verdict must be one of" in bad["error"]


def test_close_a_proposed_experiment_is_refused(call, two_versions):
    call("experiment", create_args())
    refused = call("experiment", {"action": "close", "experiment": "X-1"})
    assert refused["ok"] is False
    assert "X-1 is proposed; an Experiment closes only while it is running" in refused["error"]


def test_results_never_show_a_store_path(call, running):
    text = str(call("experiment", {"action": "read", "experiment": "X-1"}))
    assert ".state/" not in text and "store.db" not in text


def test_update_run_takes_the_runs_own_experiment_and_refuses_another(add_run, call, running):
    run = add_run(kind="xp", platform_request_id="xreq_1", arm="baseline")["id"]
    args = {"action": "update_run", "run": run, "validity": "invalid: wrong seat"}
    assert call("experiment", {**args, "experiment": "X-1"})["ok"] is True
    refused = call("experiment", {**args, "experiment": "X-2"})
    assert refused["ok"] is False and "belongs to X-1" in refused["error"]
