"""The ``platform`` tool on the fake platform: every action, the Player-only Researcher rule,
files and pins, and the wait a persistent platform error sets on a held Task (A3.9)."""

from __future__ import annotations

import hashlib
import json

import pytest

import research_core.tools.platform as platform_tool  # noqa: F401  (registers the tool)
from research_core.platform import fake, wait
from research_core.platform.errors import IdentityUnavailable, PlatformError
from research_core.platform.fake import FakePlatform
from research_core.store import goals, pins, sessions
from research_core.store.actors import Actor
from research_core.tools.base import PLAYER_ID_META, run_tool

LEAGUE = fake.LEAGUE_ID
DIVISION = fake.DIVISION_ID
POLICY = json.dumps({"parameters": {"aggression": 4, "patience": 7}}).encode()


@pytest.fixture
def workspace_env(tmp_path):
    """The shared env plus a User credential, so User-identity paths are reachable."""
    return {
        "IDE_WORKSPACE": str(tmp_path / "ws"),
        "IDE_PLATFORM": "fake",
        "SOFTMAX_USER_CREDENTIAL": "usr-token",
    }


@pytest.fixture
def platform(ctx_factory) -> FakePlatform:
    return ctx_factory().platform()


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(wait, "sleep", lambda _seconds: None)


def upload(platform: FakePlatform, name: str = "starter", content: bytes = POLICY):
    return platform.upload_file_policy(name=name, content=content)


def competing(platform: FakePlatform):
    """Upload, submit and settle: a competing membership that is this Player's champion."""
    version = upload(platform)
    platform.submit(league_id=LEAGUE, policy_version_id=version.id)
    platform.memberships(league_id=LEAGUE)
    return version


def documents(ctx_factory):
    return ctx_factory().coworld().documents


# --- spec -------------------------------------------------------------------------------------


def test_spec_is_a_workspace_tool_for_both_roles():
    spec = platform_tool.SPEC
    assert spec.executor == "workspace"
    assert spec.roles == {"orchestrator", "researcher"}
    assert set(platform_tool.ACTIONS) == {action.name for action in spec.actions}
    assert set(platform_tool.ACTIONS) == {
        "leagues", "league", "divisions", "standings", "active_policies", "policy_versions",
        "policy_file", "results", "episodes", "episode", "replay", "credits", "champion",
        "forum_list", "forum_read", "forum_search", "wiki_read", "wiki_search", "wiki_history",
        "coworld", "coworld_update", "episode_summaries", "replays", "episode_results",
    }  # fmt: skip


# --- leagues, divisions, standings ------------------------------------------------------------


def test_leagues_league_divisions(call):
    listed = call("platform", {"action": "leagues"})
    assert listed["ok"] is True
    assert [row["id"] for row in listed["leagues"]] == [LEAGUE]
    assert call("platform", {"action": "leagues", "game": "game_other"})["leagues"] == []

    one = call("platform", {"action": "league", "league": LEAGUE}, role="researcher")
    assert one["league"]["name"] == fake.LEAGUE_NAME
    assert one["league"]["coworld_id"] == fake.COWORLD_ID

    divisions = call("platform", {"action": "divisions", "league": LEAGUE})
    assert [row["id"] for row in divisions["divisions"]] == [DIVISION]


def test_league_accepts_a_platform_url(call):
    url = f"https://softmax.com/leagues/{LEAGUE}?tab=standings"
    assert call("platform", {"action": "league", "league": url})["league"]["id"] == LEAGUE


def test_standings_by_division_and_by_league(call):
    by_division = call("platform", {"action": "standings", "division": DIVISION})
    assert by_division["division"] == DIVISION
    assert [row["player_name"] for row in by_division["standings"]] == ["House A", "House B"]
    assert all(row["division_id"] == DIVISION for row in by_division["standings"])

    by_league = call("platform", {"action": "standings", "league": LEAGUE})
    assert by_league["league"] == LEAGUE
    assert [row["id"] for row in by_league["divisions"]] == [DIVISION]
    assert by_league["standings"] == by_division["standings"]


def test_standings_needs_a_division_or_a_league(call):
    result = call("platform", {"action": "standings"})
    assert result["ok"] is False and "division or a league" in result["error"]


def test_unknown_league_is_a_refusal_with_route_and_status(call):
    result = call("platform", {"action": "league", "league": "league_nope"})
    assert result["ok"] is False
    assert result["context"]["status"] == 404
    assert result["context"]["route"] == "GET /v2/leagues/league_nope"
    assert result["context"]["retryable"] is False
    assert result["context"]["waiting"] is False


