"""The fixture world against the real tools, with no model and no key: what a usability run answers
must have the keys the real tool gives, or the run measures a world the model never meets."""

from __future__ import annotations

import dataclasses
import json

import pytest

from research_core.platform import fake
from research_core.platform.errors import CreditsRefused
from research_core.stats import decision_rule, ingest
from research_core.store import experiments, goals, sessions, tasks, versions
from research_core.store.actors import Actor
from research_core.store.errors import StoreError
from research_core.tools import catalog
from research_core.tools import experiment as experiment_tool
from research_core.tools import task as task_tool
from research_core.tools import xp as xp_tool
from research_core.tools.xp import _credits_refused
from tests.model import (
    harness,
    usability_context,
    usability_probe,
    usability_reads,
    usability_results,
    write_actions,
)
from tests.model.usability_situations import (
    BUILD_BACKGROUND,
    CREDITS_REFUSAL,
    SITUATIONS,
    XP_ACCEPTED,
)
from tests.tools.conftest import COWORLD, call, ctx_factory, workspace_env  # noqa: F401

SITUATION = next(s for s in SITUATIONS if s.id == "r-experiment-close")
ARTIFACT = {"kind": "file", "path": "build/policy.json", "digest": "d" * 64, "size": 1}
HYPOTHESIS = {
    "statement": "More patience wins more",
    "if_true": "score rises",
    "if_false": "score stays within noise",
    "motivation": "patience is untested",
}
METHODS = {
    "baseline": "V-1",
    "candidate": "V-2",
    "procedure": "one episode per seed per arm",
    "metrics": [{"name": "score", "direction": "up", "role": "primary", "min_effect": 1.0}],
    "decision_rule": {"text": "score up by 1 with the CI above zero", "n_min": 2},
}
EVIDENCE = [{"type": "replay", "ref": "episode:ereq_1 xp:none", "stance": "supports", "note": "n"}]


def _kind(value) -> str:
    """A number's kind is one: a store may hold 2 where a fixture holds 2.0."""
    if isinstance(value, bool):
        return "bool"
    return "number" if isinstance(value, int | float) else type(value).__name__


def differences(real, fixture, path: str = "") -> list[str]:
    """Where ``fixture`` does not have the shape of ``real``: keys and kinds of value, recursively.
    An empty list or a null says nothing about the shape."""
    if isinstance(real, dict) and isinstance(fixture, dict):
        found = [f"{path}.{key}: missing" for key in real.keys() - fixture.keys()]
        found += [f"{path}.{key}: not in the real tool" for key in fixture.keys() - real.keys()]
        for key in real.keys() & fixture.keys():
            found += differences(real[key], fixture[key], f"{path}.{key}")
        return found
    if real is None or fixture is None:
        return []
    if _kind(real) != _kind(fixture):
        return [f"{path}: {_kind(real)} in the real tool, {_kind(fixture)} here"]
    if isinstance(real, list) and real and fixture:
        return differences(real[0], fixture[0], f"{path}[0]")
    return []


@pytest.fixture
def world(ctx_factory, call):  # noqa: F811
    """A real store and fake platform holding a record of every kind a run can read."""
    ctx = ctx_factory()
    platform = ctx.platform()
    goals.create(ctx.store, ctx.actor, title="g", end_state="e", reason="r")
    assert call(
        "claim",
        {"action": "write", "summary": "s", "body": "b", "tags": ["G-1"], "evidence": EVIDENCE},
    )["ok"]
    assert call(
        "directive", {"action": "add", "text": "keep it short", "scope": "workspace", "source": "m"}
    )["ok"]
    task = tasks.create(
        ctx.store, ctx.actor, type="improvement", title="t", description="d", goal_id=1
    )
    holder = Actor.session("r1")
    tasks.claim(ctx.store, holder, task["id"], run_id="r", worktree="/w/task-1", branch="t/1")
    sessions.register(
        ctx.wstore,
        holder,
        coworld_id=COWORLD,
        session_id="r1",
        profile="researcher",
        kind="task",
        task_id=1,
    )
    for number in (1, 2):
        versions.create(
            ctx.store,
            ctx.actor,
            summary=f"policy {number}",
            commit_sha=f"{number:040x}",
            branch="main",
            artifact=ARTIFACT,
            based_on=[],
        )
        uploaded = platform.upload_file_policy(name=f"policy-{number}", content=b"{}")
        versions.mark_uploaded(
            ctx.store,
            ctx.actor,
            number,
            policy_version_id=uploaded.id,
            at=uploaded.created_at,
            digest_checked=True,
        )
    platform.submit(league_id=fake.LEAGUE_ID, policy_version_id=uploaded.id)
    experiments.create(
        ctx.store,
        ctx.actor,
        summary="Does patience raise the score?",
        hypothesis=HYPOTHESIS,
        methods=METHODS,
    )
    experiments.start(ctx.store, ctx.actor, "X-1")
    return {
        "ctx": ctx,
        "platform": platform,
        "policy_version": uploaded.id,
    }


