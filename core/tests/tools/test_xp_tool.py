"""The ``xp`` tool on the fake platform: requests as intents, credits, the ask-above setting,
status with download, evaluate and stored rows, a broken evaluate, a backgrounded evaluate."""

from __future__ import annotations

import json
import time

import pytest

import research_core.tools.xp as xp_tool
from research_core.platform import fake
from research_core.platform import xp as xp_platform
from research_core.platform.errors import PlatformError
from research_core.store import (
    capabilities,
    experiments,
    goals,
    intents,
    sessions,
    settings,
    tasks,
    versions,
)
from research_core.store.actors import Actor
from research_core.tools.base import ToolContext, run_tool
from research_core.watcher import waits
from tests.fixtures.fakegame_repo import fixture_files

N_MIN = 2
# The fake game seats two; tests about something else than the roster pin the other seat.
ROSTER = {"opponents": ["pv_other"]}
HYPOTHESIS = {
    "statement": "More patience wins more",
    "if_true": "score rises",
    "if_false": "score stays within noise",
    "motivation": "patience is untested",
}
METRICS = [
    {"name": "score", "direction": "up", "role": "primary", "min_effect": 1.0},
    {"name": "errors", "direction": "down", "role": "guardrail", "tolerance": 1.0},
]
# The fixture's evaluate.py, which prints every seat of every downloaded episode.
EVALUATE_COMMAND = 'python3 mixin/tools/evaluate.py "$XP_RESULTS"'


def policy_bytes(aggression: int, patience: int) -> bytes:
    return json.dumps({"parameters": {"aggression": aggression, "patience": patience}}).encode()


@pytest.fixture
def run(ctx_factory, workspace_env):
    """``run(tool, args, role=, session_id=, faults=)``: a tool call with fake-platform faults."""
    ctx_factory()  # registers and activates cow_fake

    def call(tool, args, *, role="orchestrator", session_id="s1", faults=""):
        env = dict(workspace_env, IDE_SESSION_ID=session_id, IDE_FAKE_FAULTS=faults)
        ctx = ToolContext.from_env(role, env)
        ctx.coordinator_instance = "coordinator:unit"
        return json.loads(run_tool(tool, args, ctx))

    return call


@pytest.fixture
def ctx(ctx_factory):
    return ctx_factory()


@pytest.fixture
def mixin_dir(ctx):
    """The fakegame mixin at ``<coworld>/code/mixin`` with the test's evaluate command."""
    code = ctx.coworld().code
    for relative, content in fixture_files(fake.COWORLD_ID).items():
        path = code / "mixin" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    set_evaluate_command(code / "mixin", EVALUATE_COMMAND)
    return code / "mixin"


def set_evaluate_command(mixin_dir, command: str) -> None:
    text = (mixin_dir / "MIXIN.md").read_text()
    head, sep, tail = text.partition("\n---\n")
    lines = [
        f"  evaluate: {json.dumps(command)}" if line.startswith("  evaluate:") else line
        for line in head.splitlines()
    ]
    (mixin_dir / "MIXIN.md").write_text("\n".join(lines) + sep + tail)


@pytest.fixture
def uploaded(ctx):
    """V-1 (baseline) and V-2 (candidate), built and uploaded through the fake platform."""
    platform = ctx.platform()
    for n, patience in ((1, 5), (2, 8)):
        versions.create(
            ctx.store,
            ctx.actor,
            summary=f"policy {n}",
            commit_sha=f"{n:040x}",
            branch="main",
            artifact={"kind": "file", "path": "build/policy.json", "digest": "d" * 64, "size": 1},
            based_on=[],
        )
        pv = platform.upload_file_policy(name=f"policy-{n}", content=policy_bytes(3, patience))
        versions.mark_uploaded(
            ctx.store, ctx.actor, n, policy_version_id=pv.id, at=pv.created_at, digest_checked=True
        )


def make_experiment(ctx, *, start=True, task_id=None, n_min=N_MIN) -> dict:
    record = experiments.create(
        ctx.store,
        ctx.actor,
        summary="Does patience raise the score?",
        task_id=task_id,
        hypothesis=HYPOTHESIS,
        methods={
            "baseline": "V-1",
            "candidate": "V-2",
            "procedure": "one episode per seed per arm",
            "metrics": METRICS,
            "decision_rule": {"text": "score up by 1 with the CI above zero", "n_min": n_min},
        },
    )
    if start:
        record = experiments.start(ctx.store, ctx.actor, record["id"])
    return record