# --- active policies --------------------------------------------------------------------------


def test_active_policies_and_exclude_player(call, platform):
    version = competing(platform)
    everyone = call("platform", {"action": "active_policies", "league": LEAGUE})
    assert [m["policy_version"]["id"] for m in everyone["memberships"]] == [version.id]
    assert everyone["memberships"][0]["policy_version"]["label"] == "starter:v1"
    assert everyone["memberships"][0]["is_champion"] is True
    assert everyone["excluded"] == {"player": None, "user_players": None}

    mine_dropped = call(
        "platform", {"action": "active_policies", "league": LEAGUE, "exclude_player": True}
    )
    assert mine_dropped["memberships"] == []
    assert mine_dropped["excluded"]["player"] == fake.DEFAULT_PLAYER_ID


def test_exclude_user_in_the_fake_falls_back_to_this_player(call, platform):
    competing(platform)
    result = call("platform", {"action": "active_policies", "league": LEAGUE, "exclude_user": True})
    assert result["ok"] is True
    assert result["memberships"] == []
    assert result["excluded"]["player"] == fake.DEFAULT_PLAYER_ID
    assert result["excluded"]["user_players"] is None
    assert "only this Player" in result["note"]


def test_exclude_user_without_a_user_identity_names_it(call, workspace_env):
    workspace_env.pop("SOFTMAX_USER_CREDENTIAL")
    result = call("platform", {"action": "active_policies", "league": LEAGUE, "exclude_user": True})
    assert result["ok"] is False
    assert "User identity not provisioned" in result["error"]


def test_researcher_is_player_only(call):
    excluded = call(
        "platform",
        {"action": "active_policies", "league": LEAGUE, "exclude_user": True},
        role="researcher",
    )
    assert excluded["ok"] is False and "Player only" in excluded["error"]
    assert excluded["context"]["identity"] == "user"

    as_user = call("platform", {"action": "policy_versions", "identity": "user"}, role="researcher")
    assert as_user["ok"] is False and "Player only" in as_user["error"]

    as_player = call("platform", {"action": "policy_versions"}, role="researcher")
    assert as_player["ok"] is True and as_player["identity"] == "player"


# --- policy versions and files ----------------------------------------------------------------


def test_policy_versions_by_name_player_and_user(call, platform):
    upload(platform, "starter")
    upload(platform, "starter", b"other bytes")
    upload(platform, "bold")
    mine = call("platform", {"action": "policy_versions"})
    assert [row["label"] for row in mine["policy_versions"]] == [
        "bold:v1", "starter:v1", "starter:v2"
    ]  # fmt: skip
    named = call("platform", {"action": "policy_versions", "name": "starter"})
    assert [row["version"] for row in named["policy_versions"]] == [1, 2]
    other = call("platform", {"action": "policy_versions", "player": "ply_other"})
    assert other["policy_versions"] == []
    as_user = call("platform", {"action": "policy_versions", "identity": "user"})
    assert as_user["identity"] == "user" and len(as_user["policy_versions"]) == 3


def test_policy_file_writes_the_uploaded_bytes(call, platform, ctx_factory):
    version = upload(platform)
    result = call("platform", {"action": "policy_file", "policy_version": version.id})
    assert result["ok"] is True
    path = documents(ctx_factory) / "policies" / f"{version.id}.txt"
    assert result["path"] == str(path)
    assert result["content_type"] == "text/plain; charset=utf-8"
    assert path.read_bytes() == POLICY
    assert result["size_bytes"] == len(POLICY)
    assert result["sha256"] == hashlib.sha256(POLICY).hexdigest()
    assert result["identity"] == "player"


def test_policy_file_tries_the_player_then_the_user(call):
    result = call("platform", {"action": "policy_file", "policy_version": "pv_nope"})
    assert result["ok"] is False
    assert result["context"]["tried"] == ["player", "user"]
    assert result["context"]["status"] == 404
    assert "refused as player and as user" in result["error"]


def test_policy_file_researcher_does_not_fall_back_to_the_user(call):
    result = call(
        "platform", {"action": "policy_file", "policy_version": "pv_nope"}, role="researcher"
    )
    assert result["ok"] is False
    assert result["context"]["tried"] == ["player"]
    assert "belongs to the Orchestrator" in result["error"]