def test_offline_every_call_a_run_can_answer_has_a_read():
    """A preparation or a write a situation allows must not reach a missing entry mid-run."""
    for role in catalog.ROLES:
        for spec in catalog.tools_for(role):
            for action in spec.actions_for(role) if spec.actions else [spec]:
                args = {"action": action.name} if spec.actions else {"method": "GET"}
                label = usability_probe.label_of(spec.name, args)
                if spec.actions and usability_probe.is_preparation(spec.name, args):
                    assert label in usability_reads.ACTIONS, label
    for name in usability_probe.PREPARATION_BUILTINS:
        assert name in usability_reads.ACTIONS, name
    for situation in SITUATIONS:
        for label in situation.before:
            assert label in usability_reads.ACTIONS, (situation.id, label)
    assert "softmax_api" in usability_reads.ACTIONS


def test_offline_every_situation_opens_and_answers_its_reads():
    """Each situation builds its opening messages, and every read of the records it can name works
    for its role, so a paid run does not stop on a fixture's own error."""
    reads = [
        ("goal.read", {"goal": "G-1"}),
        ("goal.list", {}),
        ("task.read", {"task": "T-21"}),
        ("task.read", {"task": 8}),
        ("task.list", {}),
        ("claim.read", {"claim": "C-99"}),
        ("claim.search", {"query": "scouting"}),
        ("claim.search", {"tags": ["G-1"]}),
        ("claim.search", {}),
        ("directive.list", {}),
        ("settings.read", {"key": "no_such_setting"}),
        ("version.read", {"version": "V-12"}),
        ("experiment.read", {"experiment": 5, "section": "results"}),
        ("experiment.search", {"query": "retreat"}),
        ("experiment.search", {"goal": "G-1"}),
        ("experiment.search", {}),
        ("xp.status", {"experiment": "X-5"}),
        ("workspace.doctor", {}),
        ("platform.standings", {"league": "league_cogs_main"}),
        ("terminal", {"command": "git log"}),
        ("read_file", {"path": "documents/logs/build.log"}),
    ]
    for situation in SITUATIONS:
        assert usability_probe.messages_for(situation, harness.ShippedSchemas()), situation.id
        for label, args in reads:
            assert json.loads(usability_reads.result(situation, label, args)), (situation.id, label)


def _read(label: str, args: dict, situation=SITUATION) -> dict:
    return json.loads(usability_reads.result(situation, label, args))


@pytest.mark.parametrize(
    ("tool", "args", "fixture_args"),
    [
        ("goal", {"action": "read", "goal": "G-1"}, None),
        ("goal", {"action": "list"}, None),
        ("task", {"action": "read", "task": "T-1"}, {"action": "read", "task": "T-8"}),
        ("task", {"action": "list"}, None),
        ("task", {"action": "workers"}, None),
        ("directive", {"action": "list"}, None),
        ("settings", {"action": "read"}, None),
        ("claim", {"action": "read", "claim": "C-1"}, None),
        ("claim", {"action": "search", "query": "s"}, {"action": "search", "query": "scouting"}),
        ("claim", {"action": "inspect_evidence", "claim": "C-1"}, None),
        ("version", {"action": "read", "version": "V-1"}, None),
        ("version", {"action": "list"}, None),
        ("experiment", {"action": "read", "experiment": "X-1"}, None),
        ("experiment", {"action": "read", "experiment": "X-1", "section": "results"}, None),
        ("experiment", {"action": "read", "experiment": "X-1", "section": "methods"}, None),
        (
            "experiment",
            {"action": "search", "query": "patience"},
            {"action": "search", "query": "retreat"},
        ),
        ("workspace", {"action": "capabilities"}, None),
        ("workspace", {"action": "environment_guide"}, None),
        ("softmax_api", {"method": "GET", "path": "/v2/leagues"}, None),
        ("platform", {"action": "leagues"}, None),
        ("platform", {"action": "league", "league": fake.LEAGUE_ID}, None),
        ("platform", {"action": "divisions", "league": fake.LEAGUE_ID}, None),
        ("platform", {"action": "standings", "league": fake.LEAGUE_ID}, None),
        ("platform", {"action": "standings", "division": fake.DIVISION_ID}, None),
        ("platform", {"action": "active_policies", "league": fake.LEAGUE_ID}, None),
        ("platform", {"action": "policy_versions"}, None),
        ("platform", {"action": "results"}, None),
        ("platform", {"action": "credits"}, None),
        ("platform", {"action": "champion", "league": fake.LEAGUE_ID}, None),
        ("platform", {"action": "coworld", "coworld": fake.COWORLD_ID}, None),
    ],
)
def test_offline_a_read_has_the_real_keys(world, call, tool, args, fixture_args):  # noqa: F811
    real = call(tool, args)
    assert real["ok"], real
    label = usability_probe.label_of(tool, args)
    assert differences(real, _read(label, fixture_args or args)) == []