@pytest.fixture
def running(ctx, uploaded):
    return make_experiment(ctx)


def xp_runs(ctx, n=1):
    return [r for r in experiments.read(ctx.store, n)["runs"] if r["kind"] == "xp"]


def wait_for_rows(run, token, *, timeout=20):
    deadline = time.time() + timeout
    while True:
        result = run("xp", {"action": "status", "resume": token})
        if "background" not in result:
            return result
        assert time.time() < deadline, "evaluate did not finish in time"
        time.sleep(0.1)


# --- request ---------------------------------------------------------------------------------


def test_request_creates_one_request_per_seed_and_arm(run, ctx, running):
    result = run("xp", {"action": "request", "experiment": "X-1", **ROSTER})
    assert result["ok"] is True, result
    assert len(result["created"]) == 4 and result["reconciled"] == []
    runs = result["runs"]
    assert [(r["seed"], r["arm"]) for r in runs] == [
        (1, "candidate"),
        (1, "baseline"),
        (2, "candidate"),
        (2, "baseline"),
    ]
    assert all(r["state"] == "pending" and r["episodes_total"] == 1 for r in runs)
    assert runs[0]["results"]["version"] == "V-2" and runs[1]["results"]["version"] == "V-1"
    assert runs[0]["results"]["title"] == "ide:X-1:1:candidate"
    titles = sorted(r.title for r in ctx.platform().xp_list())
    assert titles == sorted(f"ide:X-1:{seed}:{arm}" for seed in (1, 2) for arm in xp_tool.ARMS)
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 4
    assert result["credits"].startswith("credits: 96 available of 100")
    assert result["estimate"] == {"episodes": 4, "credits": 4.0}
    assert result["warnings"] == []
    assert "xp.status" in result["next"]
    assert [i["state"] for i in (intents.read(ctx.store, n) for n in range(1, 5))] == ["done"] * 4

    again = run("xp", {"action": "request", "experiment": "X-1", **ROSTER})
    assert again["ok"] is True and again["created"] == [] and len(again["runs"]) == 4
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 4


def test_request_body_and_options(run, ctx, running):
    result = run(
        "xp",
        {
            "action": "request",
            "experiment": 1,
            "seeds": [7],
            "episodes": 3,
            "arms": ["candidate"],
            "opponents": ["pv_other"],
        },
    )
    assert result["ok"] is True, result
    (row,) = ctx.platform().snapshot()["xp"].values()
    assert row["body"] == {
        "idempotency_key": intents.read(ctx.store, 1)["request"]["idempotency_key"],
        "coworld_id": fake.COWORLD_ID,
        "roster": [
            {"player": {"policy_ref": row["policy"]}, "slot": 0},
            {"player": {"policy_ref": "pv_other"}, "slot": 1},
        ],
        "num_episodes": 3,
        "title": "ide:X-1:7:candidate",
        "game_config_overrides": {"seed": 7},
    }
    assert result["runs"][0]["episodes_total"] == 3
    assert any("below the decision rule's n_min" in w for w in result["warnings"])


def test_a_league_fills_the_open_seats_and_picks_the_coworld(run, ctx, running):
    args = {"action": "request", "experiment": "X-1", "seeds": [7], "arms": ["candidate"]}
    result = run("xp", {**args, "league": fake.LEAGUE_ID})
    assert result["ok"] is True, result
    (row,) = ctx.platform().snapshot()["xp"].values()
    assert "coworld_id" not in row["body"]
    assert row["body"]["target"] == {"league_id": fake.LEAGUE_ID}
    assert row["body"]["roster"] == [
        {"player": {"policy_ref": row["policy"]}, "slot": 0},
        {"player": {"random": True}, "slot": -1},
    ]


@pytest.mark.parametrize(
    "roster",
    [
        {},
        {"opponents": []},
        {"opponents": ["a", "b"]},
        {"league": fake.LEAGUE_ID, "opponents": ["a", "b"]},
    ],
)
def test_a_roster_the_game_cannot_seat_is_refused_before_any_request(run, ctx, running, roster):
    result = run("xp", {"action": "request", "experiment": "X-1", "seeds": [1], **roster})
    assert result["ok"] is False
    assert "this game seats 2" in result["error"]
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 0