def test_policy_file_takes_a_label(call, platform):
    version = upload(platform)
    result = call("platform", {"action": "policy_file", "policy_version": version.label})
    assert result["ok"] is True and result["policy_version"] == version.id
    missing = call("platform", {"action": "policy_file", "policy_version": "nope:v9"})
    assert missing["ok"] is False and missing["context"]["status"] == 404


def test_policy_file_of_an_image_policy_says_why(call, monkeypatch):
    def no_file(self, policy_version_id, identity="player"):
        raise PlatformError("Policy version has no player file", status=409)

    monkeypatch.setattr(FakePlatform, "policy_file", no_file)
    result = call("platform", {"action": "policy_file", "policy_version": "pv_x"})
    assert result["ok"] is False and "image policy" in result["error"]
    assert result["context"]["status"] == 409 and "next" in result["context"]


def test_policy_file_without_a_user_identity_keeps_the_players_refusal(call, monkeypatch):
    real_policy_file = FakePlatform.policy_file

    def no_user(self, policy_version_id, identity="player"):
        if identity == "user":
            raise IdentityUnavailable("user")
        return real_policy_file(self, policy_version_id, identity=identity)

    monkeypatch.setattr(FakePlatform, "policy_file", no_user)
    result = call("platform", {"action": "policy_file", "policy_version": "pv_nope"})
    assert result["ok"] is False and result["context"]["status"] == 404
    assert result["context"]["tried"] == ["player"]


# --- results, episodes, replays ---------------------------------------------------------------


def test_a_coworld_with_no_league_sees_no_other_coworlds_league(ctx_factory):
    ctx = ctx_factory(coworld="cow_other")
    results = json.loads(run_tool("platform", {"action": "results", "player": "ply_x"}, ctx))
    assert results["leagues"] == []
    forum = json.loads(run_tool("platform", {"action": "forum_list"}, ctx))
    assert forum["ok"] is False and "slug is required" in forum["error"]


def test_the_player_id_stored_by_the_login_check_saves_a_whoami(call, ctx_factory, monkeypatch):
    def no_whoami(self, identity="player"):
        raise AssertionError("whoami was called")

    ctx_factory().wstore.set_meta(PLAYER_ID_META, "ply_stored")
    monkeypatch.setattr(FakePlatform, "whoami", no_whoami)
    assert call("platform", {"action": "results", "league": LEAGUE})["player"] == "ply_stored"


def test_results_for_this_player(call, platform):
    version = competing(platform)
    result = call("platform", {"action": "results"})
    assert result["player"] == fake.DEFAULT_PLAYER_ID
    (league,) = result["leagues"]
    assert league["league"]["id"] == LEAGUE
    assert [m["policy_version"]["id"] for m in league["memberships"]] == [version.id]
    assert [row["player_id"] for row in league["standings"]] == [fake.DEFAULT_PLAYER_ID]
    assert league["standings"][0]["policy_label"] == "starter:v1"

    nobody = call("platform", {"action": "results", "player": "ply_other", "league": LEAGUE})
    assert nobody["leagues"][0]["memberships"] == []
    assert nobody["leagues"][0]["standings"] == []


def test_episodes_by_policy_version_by_league_and_one_episode(call, platform):
    version = upload(platform)
    by_policy = call("platform", {"action": "episodes", "policy_version": version.id})
    assert len(by_policy["episodes"]) == fake.POLICY_EPISODE_SEEDS
    assert by_policy["episodes"][0]["policy_version_ids"][0] == version.id

    empty_league = call("platform", {"action": "episodes", "league": LEAGUE})
    assert empty_league["episodes"] == [] and empty_league["policy_versions"] == []

    platform.submit(league_id=LEAGUE, policy_version_id=version.id)
    by_league = call("platform", {"action": "episodes", "league": LEAGUE})
    assert by_league["policy_versions"] == [version.id]
    assert by_league["truncated"] is False
    assert {e["id"] for e in by_league["episodes"]} == {e["id"] for e in by_policy["episodes"]}

    episode_id = by_policy["episodes"][3]["id"]
    one = call("platform", {"action": "episode", "episode": episode_id}, role="researcher")
    assert one["episode"]["id"] == episode_id
    assert one["episode"]["seed"] == by_policy["episodes"][3]["seed"]

    neither = call("platform", {"action": "episodes"})
    assert neither["ok"] is False and "policy_version or a league" in neither["error"]