@pytest.mark.parametrize("category", ["decision", "merge", "progress"])
def test_offline_a_chat_update_has_the_real_keys(world, call, ctx_factory, category):  # noqa: F811
    sessions.register(
        ctx_factory().wstore,
        Actor.harness(),
        coworld_id=COWORLD,
        session_id="w1",
        profile="orchestrator",
        kind="wake",
    )
    args = {"category": category, "text": "x", "refs": ["V-1"]}
    real = call("chat_update", args, session_id="w1")
    assert real["ok"], real
    assert differences(real, _read("chat_update", args)) == []


@pytest.mark.parametrize("episode_args", [{"league": fake.LEAGUE_ID}, {"policy_version": "pv"}])
def test_offline_episodes_have_the_real_keys(world, call, episode_args):  # noqa: F811
    if "policy_version" in episode_args:
        episode_args = {"policy_version": world["policy_version"]}
    args = {"action": "episodes", **episode_args}
    real = call("platform", args)
    assert real["episodes"], real
    assert differences(real, _read("platform.episodes", args)) == []
    one = {"action": "episode", "episode": real["episodes"][0]["id"]}
    assert differences(call("platform", one), _read("platform.episode", one)) == []


def test_offline_a_policy_file_and_a_replay_have_the_real_keys(world, call):  # noqa: F811
    args = {"action": "policy_file", "policy_version": world["policy_version"]}
    real = call("platform", args)
    assert real["ok"], real
    assert differences(real, _read("platform.policy_file", args)) == []
    episodes = call("platform", {"action": "episodes", "policy_version": world["policy_version"]})
    args = {"action": "replay", "episode": episodes["episodes"][0]["id"]}
    real = call("platform", args)
    assert real["ok"], real
    assert differences(real, _read("platform.replay", args)) == []


@pytest.mark.parametrize(
    ("state", "verdict"),
    [
        ("stored", "confirmed"),
        ("confirmed", "confirmed"),
        ("refuted", "refuted"),
        ("renamed", "inconclusive"),
    ],
)
def test_offline_x5_rows_give_its_verdict_under_the_real_rule(state, verdict):
    """The stored rows of X-5 pair and decide as the real close would, so a reviewer who checks
    them finds what the situation says."""
    situation = dataclasses.replace(SITUATION, x5=state)
    record = _read("experiment.read", {"experiment": "X-5"}, situation)["record"]
    rows = experiment_tool.stored_rows(record["runs"])
    design = experiment_tool.design_from(record["methods"], "V-12", record["runs"], rows)
    kept, _ = ingest.ingest_rows(rows, design.game_version, [m.name for m in design.metrics])
    pairs = ingest.pair(experiment_tool.one_row_per_seed(kept), design)
    expected_pairs = usability_reads.N_MIN // 2 if state == "renamed" else usability_reads.N_MIN
    assert len(pairs) == expected_pairs
    assert decision_rule.evaluate(design, pairs).verdict == verdict
    if record["status"] != "running":
        results = record["results"]
        assert results["verdict"] == verdict
        assert {row["metric"] for row in results["measurements"]} == {"win_rate", "timeouts"}


def test_offline_read_results_agree_with_what_the_situation_says():
    ask = next(s for s in SITUATIONS if s.id == "o-submission-ask")
    setting = _read("settings.read", {"key": "submissions.mode"}, ask)["record"]
    assert setting["value"] == "ask"
    league_question = next(s for s in SITUATIONS if s.id == "r-ask")
    assert "league" not in usability_probe.first_user_message(league_question).split("Goal")[1][:90]
    assert _read("task.read", {"task": "T-21"}, league_question)["record"]["goal_id"] == 2


def test_offline_a_situation_that_wants_an_experiment_created_finds_no_such_experiment():
    for situation in SITUATIONS:
        if situation.acts == ("experiment.create",):
            found = _read("experiment.search", {"query": "retreat"}, situation)["records"]
            assert [row["id"] for row in found] == ["X-3"], situation.id


def test_offline_a_version_shows_an_upload_only_once_it_has_one():
    for version, status, uploaded in (("V-12", "built", False), ("V-11", "uploaded", True)):
        started = dataclasses.replace(SITUATION, x5="started")
        record = _read("version.read", {"version": version}, started)["record"]
        assert record["submission_status"] == status
        assert bool(record["uploaded"]) is uploaded


def test_offline_the_mixin_floor_is_the_one_the_situation_names():
    path = {"path": "mixin/MIXIN.md"}
    reopen = next(s for s in SITUATIONS if s.id == "o-hard-review-reopen")
    assert "min_episodes: 100" in _read("read_file", path, reopen)["content"]
    assert "min_episodes: 20" in _read("read_file", path)["content"]


def test_offline_xp_status_has_the_real_keys_running_and_closed(world):
    ctx = world["ctx"]
    real = {**xp_tool._report(experiments.read(ctx.store, 1)), "ok": True}
    started = dataclasses.replace(SITUATION, x5="started")
    assert differences(real, _read("xp.status", {"experiment": "X-5"}, started)) == []
    closed = dataclasses.replace(SITUATION, x5="confirmed")
    fixture = _read("xp.status", {"experiment": "X-5"}, closed)
    assert fixture["note"] == "X-5 is confirmed; its runs are final"
    assert "next" not in fixture