@pytest.mark.parametrize(
    ("low", "high", "exact", "args", "refused"),
    [
        (2, 8, None, {"league": fake.LEAGUE_ID}, True),  # how many seats to fill is unknowable
        (2, 8, None, {"opponents": ["a"]}, False),
        (1, 8, None, {}, True),  # a lone seat in a game of up to 8 is a mistake, not a choice
        (1, 8, None, {"opponents": []}, False),
        (1, 1, 1, {}, False),  # a solo game
    ],
)
def test_a_game_of_several_sizes_is_never_guessed(
    run, ctx, running, monkeypatch, low, high, exact, args, refused
):
    seats = xp_platform.Seats(exact, low, high)
    monkeypatch.setattr(xp_platform, "seats_for", lambda *_: seats)
    monkeypatch.setattr(fake, "_refuse_create", lambda _body: None)
    request = {"action": "request", "experiment": "X-1", "seeds": [1], "arms": ["candidate"]}
    result = run("xp", {**request, **args})
    assert result["ok"] is not refused, result


def test_seats_come_from_the_leagues_variant():
    manifest = {
        "game": {
            "config_schema": {
                "required": ["tokens"],
                "properties": {
                    "tokens": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 5,
                    }
                },
            }
        },
        "variants": [
            {"id": "solo", "game_config": {"num_agents": 1}},
            {"id": "five", "game_config": {"num_agents": 5}},
            {"id": "open", "game_config": {}},
        ],
    }
    assert xp_platform.seats_for(manifest).exact == 1
    assert xp_platform.seats_for(manifest, "five") == xp_platform.Seats(5, 1, 5)
    assert xp_platform.seats_for(manifest, "open").exact is None


def test_a_refused_body_is_not_replayed_on_the_next_call(run, ctx, running, monkeypatch):
    """A 4xx created nothing; the corrected call must send its own body, not the saved one."""
    args = {"action": "request", "experiment": "X-1", "seeds": [1], "arms": ["candidate"]}
    create = fake.FakePlatform.xp_create
    sent: list[dict] = []

    def refuse_first(self, body):
        sent.append(body)
        if len(sent) == 1:
            raise PlatformError("game_config is invalid", route="POST", status=400)
        return create(self, body)

    monkeypatch.setattr(fake.FakePlatform, "xp_create", refuse_first)
    first = run("xp", {**args, "opponents": ["pv_wrong"]})
    assert first["ok"] is False and intents.read(ctx.store, 1)["state"] == "failed"
    second = run("xp", {**args, "opponents": ["pv_right"]})
    assert second["ok"] is True, second
    assert sent[1]["roster"][1]["player"]["policy_ref"] == "pv_right"
    assert sent[1]["idempotency_key"] != sent[0]["idempotency_key"]
    assert intents.read(ctx.store, 1)["request"] == sent[1]


def test_request_refusals_name_every_problem(run, ctx, uploaded):
    proposed = make_experiment(ctx, start=False)
    result = run(
        "xp", {"action": "request", "experiment": proposed["id"], **ROSTER, "seeds": [1, 1]}
    )
    assert result["ok"] is False
    assert "X-1 is proposed; start the experiment first (experiment.start)" in result["problems"]
    assert "seeds must not repeat" in result["problems"]

    other = run("xp", {"action": "request", "experiment": "X-1", **ROSTER, "candidate": "V-1"})
    assert (
        "candidate V-1 differs from X-1's locked methods.candidate V-2; abandon X-1 and create a"
        " new Experiment to test another Version"
    ) in other["problems"]

    versions.create(
        ctx.store,
        ctx.actor,
        summary="not uploaded",
        commit_sha="3" * 40,
        branch="main",
        artifact={"kind": "file", "path": "build/policy.json", "digest": "e" * 64, "size": 1},
        based_on=[],
    )
    record = experiments.create(
        ctx.store,
        ctx.actor,
        summary="Second",
        hypothesis=HYPOTHESIS,
        methods={**proposed["methods"], "candidate": "V-3"},
    )
    experiments.start(ctx.store, ctx.actor, record["id"])
    result = run("xp", {"action": "request", "experiment": record["id"], **ROSTER})
    assert result["ok"] is False
    assert "V-3 (candidate) is not uploaded; run upload {version: V-3} first" in result["problems"]
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 0


def test_request_is_idempotent_after_a_crash_between_create_and_record(run, ctx, running):
    crashed = run(
        "xp", {"action": "request", "experiment": "X-1", **ROSTER}, faults="fail_after=xp_create"
    )
    assert crashed["ok"] is False and "injected fault after xp_create" in crashed["error"]
    platform = ctx.platform()
    assert platform.snapshot()["counts"]["xp_create"] == 1
    assert xp_runs(ctx) == []
    (pending,) = intents.pending(ctx.store, "xp")
    assert pending["key"] == "X-1:1:candidate"

    result = run("xp", {"action": "request", "experiment": "X-1", **ROSTER})
    assert result["ok"] is True, result
    assert platform.snapshot()["counts"]["xp_create"] == 4
    assert len(result["reconciled"]) == 1 and len(result["created"]) == 3
    assert [r.title for r in platform.xp_list() if r.id == result["reconciled"][0]] == [
        "ide:X-1:1:candidate"
    ]
    assert len(xp_runs(ctx)) == 4 and intents.pending(ctx.store, "xp") == []