def test_replay_writes_the_file_and_a_pin_once(call, platform, ctx_factory):
    version = upload(platform)
    episode_id = platform.episodes_for_policy(version.id).entries[0]["id"]
    result = call("platform", {"action": "replay", "episode": episode_id}, role="researcher")
    assert result["ok"] is True
    path = documents(ctx_factory) / "replays" / f"{episode_id}.replay"
    assert result["path"] == str(path)
    assert json.loads(path.read_bytes())["id"] == episode_id
    assert result["existing"] is False
    pin = pins.find(ctx_factory().store, "replay", f"episode:{episode_id}")
    assert pin is not None
    assert pin["download"]["path"] == str(path)
    assert pin["download"]["digest"] == hashlib.sha256(path.read_bytes()).hexdigest()

    again = call("platform", {"action": "replay", "episode": episode_id})
    assert again["existing"] is True and again["pin"]["id"] == pin["id"]

    elsewhere = call(
        "platform",
        {"action": "replay", "episode": platform.episodes_for_policy(version.id).entries[1]["id"],
         "dest": "scratch/"},
    )  # fmt: skip
    assert elsewhere["path"].startswith(str(documents(ctx_factory) / "scratch"))


# --- credits, champion, Coworld ---------------------------------------------------------------


def test_credits_and_champion(call, platform):
    credits = call("platform", {"action": "credits"}, role="researcher")
    assert credits["credits"]["balance"] == fake.DEFAULT_CREDITS
    assert credits["credits"]["recent_requests"] == []

    assert call("platform", {"action": "champion", "league": LEAGUE})["champion"] is None
    version = competing(platform)
    champion = call("platform", {"action": "champion", "league": LEAGUE})["champion"]
    assert champion["policy_version"]["id"] == version.id
    assert champion["is_champion"] is True


def test_coworld_manifest_summary(call, platform):
    platform.configure(source_url="https://example.test/fakegame.git", source_commit="a" * 40)
    result = call("platform", {"action": "coworld", "coworld": fake.COWORLD_ID})
    assert result["coworld"] == {
        "id": fake.COWORLD_ID,
        "name": fake.COWORLD_NAME,
        "version": fake.COWORLD_VERSION,
        "player_runtime": "game-hosted",
        "source_url": "https://example.test/fakegame.git",
        "commit": "a" * 40,
    }


# --- forum and wiki ---------------------------------------------------------------------------


def test_forum_reads(call, platform):
    post = platform.forum_post(
        title="Opening book", body="Greedy wins early.", idempotency_key="k1", slug=LEAGUE
    )
    other = platform.forum_post(
        title="Other", body="Nothing here.", idempotency_key="k2", slug=LEAGUE
    )
    listed = call("platform", {"action": "forum_list"})
    assert listed["slug"] == LEAGUE  # from the league's forum_markdown_url
    assert {p["id"] for p in listed["posts"]} == {post["id"], other["id"]}
    newest = call("platform", {"action": "forum_list", "slug": LEAGUE, "sort": "new", "limit": 1})
    assert len(newest["posts"]) == 1

    read = call("platform", {"action": "forum_read", "post": post["id"]}, role="researcher")
    assert read["markdown"].startswith("# Opening book")
    found = call("platform", {"action": "forum_search", "query": "greedy"})
    assert [hit["post"]["id"] for hit in found["hits"]] == [post["id"]]


def test_wiki_reads(call, platform):
    edit = platform.wiki_edit(
        "w", "openings", title="Openings", body="Greedy first.", base_revision_id=None,
        idempotency_key="e1",
    )  # fmt: skip
    page = call("platform", {"action": "wiki_read", "wiki": "w", "page": "openings"})["page"]
    assert page["title"] == "Openings" and page["revision_id"] == edit["revision_id"]
    hits = call("platform", {"action": "wiki_search", "wiki": "w", "query": "greedy"})["hits"]
    assert [hit["page"] for hit in hits] == ["openings"]
    history = call("platform", {"action": "wiki_history", "page_id": edit["page_id"]})
    assert [rev["revision_id"] for rev in history["revisions"]] == [edit["revision_id"]]


# --- platform errors: retries, then the wait --------------------------------------------------


def test_platform_down_refuses_with_route_status_and_no_wait_for_a_chat(
    call, workspace_env, no_backoff
):
    workspace_env["IDE_FAKE_FAULTS"] = "platform=down"
    result = call("platform", {"action": "credits"})
    assert result["ok"] is False
    assert "down" in result["error"]
    assert result["context"]["status"] == 503
    assert result["context"]["route"] == "credits"
    assert result["context"]["retryable"] is True
    assert result["context"]["waiting"] is False