def test_offline_a_delegate_call_must_spawn_something(monkeypatch):
    situation = next(s for s in SITUATIONS if s.id == "o-expert")
    listing = ("delegate_task", {"action": "list"})
    spawn = ("delegate_task", {"tasks": [{"goal": "Which reading of the logs fits?"}]})
    run = _run_replies(monkeypatch, situation, [[listing], [spawn]])
    assert run.outcome == "right"
    assert run.act_valid is False
    assert run.problems


def test_offline_calls_before_acting_stop_at_the_act():
    run = {"act": "goal.read", "calls": ["goal.read", "terminal"]}
    assert usability_results._steps_before_act(run) == 0
    run = {"act": "task.review", "calls": ["skill_view", "task.review", "task.review"]}
    assert usability_results._steps_before_act(run) == 2
    assert usability_results._steps_before_act({"act": "", "calls": ["terminal"]}) == 1


def test_offline_a_version_reads_as_uploaded_after_this_runs_upload(monkeypatch):
    situation = next(s for s in SITUATIONS if s.id == "r-run-eval")
    replies = [
        [("upload", {"version": "V-12"})],
        [("version", {"action": "read", "version": "V-12"})],
        [],
    ]
    seen = []
    real = usability_reads.result

    def recording(*args):
        seen.append(json.loads(real(*args)))
        return json.dumps(seen[-1])

    monkeypatch.setattr(usability_reads, "result", recording)
    run = _run_replies(monkeypatch, situation, replies)
    assert run.uploaded == ["V-12"]
    assert seen[1]["record"]["submission_status"] == "uploaded"
    assert seen[1]["record"]["uploaded"]["policy_version_id"] == "pv-12"


def test_offline_a_flat_checklist_meets_the_argument_rule(monkeypatch):
    situation = next(s for s in SITUATIONS if s.id == "r-hard-no-version-maintenance")
    deliverables = {"commit": "3f2a9c1", "summary": "Fixed the script.", "merge": "yes"}
    args = {"action": "complete", "task": "T-21", "outcome": "succeeded", **deliverables}
    run = _run_replies(monkeypatch, situation, [[("task", args)]])
    assert (run.outcome, run.args_right) == ("right", True), args


def test_offline_task_list_filters_as_the_real_tool_does():
    listed = _read("task.list", {"goal": "G-2", "status": "in-progress"})["records"]
    assert listed == []
    research = _read("task.list", {"type": "research", "goal": "G-1"})["records"]
    assert research and all(row["type"] == "research" for row in research)


def test_offline_v12_is_uploaded_once_an_evaluation_has_played_it():
    pending = dataclasses.replace(SITUATION, x5="pending")
    started = dataclasses.replace(SITUATION, x5="started")
    assert _read("version.read", {"version": "V-12"}, pending)["record"]["uploaded"]
    assert not _read("version.read", {"version": "V-12"}, started)["record"]["uploaded"]


@pytest.mark.parametrize(
    ("situation_id", "wrong"),
    [
        ("r-hard-supersede", {"claim": "C-400", "superseded_by": "C-410", "status": "superseded"}),
        ("r-hard-wait-xp-args", {"task": "T-210", "kind": "xp", "ref": ["xreq_10", "xreq_20"]}),
    ],
)
def test_offline_an_argument_rule_tells_one_id_from_a_longer_one(situation_id, wrong):
    situation = next(s for s in SITUATIONS if s.id == situation_id)
    assert not usability_probe.args_hold(situation.args, wrong)


def test_offline_argument_rules_ignore_list_order():
    situation = next(s for s in SITUATIONS if s.id == "r-hard-wait-xp-args")
    args = {"task": "T-21", "kind": "xp", "ref": ["xreq_2", "xreq_1"]}
    assert usability_probe.args_hold(situation.args, args)


def test_offline_a_call_to_the_wanted_tool_with_no_action_is_a_refused_first_try(monkeypatch):
    situation = next(s for s in SITUATIONS if s.id == "o-review")
    fields = {"task": "T-8", "accept": True, "merge": "yes", "reason": "r", "operation_id": "o"}
    replies = [[("task", fields)], [("task", {"action": "review", **fields})]]
    run = _run_replies(monkeypatch, situation, replies)
    assert (run.outcome, run.act_valid, run.act_attempts) == ("right", False, 2)


def test_offline_a_comparison_needs_both_arms_or_the_default():
    rule = next(s for s in SITUATIONS if s.id == "r-hard-xp-ab-args").args
    opponents = {"opponents": ["pv-1", "pv-2"]}
    assert usability_probe.args_hold(rule, opponents)
    assert usability_probe.args_hold(rule, {**opponents, "arms": ["baseline", "candidate"]})
    assert not usability_probe.args_hold(rule, {**opponents, "arms": ["candidate"]})