@pytest.fixture
def researcher_task(ctx):
    """A Researcher task Session ``r1`` holding T-1, and X-1 as that Task's Experiment."""
    goal = goals.create(ctx.store, ctx.actor, title="g", end_state="e", reason="r")
    task = tasks.create(
        ctx.store, ctx.actor, type="improvement", title="t", description="d", goal_id=goal["id"]
    )
    holder = Actor.session("r1")
    tasks.claim(ctx.store, holder, task["id"], run_id="run-1", worktree="/w/task-1", branch="t/1")
    sessions.register(
        ctx.wstore,
        holder,
        coworld_id="cow_fake",
        session_id="r1",
        profile="researcher",
        kind="task",
        task_id=1,
    )
    return task


def test_credits_refusal_names_a_need_the_wait_can_read(
    run, ctx, uploaded, researcher_task, monkeypatch
):
    monkeypatch.setattr(xp_tool.credits_module, "estimate", lambda *_, **__: 2.5)
    make_experiment(ctx, task_id=1)
    result = run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER},
        role="researcher",
        session_id="r1",
        faults="credits_refuse=1",
    )
    detail = result["next"].split("detail: '")[1].split("'")[0]
    assert waits.NEED.search(detail)[1] == "2.50"
    assert "resumes you when the balance covers it" in result["next"]


def test_credits_refusal_reports_the_balance_and_sets_no_wait(run, ctx, uploaded, researcher_task):
    make_experiment(ctx, task_id=1)
    result = run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER},
        role="researcher",
        session_id="r1",
        faults="credits_refuse=1",
    )
    assert result["ok"] is False
    assert result["credits_refused"]["balance"] == 100.0
    assert result["credits_refused"]["held"] == 0.0
    assert result["credits_refused"]["next_refill_at"]
    assert "task_wait {task, kind: credits" in result["next"]
    assert "resumes you when the balance is above zero" in result["next"]  # no estimate here
    assert result["created"] == [] and result["runs"] == []
    assert tasks.read(ctx.store, 1)["waiting_on"] is None
    intent = intents.read(ctx.store, 1)
    assert intent["state"] == "failed" and intent["result"]["status"] == 402
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 0

    retried = run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER},
        role="researcher",
        session_id="r1",
    )
    assert retried["ok"] is True, retried
    assert (
        intents.read(ctx.store, 1)["state"] == "done" and intents.read(ctx.store, 1)["attempt"] == 2
    )
    assert "task_wait {task, kind: xp," in retried["next"]
    assert tasks.read(ctx.store, 1)["waiting_on"] is None


def test_a_tasks_experiment_is_refused_for_anyone_but_its_holder_before_any_request(
    run, ctx, uploaded, researcher_task
):
    make_experiment(ctx, task_id=researcher_task["id"])
    refused = run("xp", {"action": "request", "experiment": "X-1", **ROSTER})
    assert refused["ok"] is False and "belongs to T-1" in str(refused["problems"])
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 0
    held = run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER},
        role="researcher",
        session_id="r1",
    )
    assert held["ok"] is True, held
    # The wait takes the Task's open requests by itself, so the model copies no ids.
    assert held["next"].startswith("task_wait {task, kind: xp, detail: 'XP requests for X-1'}")


def test_ask_above_warns_the_orchestrator_and_asks_the_researcher(run, ctx, uploaded):
    make_experiment(ctx)
    settings.update(ctx.store, ctx.actor, xp_tool.ASK_ABOVE_KEY, 2, cites="the user said so")
    researcher = run("xp", {"action": "request", "experiment": "X-1", **ROSTER}, role="researcher")
    assert researcher["ok"] is False and researcher["ask_orchestrator"] is True
    assert researcher["counts"] == {"existing": 0, "new": 4, "total": 4, "limit": 2}
    assert researcher["suggest"].startswith("task_ask {task: <your Task>, question: ")
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 0

    orchestrator = run("xp", {"action": "request", "experiment": "X-1", **ROSTER})
    assert orchestrator["ok"] is True, orchestrator
    assert len(orchestrator["created"]) == 4
    (warning,) = orchestrator["warnings"]
    assert "4 XP requests, above the xp.ask_above_requests_per_experiment setting of 2" in warning