def test_platform_down_retries_three_times(call, workspace_env, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(wait, "sleep", slept.append)
    workspace_env["IDE_FAKE_FAULTS"] = "platform=down"
    assert call("platform", {"action": "leagues"})["ok"] is False
    assert slept == [1.0, 4.0]


def test_platform_down_puts_the_held_task_on_wait(call, ctx_factory, workspace_env, no_backoff):
    tasks = pytest.importorskip("research_core.store.tasks")
    harness = Actor.harness()
    ctx = ctx_factory("researcher", "s-task")
    goal = goals.create(
        ctx.store, harness, title="Win", end_state="Rank first", reason="the user asked"
    )
    task = tasks.create(
        ctx.store, harness, type="research", title="Survey", description="Read.", goal_id=goal["id"]
    )
    holder = Actor.session("s-task")
    tasks.claim(ctx.store, holder, task["id"], run_id="r1", worktree="/w/task-1", branch="task/1")
    sessions.register(
        ctx.wstore, holder, session_id="s-task", profile="researcher", kind="task",
        coworld_id=fake.COWORLD_ID, task_id=1,
    )  # fmt: skip

    from research_core.coordinator import Coordinator

    sessions.set_run(ctx.wstore, holder, "s-task", "r1")
    Coordinator.adopt_existing_workers(ctx.workspace, ctx.wstore)
    workspace_env["IDE_FAKE_FAULTS"] = "platform=down"
    result = call("platform", {"action": "credits"}, role="researcher", session_id="s-task")
    assert result["ok"] is False
    assert result["context"]["waiting"] is True
    assert "end your run" in result["context"]["next"]
    assert result["context"]["task"] == task["id"]
    waiting_on = tasks.read(ctx.store, task["id"])["waiting_on"]
    assert waiting_on["kind"] == "platform"
    assert waiting_on["detail"] == "credits 503"
    assert waiting_on["ref"] is None
    assert ctx.store.events_after(0)[-1].kind == "task.waiting"

    # Another Session (not the holder) gets the refusal without a wait.
    other = call("platform", {"action": "credits"}, role="researcher", session_id="s-other")
    assert other["context"]["waiting"] is False


@pytest.mark.parametrize("kind", ["transition_pending", "transition_refused"])
def test_platform_failure_does_not_claim_wait_when_command_is_unapplied(
    tmp_path, monkeypatch, kind
):
    from research_core.platform.errors import PlatformError
    from research_core.tools import task
    from research_core.tools.base import ToolContext
    from tests.test_coordinator_transitions import assigned
    from tests.watcher._support import make_ctx

    tick = make_ctx(tmp_path)
    row = assigned(tick)
    ctx = ToolContext(
        role="researcher", actor=Actor.session(row["session_id"]), workspace=tick.ws, env=tick.env
    )
    ctx.store, ctx.wstore = tick.store, tick.wstore
    result = {
        "kind": kind,
        "command_id": "wait",
        "ok": False,
        "error": "Unavailable",
        "transition_state": kind.removeprefix("transition_"),
    }
    monkeypatch.setattr(task, "dispatch", lambda call: result)
    error = PlatformError("Platform unavailable", status=503)
    context = wait._wait_on_platform(ctx, "credits", error)
    assert context == {"waiting": False, "transition": result}


def test_refused_wait_preserves_original_platform_error(tmp_path, monkeypatch):
    from research_core.platform.errors import PlatformError
    from research_core.tools import task
    from research_core.tools.base import ToolContext, ToolError
    from tests.test_coordinator_transitions import assigned
    from tests.watcher._support import make_ctx

    tick = make_ctx(tmp_path)
    row = assigned(tick)
    ctx = ToolContext(
        role="researcher", actor=Actor.session(row["session_id"]), workspace=tick.ws, env=tick.env
    )
    ctx.store, ctx.wstore = tick.store, tick.wstore

    def refuse(call):
        raise ToolError("Worker authority retired")

    monkeypatch.setattr(task, "dispatch", refuse)
    error = wait.tool_error(ctx, "credits", PlatformError("Platform unavailable", status=503))
    assert error.problems == ["Platform unavailable"]
    assert error.context["status"] == 503 and error.context["route"] == "credits"
    assert error.context["waiting"] is False
    assert error.context["wait_skipped"] == "Worker authority retired"