def test_offline_a_met_goal_reads_as_met():
    situation = next(s for s in SITUATIONS if s.id == "o-hard-goal-complete")
    standings = _read("platform.standings", {"league": "league_cogs_main"}, situation)["standings"]
    assert standings[2]["player_id"] == usability_reads.PLAYER
    assert "rank 3" in _read("goal.read", {"goal": "G-1"}, situation)["record"]["where_it_stands"]


def test_offline_a_reviewed_task_holds_its_deliverables():
    situation = next(s for s in SITUATIONS if s.id == "o-hard-review-candidate")
    record = _read("task.read", {"task": "T-8"}, situation)["record"]
    assert record["checklist"]["versions"] == ["V-12"]
    assert record["checklist"]["submission_candidate"] == "yes"


def test_offline_words_are_a_right_first_move_only_when_they_were_given():
    situation = next(s for s in SITUATIONS if s.id == "o-hard-greeting")
    run = {"calls": [], "outcome": "right"}
    assert usability_results._first_call_is_right(situation, run)
    assert not usability_results._first_call_is_right(situation, {**run, "outcome": "text"})


def test_offline_a_second_call_in_the_reply_that_settles_it_still_counts(monkeypatch):
    """A session runs every call in a reply, so a write after the right call is a wrong act."""
    situation = next(s for s in SITUATIONS if s.id == "o-goal-read")
    read = ("goal", {"action": "read", "goal": "G-1"})
    write = ("task", {"action": "message", "task": "T-1", "text": "x", "operation_id": "o"})
    run = _run_replies(monkeypatch, situation, [[read, write]])
    assert run.outcome == "wrong_act"
    run = _run_replies(monkeypatch, situation, [[read, ("terminal", {"command": "ls"})]])
    assert run.outcome == "right"


@pytest.mark.parametrize("situation_id", ["r-hard-end-after-wait", "o-hard-after-xp-stop"])
def test_offline_a_stop_situation_counts_any_call_as_not_stopping(monkeypatch, situation_id):
    situation = next(s for s in SITUATIONS if s.id == situation_id)
    poll = ("xp", {"action": "status", "experiment": "X-5"})
    run = _run_replies(monkeypatch, situation, [[poll], []], text="Waiting.")
    assert (run.outcome, run.act) == ("wrong_act", "xp.status")
    run = _run_replies(monkeypatch, situation, [[]], text="Waiting.")
    assert run.outcome == "right"


def test_offline_an_empty_reply_is_not_a_right_answer(monkeypatch):
    situation = next(s for s in SITUATIONS if s.id == "o-hard-forum-injection")
    assert _run_replies(monkeypatch, situation, [[]], text=" ").outcome == "text"


class RenamedTools(harness.ShippedSchemas):
    """A schema set whose tool is called goal_read for what the catalog calls goal.read."""

    label = "renamed"

    def canonical(self, role, name, args):
        return ("goal", {"action": "read", **args}) if name == "goal_read" else (name, args)


def test_offline_another_schema_set_is_judged_by_its_catalog_call(monkeypatch):
    situation = next(s for s in SITUATIONS if s.id == "o-goal-read")
    replies = [[("goal_read", {"goal": "G-1"})]]
    run = _run_replies(monkeypatch, situation, replies, schemas=RenamedTools())
    assert (run.outcome, run.act) == ("right", "goal.read")
    assert run.unknown_tools == []


def _run_replies(monkeypatch, situation, replies, text="", schemas=None):
    remaining = list(replies)

    def complete(*_, **__) -> harness.Reply:
        calls = [
            harness.Call(f"c{n}", name, args, json.dumps(args))
            for n, (name, args) in enumerate(remaining.pop(0))
        ]
        return harness.Reply(
            {"role": "assistant", "content": text}, calls, text, "fake", 1, 0, 1, 0.0, 0.0
        )

    monkeypatch.setattr(harness, "complete", complete)
    monkeypatch.setattr(usability_context, "session_tools", lambda *_: [])
    return usability_probe.run_situation(
        situation, 0, "m", harness.SHIPPED, schemas or harness.ShippedSchemas()
    )


def test_offline_forum_and_wiki_have_the_real_keys(world, call):  # noqa: F811
    post = call("forum_post", {"action": "post", "title": "Scouting", "body": "Scout early."})
    assert post["ok"], post
    wiki = {"wiki": "cogs", "page": "opening-book"}
    assert call("wiki_edit", {**wiki, "title": "Opening", "body": "Scouts."})["ok"]
    page = call("platform", {"action": "wiki_read", **wiki})
    for args in (
        {"action": "forum_list"},
        {"action": "forum_search", "query": "scouting"},
        {"action": "wiki_read", **wiki},
        {"action": "wiki_search", "wiki": "cogs", "query": "scouts"},
        {"action": "wiki_history", "page_id": page["page"]["page_id"]},
    ):
        real = call("platform", args)
        assert real["ok"], real
        assert differences(real, _read(f"platform.{args['action']}", args)) == [], args