def test_over_the_limit_a_task_experiment_takes_requests_after_the_user_raises_the_setting(
    run, ctx, uploaded, researcher_task
):
    """The Researcher asks, the Orchestrator cannot send for it, the user's word raises it."""
    make_experiment(ctx, task_id=researcher_task["id"])
    settings.update(ctx.store, ctx.actor, xp_tool.ASK_ABOVE_KEY, 2, cites="the user said so")
    held = {"role": "researcher", "session_id": "r1"}
    asked = run("xp", {"action": "request", "experiment": "X-1", **ROSTER}, **held)
    assert asked["ask_orchestrator"] is True and "raise the setting" in asked["suggest"]

    orchestrator = run("xp", {"action": "request", "experiment": "X-1", **ROSTER})
    assert orchestrator["ok"] is False and "belongs to T-1" in str(orchestrator["problems"])
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 0

    raised = run(
        "settings_update", {"key": xp_tool.ASK_ABOVE_KEY, "value": 4, "cites": "message m-1"}
    )
    assert raised["ok"] is True, raised
    sent = run("xp", {"action": "request", "experiment": "X-1", **ROSTER}, **held)
    assert sent["ok"] is True and len(sent["created"]) == 4
    assert len(sent["runs"]) == 4


def test_tool_spec(run):
    spec = xp_tool.SPEC
    assert spec.executor == "workspace" and spec.roles == {"orchestrator", "researcher"}
    assert [action.name for action in spec.actions] == ["request", "status"]
    assert {action.name for action in spec.actions_for("researcher")} == {"request", "status"}
    missing = run("xp", {"action": "status"})
    assert missing["ok"] is False and "needs an experiment" in missing["error"]


# --- status ----------------------------------------------------------------------------------


def test_status_completes_downloads_evaluates_and_stores_rows(run, ctx, running, mixin_dir):
    ctx.platform().configure(polls_to_complete=2)
    requested = run("xp", {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1]})
    assert requested["ok"] is True, requested
    first = run("xp", {"action": "status", "experiment": "X-1"})
    assert first["ok"] is True and first["complete"] is False
    assert len(first["pending"]) == 2
    assert all(r["state"] == "running" for r in first["runs"])

    second = run("xp", {"action": "status", "experiment": "X-1"})
    assert second["ok"] is True, second
    assert second["complete"] is True and second["pending"] == []
    assert "experiment.close" in second["next"]
    by_arm = {r["arm"]: r for r in second["runs"]}
    for arm, version in (("candidate", "V-2"), ("baseline", "V-1")):
        run_record = by_arm[arm]
        assert run_record["state"] == "completed" and run_record["validity"] == "valid"
        assert run_record["episodes_completed"] == 1
        request_id = run_record["platform_request_id"]
        rows = run_record["results"]["rows"]
        assert len(rows) == 1
        assert rows[0] == {
            "episode_id": f"{request_id}:0:s0",
            "game_version": "1",
            "policy_version": version,
            "seed": 1,
            "seats": [0],
            "opponents": [],
            "metrics": {
                "score": rows[0]["metrics"]["score"],
                "errors": rows[0]["metrics"]["errors"],
            },
            "status": "completed",
            "reason": None,
        }
        assert run_record["results"]["evaluate"]["exit_code"] == 0
        dest = ctx.coworld().documents / "xp" / "X-1" / request_id
        assert (dest / "results.json").is_file()
    assert (
        by_arm["candidate"]["results"]["rows"][0]["metrics"]
        != by_arm["baseline"]["results"]["rows"][0]["metrics"]
    )
    # Stored rows rebuild as EpisodeRow through the experiment tool's contract.
    from research_core.tools.experiment import row_from_dict

    assert row_from_dict(by_arm["candidate"]["results"]["rows"][0]).policy_version == "V-2"

    third = run("xp", {"action": "status", "request": by_arm["baseline"]["platform_request_id"]})
    assert third["complete"] is True and len(third["runs"]) == 2