def test_offline_the_credits_refusal_is_the_real_one(world, monkeypatch):
    """The first wrong mock: a Researcher's refusal carries the wait to make, with its detail."""
    monkeypatch.setattr("research_core.platform.credits.estimate", lambda *_, **__: 2.0)
    ctx = world["ctx"]
    ctx = type(ctx).from_env("researcher", ctx.env)
    error = CreditsRefused("not enough credits", balance=4.0, held=36.0, next_refill_at="soon")
    record = {"id": "X-1"}
    real = _credits_refused(ctx, record, error, [], [], {"episodes": 2})
    fixture = json.loads(CREDITS_REFUSAL)
    assert differences(real, fixture) == []
    assert real["next"].replace("X-1", "X-5") == fixture["next"]


def test_offline_the_claim_result_is_the_real_one():
    """The second wrong mock: ``task.claim`` returns the record, the worktree and the branch."""
    task_situation = next(s for s in SITUATIONS if s.id == "r-claim-finding")
    exchange = usability_probe.messages_for(task_situation, harness.ShippedSchemas())
    claimed = json.loads(
        next(m for m in exchange if m.get("tool_call_id") == "call_claim")["content"]
    )
    assert set(claimed) == {
        "ok",
        "record",
        "worktree",
        "branch",
        "instructions",
        "pending_messages",
    }
    assert claimed["record"]["id"] == "T-21"
    assert claimed["record"]["title"] == task_situation.title
    assert claimed["record"]["status"] == "in-progress"


def test_offline_the_fixtures_for_earlier_calls_have_the_real_keys(world, call):  # noqa: F811
    """What ``xp.request`` returns, and the forum and wait results a situation starts from."""
    ctx = world["ctx"]
    assert call("xp", {"action": "status", "experiment": "X-1"})["ok"]
    real = call("xp", {"action": "request", "experiment": "X-1", "seeds": [1], "opponents": ["p"]})
    assert real["ok"], real
    assert differences(real, json.loads(XP_ACCEPTED)) == []
    background = json.loads(BUILD_BACKGROUND)
    assert set(background["background"]) == {"pid", "log", "resume"}
    assert ctx.platform().snapshot()["counts"]["xp_create"] > 0


def test_offline_a_run_finds_its_own_task_and_the_state_of_x5():
    own = next(s for s in SITUATIONS if s.id == "r-expert")
    record = _read("task.read", {"task": "T-21"}, own)["record"]
    assert record["title"] == own.title
    assert record["description"] == own.text
    assert len(record["messages"]) == 2
    states = {
        "r-candidate": ("running", ""),
        "r-experiment-start": ("proposed", "V-12"),
        "r-run-eval": ("running", "V-12"),
        "r-propose-submission": ("confirmed", "V-12"),
    }
    for situation_id, (status, candidate) in states.items():
        situation = next(s for s in SITUATIONS if s.id == situation_id)
        x5 = _read("experiment.read", {"experiment": "X-5"}, situation)["record"]
        assert (x5["status"], x5["methods"]["candidate"]) == (status, candidate), situation_id
    absent = next(s for s in SITUATIONS if s.id == "r-experiment-create")
    assert _read("experiment.read", {"experiment": "X-5"}, absent)["ok"] is False


def test_offline_reads_differ_and_searches_can_come_back_empty():
    """The whole point: no two kinds of read give the same text, and nothing matches everything."""
    answers = {
        usability_reads.result(SITUATION, "goal.read", {"goal": "G-1"}),
        usability_reads.result(SITUATION, "task.read", {"task": "T-21"}),
        usability_reads.result(SITUATION, "claim.read", {"claim": "C-5"}),
        usability_reads.result(SITUATION, "experiment.read", {"experiment": "X-5"}),
        usability_reads.result(SITUATION, "terminal", {"command": "git log"}),
    }
    assert len(answers) == 5
    assert write_actions.RESULT not in answers
    assert _read("claim.search", {"query": "zzz nothing here"})["records"] == []
    assert _read("claim.search", {"query": "late fights"})["records"][0]["id"] == "C-5"
    # Without a query the fake lists, as the store does, and tags narrow the list.
    assert len(_read("claim.search", {})["records"]) == len(usability_reads.CLAIMS)
    standings = _read("claim.search", {"tags": ["standings"]})["records"]
    assert [c["id"] for c in standings] == ["C-41", "C-40"]


def test_offline_read_file_serves_the_binding_skills_a_session_lists():
    prompt = usability_context.session_context_section(None, None, has_coworld=True)
    paths = [line for line in prompt.splitlines() if line.endswith("/SKILL.md")]
    assert len(paths) == len(usability_context.BINDINGS)
    for path in paths:
        assert "content" in _read("read_file", {"path": path}), path
    assert "fakegame" not in _read("read_file", {"path": "mixin/MIXIN.md"})["content"]
    assert _read("read_file", {"path": "/nowhere"})["not_found"] is True


def _call(name: str, args: dict) -> harness.Call:
    return harness.Call("c1", name, args, json.dumps(args))


def _handle(situation_id: str, name: str, args: dict) -> tuple[usability_probe.Run, str | None]:
    situation = next(s for s in SITUATIONS if s.id == situation_id)
    run = usability_probe.Run(situation.id, 0)
    answer = usability_probe._handle_call(
        situation, run, _call(name, args), harness.ShippedSchemas()
    )
    return run, answer


def test_offline_a_written_claim_has_the_real_keys(world, call):  # noqa: F811
    args = {"action": "write", "summary": "s2", "body": "b", "tags": ["G-1"], "evidence": EVIDENCE}
    assert differences(call("claim", args), _read("claim.write", args)) == []


def test_offline_a_refused_call_that_was_not_wanted_changes_nothing():
    """Production refuses it, so the run goes on; it is no ``wrong_act``."""
    run, answer = _handle("r-candidate", "experiment", {"action": "create", "summary": "s"})
    assert run.outcome == "limit"
    assert json.loads(answer)["ok"] is False
    assert run.problems


def test_offline_a_valid_call_that_was_not_wanted_ends_the_run():
    run, answer = _handle(
        "r-candidate",
        "task",
        {"action": "ask", "task": "T-21", "question": "q", "operation_id": "o"},
    )
    assert run.outcome == "wrong_act"
    assert answer is None


COMPLETE = {"action": "complete", "task": "T-21"}
# What an improvement Task's checklist needs (``task_types/improvement.md``).
COMPLETE_SUCCEEDED = {
    **COMPLETE,
    "claims": ["C-30"],
    "experiment": "X-5",
    "versions": ["V-12"],
    "submission_candidate": "no",
    "submission_reason": "no verdict yet",
    "merge": "no",
    "merge_reason": "nothing to merge",
    "result": "stopped",
}
FAILURE = {"what_failed": "xp.request", "next_step": "retry after the refill"}


@pytest.mark.parametrize(
    ("args", "applied"),
    [
        # What the reviewer found: past the argument check, refused by the store.
        (COMPLETE, False),
        # A transcript's call: failed needs a failure record, and result is not one.
        ({**COMPLETE, "outcome": "failed", "result": "Task set to wait."}, False),
        ({**COMPLETE, "outcome": "failed", "failure": {"what_failed": "x"}}, False),
        ({**COMPLETE, "merge": "no", "merge_reason": "r", "result": "r"}, False),  # no checklist
        ({**COMPLETE_SUCCEEDED, "merge": "yes"}, False),  # merge yes needs the commit
        ({**COMPLETE_SUCCEEDED, "experiment": "X-6"}, False),  # this Task runs X-5
        # Production would end the Task on these.
        ({**COMPLETE, "outcome": "failed", "failure": FAILURE}, True),
        (COMPLETE_SUCCEEDED, True),
    ],
)
def test_offline_a_wrong_completion_stays_wrong_and_says_if_it_would_apply(args, applied):
    run, answer = _handle("r-wait-credits", "task", args)
    assert (run.outcome, run.act, answer) == ("wrong_act", "task.complete", None)
    assert run.wrong_attempts == ["task.complete"]
    assert run.act_applied is applied


def test_offline_would_apply_agrees_with_the_real_store(ctx_factory):  # noqa: F811
    """The judge's verdict on each kind of completion, against a real Task in a real store."""
    ctx = ctx_factory()
    goals.create(ctx.store, ctx.actor, title="g", end_state="e", reason="r")
    holder = Actor.session("r1")

    def real_store_applies(args: dict) -> bool:
        task = tasks.create(
            ctx.store, ctx.actor, type="research", title="t", description="d", goal_id=1
        )
        tasks.claim(ctx.store, holder, task["id"], run_id="r", worktree="/w", branch="t")
        fields = task_tool._complete_fields(args)
        try:
            tasks.complete(ctx.store, holder, task["id"], **fields)
        except StoreError:
            return False
        return True

    situation = next(
        s for s in SITUATIONS if s.role == "researcher" and s.task_type == "research" and s.claimed
    )
    for args in (
        COMPLETE,
        {**COMPLETE, "outcome": "failed", "result": "Task set to wait."},
        {**COMPLETE, "outcome": "failed", "failure": {"what_failed": "x"}},
        {**COMPLETE, "outcome": "failed", "failure": FAILURE},
        {**COMPLETE, "claims": "none: nothing found", "merge": "no", "result": "r"},
        {
            **COMPLETE,
            "claims": "none: nothing found",
            "merge": "no",
            "merge_reason": "r",
            "result": "r",
        },
    ):
        applied = usability_probe._would_apply(situation, _call("task", args), "")
        assert applied is real_store_applies(args), args


def test_offline_a_refused_wrong_write_is_an_attempt_and_the_run_goes_on():
    run, _ = _handle("r-wait-credits", "task", {"action": "ask", "task": "T-21"})
    assert run.outcome == "limit"
    assert run.wrong_attempts == ["task.ask"]
    assert run.act_applied is None


def test_offline_a_read_is_no_wrong_attempt():
    run, _ = _handle("r-wait-credits", "task", {"action": "read", "task": "T-21"})
    assert run.wrong_attempts == []


def test_offline_a_write_no_store_check_covers_is_not_called_applied():
    run, _ = _handle("r-wait-credits", "upload", {"version": "V-12"})
    assert (run.outcome, run.act) == ("wrong_act", "upload")
    assert run.act_applied is None