def test_status_keeps_only_our_seats_rows(run, ctx, running, mixin_dir, tmp_path):
    # An evaluate that prints every seat: seat 1 is the opponent and must not count as ours.
    script = tmp_path / "all_seats.py"
    script.write_text(
        "import json\n"
        "for seat, won in ((0, 1.0), (1, 0.0)):\n"
        "    print(json.dumps({'episode': 0, 'seed': 1, 'seat': seat,"
        " 'metrics': {'score': won, 'errors': 0}}))\n"
    )
    set_evaluate_command(mixin_dir, f"python3 {script}")
    run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]},
    )
    result = run("xp", {"action": "status", "experiment": "X-1"})
    assert result["ok"] is True, result
    (run_record,) = result["runs"]
    rows = run_record["results"]["rows"]
    assert [row["seats"] for row in rows] == [[0]] and rows[0]["metrics"]["score"] == 1.0
    assert run_record["results"]["other_seats"]["rows"] == 1


def test_only_our_seat_counts_and_the_opponents_seats_are_kept_apart(run, ctx, running, mixin_dir):
    """The fake scores both seats and the fixture's evaluate prints both. Seat 1 is pv_other's:
    counted as ours it would pull both arms toward the opponent (a false tie)."""
    requested = run("xp", {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1]})
    assert requested["runs"][0]["results"]["seat"] == 0
    assert requested["runs"][0]["results"]["roster"]["1"] == "pv_other"
    result = run("xp", {"action": "status", "experiment": "X-1"})
    for run_record in result["runs"]:
        results = run_record["results"]
        assert [row["seats"] for row in results["rows"]] == [[0]]
        assert results["other_seats"]["rows"] == 1
        with open(results["other_seats"]["file"], encoding="utf-8") as stream:
            (other,) = [json.loads(line) for line in stream]
        assert other["seats"] == [1] and other["policy_version"] == "pv_other"
        assert other["episode_id"] == f"{run_record['platform_request_id']}:0:s1"
    closed = run("experiment", {"action": "close", "experiment": "X-1"})
    assert closed["ok"] is True, closed
    assert closed["evaluation"]["rows"]["dropped"]["other_seats"] == 0
    errors = next(m for m in closed["record"]["results"]["measurements"] if m["metric"] == "errors")
    by_arm = {r["arm"]: r["results"]["rows"][0]["metrics"]["errors"] for r in result["runs"]}
    assert (errors["baseline"], errors["candidate"]) == (by_arm["baseline"], by_arm["candidate"])


def test_rows_carry_the_game_version_the_platform_played(run, ctx, running, mixin_dir):
    """MIXIN.md says what the mixin was written for; after a Coworld release it is stale."""
    manifest = mixin_dir / "MIXIN.md"
    stale = manifest.read_text().replace('coworld_version: "1"', 'coworld_version: "0"')
    assert 'coworld_version: "0"' in stale
    manifest.write_text(stale)
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    (created,) = run("xp", args)["runs"]
    assert created["results"]["coworld_version"] == fake.COWORLD_VERSION
    (run_record,) = run("xp", {"action": "status", "experiment": "X-1"})["runs"]
    assert run_record["results"]["rows"][0]["game_version"] == fake.COWORLD_VERSION


def test_a_league_sampled_seat_is_labelled_as_such(run, ctx, running, mixin_dir):
    args = {"action": "request", "experiment": "X-1", "seeds": [7], "arms": ["candidate"]}
    run("xp", {**args, "league": fake.LEAGUE_ID})
    (run_record,) = run("xp", {"action": "status", "experiment": "X-1"})["runs"]
    assert run_record["results"]["roster"] == {"0": run_record["results"]["policy_version_id"]}
    with open(run_record["results"]["other_seats"]["file"], encoding="utf-8") as stream:
        (other,) = [json.loads(line) for line in stream]
    assert other["policy_version"] == xp_tool.LEAGUE_SAMPLED


def test_evaluate_without_a_row_for_our_seat_is_broken(run, ctx, running, mixin_dir):
    """Rows of the other seats alone are never counted as ours."""
    set_evaluate_command(mixin_dir, f'{EVALUATE_COMMAND} | grep -v "\\"seat\\": 0"')
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    run("xp", args)
    (run_record,) = run("xp", {"action": "status", "experiment": "X-1"})["runs"]
    assert run_record["validity"].startswith("invalid: evaluate output: no row for seat 0")
    assert "rows" not in run_record["results"]
    assert capabilities.read(ctx.store, "evaluate")["status"] == "broken"


def test_evaluate_is_told_our_seat_and_policy_version(run, ctx, running, mixin_dir):
    told = 'echo "seat=$XP_SEAT pv=$XP_POLICY_VERSION_ID" > "$XP_RESULTS/told.txt"'
    set_evaluate_command(mixin_dir, f"{told}; {EVALUATE_COMMAND}")
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    (created,) = run("xp", args)["runs"]
    run("xp", {"action": "status", "experiment": "X-1"})
    dest = ctx.coworld().documents / "xp" / "X-1" / created["platform_request_id"]
    told_text = (dest / "told.txt").read_text()
    assert told_text == f"seat=0 pv={created['results']['policy_version_id']}\n"