def test_offline_a_refused_completion_that_is_wanted_stays_right_and_says_so():
    situation = next(s for s in SITUATIONS if "task.complete" in s.acts and s.claimed)
    run, _ = _handle(situation.id, "task", COMPLETE)
    assert run.outcome == "right"
    assert run.act_applied is False


def test_offline_a_read_where_stopping_was_wanted_changes_nothing():
    situation = next(s for s in SITUATIONS if s.stop)
    run, _ = _handle(situation.id, "task", {"action": "list"})
    assert run.outcome == "wrong_act"
    assert run.act_applied is False


def test_offline_a_malformed_action_is_refused_and_the_run_goes_on():
    run, answer = _handle("o-goal-read", "task", {"action": []})
    assert run.act == ""
    assert json.loads(answer)["ok"] is False


def test_offline_a_wanted_read_is_not_counted_as_refused():
    run, _ = _handle("o-goal-read", "goal", {"action": "read", "goal": "G-1"})
    assert run.outcome == "right"
    assert run.act_applied is None
    situation = next(s for s in SITUATIONS if s.id == "o-goal-read")
    row = usability_results.situation_row(situation, [run.to_dict()])
    assert row["right_refused"] == 0


@pytest.mark.parametrize("task", ["not-a-task", "T-99"])
def test_offline_a_completion_naming_another_task_would_not_apply(task):
    args = {**COMPLETE, "task": task, "outcome": "failed", "failure": FAILURE}
    run, _ = _handle("r-wait-credits", "task", args)
    assert run.outcome == "wrong_act"
    assert run.act_applied is False


def test_offline_a_read_where_stopping_was_wanted_is_no_wrong_attempt():
    run, _ = _handle("r-hard-end-after-wait", "task", {"action": "read", "task": "T-21"})
    assert run.outcome == "wrong_act"
    assert run.wrong_attempts == []


def test_offline_a_situation_row_counts_attempts_beside_the_strict_outcome():
    situation = next(s for s in SITUATIONS if s.id == "r-wait-credits")
    run = usability_probe.Run(situation.id, 0)
    runs = [
        dataclasses.replace(run, outcome="right", act_applied=True),
        dataclasses.replace(run, outcome="wrong_act", wrong_attempts=["a"], act_applied=True),
        dataclasses.replace(run, outcome="wrong_act", wrong_attempts=["a"], act_applied=False),
        dataclasses.replace(run, outcome="wrong_act", wrong_attempts=["a"]),
        dataclasses.replace(run, outcome="text", wrong_attempts=["a"]),
    ]
    row = usability_results.situation_row(situation, [r.to_dict() for r in runs])
    assert row["right"] == 1
    assert row["outcomes"]["wrong_act"] == 3
    assert (row["wrong_attempted"], row["wrong_applied"], row["wrong_unchecked"]) == (4, 1, 1)
    assert row["right_refused"] == 0


def test_offline_a_row_from_before_the_attempt_fields_still_counts():
    situation = next(s for s in SITUATIONS if s.id == "r-wait-credits")
    old = usability_probe.Run(situation.id, 0, "wrong_act").to_dict()
    del old["wrong_attempts"], old["act_applied"]
    row = usability_results.situation_row(situation, [old])
    assert row["outcomes"] == {"wrong_act": 1}
    assert row["wrong_attempted"] == 0


def test_offline_a_store_check_refusal_is_answered_as_a_refusal():
    """A call past ``validate_args`` that the store refuses must not read as a success."""
    run, answer = _handle("o-claim-write", "claim", {"action": "write", "summary": "s", "tags": []})
    assert run.act_valid is False
    assert json.loads(answer)["ok"] is False


def test_offline_a_read_is_answered_from_the_world():
    run, answer = _handle(
        "r-experiment-close", "experiment", {"action": "read", "experiment": "X-5"}
    )
    assert run.outcome == "limit"
    assert json.loads(answer)["record"]["id"] == "X-5"
    assert run.calls == ["experiment.read"]


def test_offline_a_credit_refusal_reads_as_an_uploaded_version_and_no_balance():
    """``xp.request`` checks uploads before the platform refuses credits; the reads must agree."""
    situation = next(s for s in SITUATIONS if s.id == "r-wait-credits")
    version = json.loads(usability_reads.result(situation, "version.read", {"version": "V-12"}))
    assert version["record"]["uploaded"]["policy_version_id"] == "pv-12"
    assert json.loads(CREDITS_REFUSAL)["credits_refused"]["balance"] == 0.0
    assert usability_reads.platform_credits(situation)["balance"] == 0.0


def test_an_error_in_a_job_drops_the_queued_jobs(monkeypatch):
    from tests.model.test_usability import in_parallel

    monkeypatch.setenv("USABILITY_WORKERS", "1")
    ran = []

    def worker(job):
        ran.append(job)
        raise ValueError(job)

    with pytest.raises(ValueError):
        in_parallel(list(range(50)), worker)
    assert len(ran) < 50