def test_status_marks_a_broken_evaluate_invalid_and_reports_the_capability(
    run, ctx, running, mixin_dir
):
    set_evaluate_command(mixin_dir, "false")
    run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]},
    )
    result = run("xp", {"action": "status", "experiment": "X-1"})
    assert result["ok"] is True, result
    (run_record,) = result["runs"]
    assert run_record["validity"] == "invalid: evaluate failed (exit 1)"
    assert "rows" not in run_record["results"]
    assert result["complete"] is False
    broken = capabilities.read(ctx.store, "evaluate")
    assert broken["status"] == "broken" and broken["command"] == "false"
    assert tasks.read(ctx.store, broken["maintenance_task_id"])["capability"] == "evaluate"

    set_evaluate_command(mixin_dir, "echo not-json")
    run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [2], "arms": ["candidate"]},
    )
    result = run("xp", {"action": "status", "experiment": "X-1"})
    second = [r for r in result["runs"] if r["seed"] == 2][0]
    assert second["validity"].startswith("invalid: evaluate output: no row for seat 0")

    set_evaluate_command(mixin_dir, "echo '{\"episode\": 0,'")
    run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [3], "arms": ["candidate"]},
    )
    result = run("xp", {"action": "status", "experiment": "X-1"})
    third = [r for r in result["runs"] if r["seed"] == 3][0]
    assert third["validity"].startswith("invalid: evaluate output: evaluate output line 1")


def test_log_lines_on_stderr_do_not_break_evaluate(run, ctx, running, mixin_dir):
    """stderr shares the log; a uv or warning line there is not a broken evaluate."""
    set_evaluate_command(mixin_dir, f'echo "Installed 3 packages" >&2; {EVALUATE_COMMAND}')
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    run("xp", args)
    (run_record,) = run("xp", {"action": "status", "experiment": "X-1"})["runs"]
    assert run_record["validity"] == "valid" and len(run_record["results"]["rows"]) == 1


def test_status_backgrounds_a_slow_evaluate_and_resumes(run, ctx, running, mixin_dir, monkeypatch):
    monkeypatch.setattr(xp_tool, "EVALUATE_BUDGET_S", 0.2)
    set_evaluate_command(mixin_dir, f"sleep 1; {EVALUATE_COMMAND}")
    run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["baseline"]},
    )
    started = run("xp", {"action": "status", "experiment": "X-1"})
    assert started["ok"] is True and started["background"]["pid"] > 0
    assert "task_wait {task, kind: process" in started["next"]
    token = started["background"]["resume"]
    assert "rows" not in xp_runs(ctx)[0]["results"]

    finished = wait_for_rows(run, token)
    assert finished["ok"] is True, finished
    assert finished["complete"] is True
    (run_record,) = finished["runs"]
    assert len(run_record["results"]["rows"]) == 1 and run_record["validity"] == "valid"
    # The same token again is a no-op: the rows are already stored.
    assert run("xp", {"action": "status", "resume": token})["complete"] is True


def test_status_on_a_closed_experiment_reports_without_touching_runs(run, ctx, running):
    run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["baseline"]},
    )
    experiments.abandon(ctx.store, ctx.actor, "X-1", reason="moot")
    result = run("xp", {"action": "status", "experiment": "X-1"})
    assert result["ok"] is True and result["note"] == "X-1 is abandoned; its runs are final"
    assert result["runs"][0]["state"] == "pending"


def test_recovery_ignores_other_requests_with_same_title(run, ctx, running):
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    assert run("xp", args, faults="fail_after=xp_create")["ok"] is False
    platform = ctx.platform()
    (original,) = platform.snapshot()["xp"].values()
    foreign = platform.xp_create(
        {**original["body"], "coworld_id": "cow_other", "idempotency_key": "foreign"}
    )
    result = run("xp", args)
    assert result["ok"] is True, result
    assert result["requests"] == [original["id"]]
    assert result["requests"] != [foreign.id]
    assert platform.snapshot()["counts"]["xp_create"] == 2


def test_recovery_replays_saved_body_without_listing(run, ctx, running, monkeypatch):
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    assert run("xp", args, faults="fail_after=xp_create")["ok"] is False
    (pending,) = intents.pending(ctx.store, "xp")
    saved = pending["request"]
    assert saved["idempotency_key"]

    def no_listing(*args, **kwargs):
        pytest.fail("Recovery must not search platform request listings")

    monkeypatch.setattr(fake.FakePlatform, "xp_list", no_listing)
    result = run("xp", {**args, "episodes": 9})
    assert result["ok"] is True, result
    (row,) = ctx.platform().snapshot()["xp"].values()
    assert row["body"] == saved
    assert result["runs"][0]["episodes_total"] == 1
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 1


def test_old_pending_request_is_not_resubmitted(run, ctx, running):
    intents.begin(ctx.store, ctx.actor, kind="xp", key="X-1:1:candidate", subject="X-1")
    result = run("xp", {"action": "request", "experiment": "X-1", **ROSTER})
    assert result["ok"] is False
    assert "predates idempotent XP requests" in result["error"]
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 0


def test_distinct_experiments_get_distinct_request_keys(run, ctx, running):
    first = run("xp", {"action": "request", "experiment": "X-1", **ROSTER})
    second_experiment = make_experiment(ctx)
    second = run("xp", {"action": "request", "experiment": second_experiment["id"], **ROSTER})
    assert first["ok"] and second["ok"]
    keys = [row["body"]["idempotency_key"] for row in ctx.platform().snapshot()["xp"].values()]
    assert len(set(keys)) == 8


def test_retry_after_lost_response_creates_one_request(run, ctx, running, monkeypatch):
    from research_core.platform.errors import PlatformError

    create = fake.FakePlatform.xp_create
    calls = []

    def lose_first_response(self, body):
        result = create(self, body)
        calls.append(dict(body))
        if len(calls) == 1:
            raise PlatformError("lost response", retryable=True)
        return result

    monkeypatch.setattr(fake.FakePlatform, "xp_create", lose_first_response)
    monkeypatch.setattr(xp_tool.wait, "sleep", lambda _: None)
    result = run(
        "xp",
        {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]},
    )
    assert result["ok"] is True, result
    assert len(calls) == 2 and calls[0] == calls[1]
    assert ctx.platform().snapshot()["counts"]["xp_create"] == 1


def test_a_repaired_evaluate_makes_a_failed_run_good_again(run, ctx, running, mixin_dir):
    """A failed evaluate says nothing about the episodes: it must not settle the Experiment."""
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    set_evaluate_command(mixin_dir, "false")
    assert run("xp", args)["ok"] is True
    failed = run("xp", {"action": "status", "experiment": "X-1"})
    assert failed["complete"] is False and "evaluates them again" in failed["next"]
    set_evaluate_command(mixin_dir, EVALUATE_COMMAND)
    repaired = run("xp", {"action": "status", "experiment": "X-1"})
    assert repaired["runs"][0]["validity"] == "valid"
    assert repaired["runs"][0]["results"]["rows"]
    assert repaired["complete"] is True


def test_a_failure_the_watcher_recorded_still_makes_the_run_invalid(run, ctx, running):
    """waits._xp writes the platform state onto runs it may write. A failed state it wrote used
    to leave the run valid with no rows, so the Experiment never settled."""
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    (created,) = run("xp", args)["runs"]
    experiments.update_run(ctx.store, Actor.harness(), created["id"], state="failed")
    result = run("xp", {"action": "status", "experiment": "X-1"})
    assert result["runs"][0]["validity"] == "invalid: request failed"
    assert result["complete"] is True


def test_a_user_marked_invalid_run_stays_settled(run, ctx, running):
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    (created,) = run("xp", args)["runs"]
    experiments.update_run(ctx.store, Actor.harness(), created["id"], validity="invalid: bad seed")
    result = run("xp", {"action": "status", "experiment": "X-1"})
    assert "rows" not in result["runs"][0]["results"]


def test_a_status_call_stops_evaluating_when_its_time_runs_out(
    run, ctx, running, mixin_dir, monkeypatch
):
    args = {"action": "request", "experiment": "X-1", **ROSTER, "seeds": [1], "arms": ["candidate"]}
    assert run("xp", args)["ok"] is True
    with monkeypatch.context() as patch:
        patch.setattr(xp_tool, "STATUS_BUDGET_S", -1)
        result = run("xp", {"action": "status", "experiment": "X-1"})
    assert "time ran out" in result["note"] and result["complete"] is False
    assert "rows" not in (result["runs"][0]["results"] or {})
    assert run("xp", {"action": "status", "experiment": "X-1"})["complete"] is True
