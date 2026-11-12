import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from research_core import platform as platform_pkg
from research_core.platform import fake
from research_core.platform.errors import (
    CreditsRefused,
    IdentityUnavailable,
    NotAvailable,
    PlatformError,
)
from research_core.platform.fake import FakePlatform
from research_core.platform.protocol import with_retries
from tests.fixtures.fakegame_repo import SKILL_NAMES, STARTER_POLICY, make_fakegame_repo

LEAGUE = fake.LEAGUE_ID


def policy_bytes(aggression: int, patience: int) -> bytes:
    return json.dumps({"parameters": {"aggression": aggression, "patience": patience}}).encode()


def make(tmp_path: Path, faults: str = "", **env: str) -> FakePlatform:
    return FakePlatform(
        tmp_path / ".state" / "fake_platform.json", {"IDE_FAKE_FAULTS": faults, **env}
    )


def xp_body(policy_version_id: str, seed: int | None, *, title: str | None = None) -> dict:
    body = {
        "coworld_id": fake.COWORLD_ID,
        "roster": [
            {"player": {"policy_ref": policy_version_id}, "slot": 0},
            {"player": {"policy_ref": "pv_house"}, "slot": 1},
        ],
        "num_episodes": 2,
    }
    if seed is not None:
        body["game_config_overrides"] = {"seed": seed}
    if title is not None:
        body["title"] = title
    return body


def download_results(platform: FakePlatform, xp_id: str, dest: Path) -> dict:
    platform.xp_get(xp_id)
    (path,) = platform.xp_download(xp_id, include=["results"], dest=dest)
    return json.loads(path.read_text())


def front_matter(text: str) -> dict:
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


# --- state and ids ----------------------------------------------------------------------------


def test_state_persists_across_processes(tmp_path):
    platform = make(tmp_path)
    uploaded = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    script = (
        "import json, sys\n"
        "from research_core.platform.fake import FakePlatform\n"
        "p = FakePlatform(sys.argv[1], {})\n"
        "print(json.dumps({'labels': [v.label for v in p.policy_versions()],"
        " 'ids': [v.id for v in p.policy_versions()], 'counts': p.snapshot()['counts']}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(platform.state_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    seen = json.loads(result.stdout)
    assert seen == {
        "labels": ["starter:v1"],
        "ids": [uploaded.id],
        "counts": {"upload": 1, "submit": 0, "xp_create": 0},
    }


def test_ids_are_deterministic_across_state_files(tmp_path):
    ids = []
    for folder in ("a", "b"):
        platform = make(tmp_path / folder)
        version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
        submission = platform.submit(league_id=LEAGUE, policy_version_id=version.id)
        request = platform.xp_create(xp_body(version.id, 1, title="ide:X-1:1:a"))
        ids.append((version.id, submission.id, submission.membership_id, request.id))
    assert ids[0] == ids[1]
    assert ids[0][0].startswith("pv_") and ids[0][1].startswith("sub_")
    assert ids[0][3].startswith("xreq_")


def test_from_env_selects_the_fake_under_the_workspace_state(tmp_path):
    platform = platform_pkg.from_env({"IDE_PLATFORM": "fake", "IDE_WORKSPACE": str(tmp_path)})
    assert isinstance(platform, FakePlatform)
    assert platform.state_path == tmp_path / ".state" / "fake_platform.json"


# --- policies -----------------------------------------------------------------------------------


def test_upload_returns_the_sha256_and_stores_the_bytes(tmp_path):
    platform = make(tmp_path)
    content = policy_bytes(3, 5)
    first = platform.upload_file_policy(name="starter", content=content, tags={"task": "T-1"})
    second = platform.upload_file_policy(name="starter", content=policy_bytes(4, 5))
    other = platform.upload_file_policy(name="other", content=b"not json")
    assert first.content_hash == hashlib.sha256(content).hexdigest()
    assert (first.version, second.version, other.version) == (1, 2, 1)
    assert first.player_id == fake.DEFAULT_PLAYER_ID and first.label == "starter:v1"
    assert platform.policy_file(first.id) == content
    assert [v.id for v in platform.policy_versions(name="starter")] == [first.id, second.id]
    assert [v.id for v in platform.policy_versions(mine=True, name="other")] == [other.id]
    assert platform.snapshot()["counts"]["upload"] == 3
    with pytest.raises(PlatformError) as info:
        platform.policy_file("pv_missing")
    assert info.value.status == 404 and not info.value.retryable


def test_lookup_policy_upload_matches_the_saved_inputs(tmp_path):
    platform = make(tmp_path)
    content = policy_bytes(3, 5)
    artifact = tmp_path / "policy.json"
    artifact.write_bytes(content)
    inputs = platform.upload_inputs(name="starter", artifact=artifact)
    assert inputs == {
        "name": "starter",
        "player_id": fake.DEFAULT_PLAYER_ID,
        "file_hash": hashlib.sha256(content).hexdigest(),
        "file_size": len(content),
        "attributes": {},
    }
    assert platform.lookup_policy_upload(inputs) is None
    uploaded = platform.upload_file_policy(name="starter", content=content)
    assert platform.lookup_policy_upload(inputs) == uploaded
    assert platform.lookup_policy_upload({**inputs, "name": "other"}) is None
    assert platform.lookup_policy_upload({**inputs, "file_size": 1}) is None
    assert platform.lookup_policy_upload({**inputs, "player_id": "ply_other"}) is None
    # Faults: no match while the version exists, errors and a client without the route.
    assert make(tmp_path, "lookup=not_found").lookup_policy_upload(inputs) is None
    for fault, status in (("lookup=503", 503), ("lookup=403", 403), ("platform=down", 503)):
        with pytest.raises(PlatformError) as info:
            make(tmp_path, fault).lookup_policy_upload(inputs)
        assert info.value.status == status
    with pytest.raises(NotAvailable):
        make(tmp_path, "lookup=unavailable").lookup_policy_upload(inputs)
    assert platform.snapshot()["counts"]["upload"] == 1


# --- submissions, memberships, champion -----------------------------------------------------------


def test_submission_is_placed_after_polls_and_crowns_the_champion(tmp_path):
    platform = make(tmp_path)
    platform.configure(polls_to_complete=2)
    first = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    second = platform.upload_file_policy(name="starter", content=policy_bytes(3, 9))
    submission = platform.submit(league_id=LEAGUE, policy_version_id=first.id)
    assert submission.status == "pending" and submission.membership_id

    (pending,) = platform.submissions(mine=True)
    assert pending.status == "pending"  # one poll of two
    (placed,) = platform.submissions(policy_version_id=first.id)
    assert placed.status == "placed"
    (membership,) = platform.memberships(mine=True)
    assert (membership.status, membership.is_champion) == ("competing", True)
    assert membership.policy_version.id == first.id and membership.league_id == LEAGUE

    platform.submit(league_id=LEAGUE, policy_version_id=second.id, auto_champion="never")
    platform.memberships(mine=True)  # poll one
    by_policy = {m.policy_version.id: m for m in platform.memberships(mine=True, active_only=True)}
    assert by_policy[second.id].status == "competing" and not by_policy[second.id].is_champion
    assert by_policy[first.id].is_champion
    champions = platform.memberships(mine=True, champions_only=True)
    assert [m.policy_version.id for m in champions] == [first.id]

    platform.set_champion(by_policy[second.id].id)
    (champion,) = platform.memberships(league_id=LEAGUE, champions_only=True)
    assert champion.policy_version.id == second.id
    assert platform.snapshot()["counts"]["submit"] == 2


def test_lineage_crowns_only_a_later_version_of_the_champions_policy(tmp_path):
    """As ``should_auto_promote_membership_to_champion`` in metta: same policy, earlier version."""
    platform = make(tmp_path)
    first = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    second = platform.upload_file_policy(name="starter", content=policy_bytes(3, 9))
    third = platform.upload_file_policy(name="starter", content=policy_bytes(3, 11))
    other = platform.upload_file_policy(name="other", content=policy_bytes(3, 13))

    def champion_after(policy, auto_champion):
        platform.submit(league_id=LEAGUE, policy_version_id=policy.id, auto_champion=auto_champion)
        platform.memberships(mine=True)
        platform.memberships(mine=True)
        (champion,) = platform.memberships(mine=True, champions_only=True)
        return champion.policy_version.id

    assert champion_after(second, "always") == second.id
    assert champion_after(first, "lineage") == second.id  # an earlier version
    assert champion_after(other, "lineage") == second.id  # another policy
    assert champion_after(third, "lineage") == third.id


def test_submit_refuses_unknown_policy_and_league(tmp_path):
    platform = make(tmp_path)
    version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    with pytest.raises(PlatformError) as info:
        platform.submit(league_id=LEAGUE, policy_version_id="pv_missing")
    assert info.value.status == 404
    with pytest.raises(PlatformError):
        platform.submit(league_id="league_other", policy_version_id=version.id)
    assert platform.snapshot()["counts"]["submit"] == 0


def test_champion_fault_flips_is_champion_on_the_next_read(tmp_path):
    platform = make(tmp_path)
    first = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    second = platform.upload_file_policy(name="starter", content=policy_bytes(3, 9))
    platform.submit(league_id=LEAGUE, policy_version_id=first.id)
    (champion,) = platform.memberships(mine=True, champions_only=True)
    assert champion.policy_version.id == first.id

    external = make(tmp_path, faults=f"champion={second.id}")
    by_policy = {m.policy_version.id: m for m in external.memberships(mine=True)}
    assert by_policy[second.id].is_champion and by_policy[second.id].status == "competing"
    assert not by_policy[first.id].is_champion
    # The flip happens once: a later set_champion is not undone by the fault.
    external.set_champion(by_policy[first.id].id)
    (champion,) = external.memberships(mine=True, champions_only=True)
    assert champion.policy_version.id == first.id


# --- XP -------------------------------------------------------------------------------------------


def test_xp_request_runs_through_pending_running_completed(tmp_path):
    platform = make(tmp_path)
    platform.configure(polls_to_complete=3)
    version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    request = platform.xp_create(xp_body(version.id, 11, title="ide:X-1:11:candidate"))
    assert (request.status, request.episode_count, request.title) == (
        "pending",
        2,
        "ide:X-1:11:candidate",
    )
    assert request.coworld_id == fake.COWORLD_ID
    with pytest.raises(PlatformError) as info:
        platform.xp_download(request.id, include=["results"], dest=tmp_path / "early")
    assert info.value.status == 409

    assert platform.xp_get(request.id).status == "running"
    assert platform.xp_get(request.id).status == "running"
    done = platform.xp_get(request.id)
    assert (done.status, done.completed_count, done.terminal) == ("completed", 2, True)
    (listed,) = platform.xp_list(mine=True)
    assert listed.title == "ide:X-1:11:candidate" and listed.status == "completed"
    episodes = platform.xp_episodes(request.id)
    assert [e["index"] for e in episodes] == [0, 1]
    assert all(e["status"] == "completed" and e["seed"] == 11 for e in episodes)

    paths = platform.xp_download(request.id, include=["results", "replay"], dest=tmp_path / "dl")
    assert [p.name for p in paths] == ["results.json", "replay.json"]
    results = json.loads(paths[0].read_text())
    assert results["seed"] == 11 and results["request_id"] == request.id
    # Every seat is scored, the opponent's too, as a real results artifact does.
    assert [(e["episode"], e["seat"]) for e in results["episodes"]] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]
    assert {"score", "errors", "seed"} <= set(results["episodes"][0])
    assert json.loads(paths[1].read_text())["ticks"]
    credits = platform.credits()
    assert (
        credits.balance == fake.DEFAULT_CREDITS - 1 and credits.recent_requests[0]["episodes"] == 2
    )
    assert platform.snapshot()["counts"]["xp_create"] == 1
    with pytest.raises(PlatformError) as info:
        platform.xp_get("xreq_missing")
    assert info.value.status == 404


def test_seeded_lever_same_scores_fewer_errors_with_higher_patience(tmp_path):
    platform = make(tmp_path)
    low = platform.upload_file_policy(name="low", content=policy_bytes(3, 5))
    high = platform.upload_file_policy(name="high", content=policy_bytes(3, 9))
    gaps = []
    for seed in range(30):
        rows = {}
        for arm, version in (("low", low), ("high", high)):
            request = platform.xp_create(xp_body(version.id, seed, title=f"ide:X-1:{seed}:{arm}"))
            results = download_results(platform, request.id, tmp_path / "dl" / arm / str(seed))
            rows[arm] = results["episodes"]
        assert [e["score"] for e in rows["low"]] == [e["score"] for e in rows["high"]]
        gaps.append(
            sum(e["errors"] for e in rows["low"]) / 2 - sum(e["errors"] for e in rows["high"]) / 2
        )
    assert sum(gaps) / len(gaps) > 2


def test_seeded_results_repeat_and_unseeded_results_key_on_the_request(tmp_path):
    platform = make(tmp_path)
    version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    a = platform.xp_create(xp_body(version.id, 5, title="a"))
    b = platform.xp_create(xp_body(version.id, 5, title="b"))
    assert (
        download_results(platform, a.id, tmp_path / "a")["episodes"]
        == download_results(platform, b.id, tmp_path / "b")["episodes"]
    )
    c = platform.xp_create(xp_body(version.id, None, title="c"))
    d = platform.xp_create(xp_body(version.id, None, title="d"))
    c_rows = download_results(platform, c.id, tmp_path / "c")
    d_rows = download_results(platform, d.id, tmp_path / "d")
    assert "seed" not in c_rows and c_rows["episodes"] != d_rows["episodes"]


# --- leagues, episodes, standings ----------------------------------------------------------------


def test_leagues_divisions_episodes_and_standings(tmp_path):
    platform = make(tmp_path)
    (league,) = platform.leagues()
    assert league.id == LEAGUE and league.coworld_id == fake.COWORLD_ID
    assert platform.leagues(game_id="game_other") == []
    assert platform.league(LEAGUE).name == fake.LEAGUE_NAME
    assert platform.divisions(LEAGUE)[0]["id"] == fake.DIVISION_ID
    with pytest.raises(PlatformError):
        platform.league("league_other")

    episodes = platform.league_episodes(60)
    seeds = {}
    for episode in episodes:
        seeds.setdefault(episode["seed"], []).append(episode["policy_version_ids"][0])
    assert len(seeds) == 20 and all(len(v) == 3 for v in seeds.values())
    assert all(e["policy_version_ids"][1] == fake.LEAGUE_HOUSE for e in episodes)

    version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    mine = platform.episodes_for_policy(version.id).entries
    assert len(mine) == 10 and all(e["policy_version_ids"][0] == version.id for e in mine)
    assert platform.episode(mine[0]["id"])["seed"] == mine[0]["seed"]
    results = json.loads(platform.episode_artifact(mine[0]["id"], "results"))
    assert results["episodes"][0]["seat"] == 0 and "score" in results["episodes"][0]
    assert json.loads(platform.episode_artifact(mine[0]["id"], "replay"))["ticks"]
    with pytest.raises(PlatformError) as info:
        platform.episode_artifact(mine[0]["id"], "events")
    assert info.value.status == 404

    assert [row["player_id"] for row in platform.standings(fake.DIVISION_ID)] == [
        "ply_house_a",
        "ply_house_b",
    ]
    platform.submit(league_id=LEAGUE, policy_version_id=version.id)
    rows = platform.standings(fake.DIVISION_ID)
    ours = next(row for row in rows if row["player_id"] == fake.DEFAULT_PLAYER_ID)
    assert ours["rank"] == 2 and ours["policy_label"] == "starter:v1"


# --- identities -----------------------------------------------------------------------------------


def test_whoami_for_player_and_user(tmp_path):
    platform = make(tmp_path, SOFTMAX_PLAYER_ID="ply_123")
    who = platform.whoami()
    assert (who.kind, who.subject_id) == ("player", "ply_123") and who.expires_at
    with pytest.raises(IdentityUnavailable) as info:
        platform.whoami("user")
    assert "User identity not provisioned" in str(info.value)
    with pytest.raises(IdentityUnavailable):
        platform.forum_post(title="t", body="b", idempotency_key="k", identity="user")

    with_user = make(tmp_path, SOFTMAX_USER_CREDENTIAL="user-token")
    assert with_user.whoami("user").kind == "user"
    assert with_user.policy_versions(identity="user") == []
    agent = make(tmp_path, SOFTMAX_PLAYER_CREDENTIAL="cga_abc")
    assert agent.whoami().expires_at is None


# --- faults ---------------------------------------------------------------------------------------


def test_fault_credits_and_credits_refuse(tmp_path):
    assert make(tmp_path, "credits=5").credits().balance == 5.0
    platform = make(tmp_path, "credits_refuse=1")
    version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    with pytest.raises(CreditsRefused) as info:
        platform.xp_create(xp_body(version.id, 1))
    assert info.value.balance == 100.0 and info.value.held == 0.0 and info.value.next_refill_at
    assert info.value.status == 402 and not info.value.retryable
    assert platform.snapshot()["counts"]["xp_create"] == 0

    broke = make(tmp_path)
    broke.configure(credits=0)
    with pytest.raises(CreditsRefused):
        broke.xp_create(xp_body(version.id, 1))


def test_fault_identity_user_rejected_and_expired(tmp_path):
    as_user = make(tmp_path, "identity=user")
    assert as_user.whoami().kind == "user"
    with pytest.raises(PlatformError) as info:
        as_user.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    assert info.value.status == 403 and not info.value.retryable

    rejected = make(tmp_path, "identity=rejected")
    with pytest.raises(PlatformError) as info:
        rejected.whoami()
    assert info.value.status == 401
    with pytest.raises(PlatformError):
        rejected.submit(league_id=LEAGUE, policy_version_id="pv_x")

    expired = make(tmp_path, "expires_in=0")
    with pytest.raises(PlatformError) as info:
        expired.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    assert info.value.status == 401 and "expired" in info.value.detail
    assert make(tmp_path).snapshot()["counts"]["upload"] == 0


def test_fault_fail_after_raises_after_persisting(tmp_path):
    platform = make(tmp_path, "fail_after=upload")
    with pytest.raises(RuntimeError, match="injected fault after upload"):
        platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    clean = make(tmp_path)
    assert [v.label for v in clean.policy_versions()] == ["starter:v1"]
    assert clean.snapshot()["counts"]["upload"] == 1

    version = clean.policy_versions()[0]
    failing = make(tmp_path, "fail_after=xp_create")
    with pytest.raises(RuntimeError):
        failing.xp_create(xp_body(version.id, 1, title="ide:X-1:1:candidate"))
    assert [r.title for r in clean.xp_list()] == ["ide:X-1:1:candidate"]


def test_fault_xp_limit(tmp_path):
    platform = make(tmp_path, "xp_limit=1")
    version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    platform.xp_create(xp_body(version.id, 1))
    with pytest.raises(PlatformError) as info:
        platform.xp_create(xp_body(version.id, 2))
    assert info.value.status == 429 and info.value.retryable


def test_fault_upload_503_is_retryable_and_counted_once(tmp_path):
    platform = make(tmp_path, "upload=503:2")
    with pytest.raises(PlatformError) as info:
        platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    assert info.value.status == 503 and info.value.retryable
    slept = []
    version = with_retries(
        lambda: platform.upload_file_policy(name="starter", content=policy_bytes(3, 5)),
        sleep=slept.append,
    )
    assert version.version == 1 and slept == [1.0]  # one more failure, then success
    assert platform.snapshot()["counts"]["upload"] == 1
    assert make(tmp_path, "upload=503").upload_file_policy(name="x", content=b"y").version == 1


def test_fault_submit_409(tmp_path):
    platform = make(tmp_path, "submit=409")
    version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    with pytest.raises(PlatformError) as info:
        platform.submit(league_id=LEAGUE, policy_version_id=version.id)
    assert info.value.status == 409 and not info.value.retryable
    assert platform.snapshot()["counts"]["submit"] == 0


def test_fault_platform_down(tmp_path):
    platform = make(tmp_path, "platform=down")
    for call in (platform.whoami, platform.credits, platform.leagues):
        with pytest.raises(PlatformError) as info:
            call()
        assert info.value.status == 503 and info.value.retryable
    assert platform.configure(polls_to_complete=2)["polls_to_complete"] == 2  # helpers still work


# --- forum and wiki -------------------------------------------------------------------------------


def test_forum_posts_comments_votes_and_idempotency(tmp_path):
    platform = make(tmp_path)
    forum = "league_fake"
    post = platform.forum_post(
        title="Opening book", body="Patience wins.", idempotency_key="p1", slug=forum
    )
    again = platform.forum_post(title="other", body="other", idempotency_key="p1", slug=forum)
    assert again["id"] == post["id"] and post["identity"] == "player"
    other = platform.forum_post(
        title="Noise", body="Board terms.", idempotency_key="p2", slug=forum
    )
    comment = platform.forum_comment(post["id"], body="Agreed.", idempotency_key="c1")
    assert comment["post_id"] == post["id"]
    vote = platform.forum_vote(post["id"], 1)
    assert vote["score"] == 1 and platform.forum_vote(post["id"], 1)["score"] == 1
    assert (
        platform.forum_vote(post["id"], 0, comment_id=comment["id"])["comment_id"] == comment["id"]
    )

    listed = platform.forum_list(forum)
    assert [p["id"] for p in listed] == [post["id"], other["id"]]  # by score, then date
    assert [p["id"] for p in platform.forum_list(forum, sort="new")] == [other["id"], post["id"]]
    assert platform.forum_list("another") == []
    assert [hit["post"]["id"] for hit in platform.forum_search("patience")] == [post["id"]]
    text = platform.forum_read(post["id"])
    assert text.startswith("# Opening book") and "- (Autoresearch) Agreed." in text
    with pytest.raises(PlatformError) as info:
        platform.forum_comment("post_missing", body="x", idempotency_key="c2")
    assert info.value.status == 404


def test_wiki_compare_and_swap(tmp_path):
    platform = make(tmp_path, SOFTMAX_USER_CREDENTIAL="user-token")
    with pytest.raises(PlatformError) as info:
        platform.wiki_read("fake", "home")
    assert info.value.status == 404

    created = platform.wiki_edit(
        "fake", "home", title="Home", body="v1", base_revision_id=None, idempotency_key="w1"
    )
    assert created["conflict"] is False and created["revision_id"]
    page = platform.wiki_read("fake", "home")
    assert (page["body"], page["revision_id"]) == ("v1", created["revision_id"])

    stale = platform.wiki_edit(
        "fake", "home", title="Home", body="v2", base_revision_id="wrev_stale", idempotency_key="w2"
    )
    assert stale == {
        "conflict": True,
        "page_id": page["page_id"],
        "current_revision_id": created["revision_id"],
        "current_body": "v1",
    }
    edited = platform.wiki_edit(
        "fake",
        "home",
        title="Home",
        body="v2",
        base_revision_id=created["revision_id"],
        idempotency_key="w3",
        note="second",
        identity="user",
    )
    assert edited["conflict"] is False and edited["identity"] == "user"
    assert (
        platform.wiki_edit(
            "fake", "home", title="x", body="x", base_revision_id="y", idempotency_key="w3"
        )
        == edited
    )
    history = platform.wiki_history(page["page_id"])
    assert [h["body"] for h in history] == ["v2", "v1"] and history[0]["note"] == "second"
    assert [p["page"] for p in platform.wiki_search("fake", "V2")] == ["home"]


# --- the generic request --------------------------------------------------------------------------


def test_request_routes_and_unknown_paths(tmp_path):
    platform = make(tmp_path)
    version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    submission = platform.submit(league_id=LEAGUE, policy_version_id=version.id)
    assert platform.request("GET", f"/v2/policy-versions/{version.id}")["name"] == "starter"
    assert "content_b64" not in platform.request("GET", f"/v2/policy-versions/{version.id}")
    assert platform.request("GET", f"/observatory/v2/league-submissions/{submission.id}")["id"]
    crowned = platform.request(
        "POST", f"/v2/league-policy-memberships/{submission.membership_id}/champion"
    )
    assert crowned["is_champion"] is True and crowned["identity"] == "player"
    assert platform.request("GET", "/whoami")["subject_type"] == "player"
    assert platform.request("GET", "/usage/me/credits")["balance"] == 100.0
    assert platform.request("GET", f"/v2/coworlds/{fake.COWORLD_ID}")["name"] == "fakegame"
    for method, path in (("GET", "/v2/nothing/here"), ("POST", "/v2/leagues/x/grade")):
        with pytest.raises(PlatformError) as info:
            platform.request(method, path)
        assert info.value.status == 404 and info.value.route == f"{method} {path}"
        assert not info.value.retryable


# --- Coworld manifest -----------------------------------------------------------------------------


def test_coworld_manifest_from_env_and_configure(tmp_path):
    platform = make(tmp_path)
    assert platform.coworld_manifest(fake.COWORLD_ID)["manifest"]["game"]["runnable"] == {
        "source_url": None,
        "commit": None,
    }
    platform.configure(source_url="file:///tmp/fakegame_repo", source_commit="abc123")
    manifest = platform.coworld_manifest("cow_fake")
    assert manifest["id"] == "cow_fake" and manifest["name"] == "fakegame"
    assert manifest["version"] == "1"
    assert manifest["manifest"]["game"]["player_runtime"] == "game-hosted"
    assert manifest["manifest"]["game"]["runnable"] == {
        "source_url": "file:///tmp/fakegame_repo",
        "commit": "abc123",
    }
    from_env = make(tmp_path, IDE_FAKE_SOURCE_URL="file:///elsewhere")
    assert from_env.coworld_manifest("cow_fake")["manifest"]["game"]["runnable"]["source_url"] == (
        "file:///elsewhere"
    )
    with pytest.raises(PlatformError) as info:
        platform.coworld_manifest("cow_other")
    assert info.value.status == 404


# --- the fakegame fixture repository --------------------------------------------------------------


def test_fakegame_repo_is_a_committed_mixin(tmp_path):
    repo, sha = make_fakegame_repo(tmp_path)
    assert repo.is_dir() and len(sha) == 40
    git = ["git", "-C", str(repo)]
    assert (
        subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        == sha
    )
    assert (
        subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True).stdout == ""
    )

    mixin = front_matter((repo / "MIXIN.md").read_text())
    assert mixin["coworld"] == "cow_fake" and mixin["coworld_version"] == "1"
    assert set(mixin["commands"]) == {"build", "check", "upload", "evaluate"}
    assert mixin["artifact"] == {"kind": "file", "path": "build/policy.json"}
    assert [m["name"] for m in mixin["metrics"]] == ["score", "errors"]
    assert mixin["toolchains"] == [] and mixin["evidence_types"] and mixin["cost_per_episode"] == 1
    assert (repo / "AGENTS.md").is_file() and (repo / "docs" / "mechanics.md").is_file()
    assert json.loads((repo / "policy.json").read_text()) == STARTER_POLICY
    for name in SKILL_NAMES:
        skill = front_matter((repo / "skills" / name / "SKILL.md").read_text())
        assert skill["name"] == name and len(skill["description"]) <= 60

    claims = sorted((repo / "seed" / "kb" / "claims").glob("C-*.md"))
    lessons = sorted((repo / "seed" / "lessons").glob("L-*.md"))
    citations = json.loads((repo / "seed" / "evidence" / "source-citations.json").read_text())
    assert len(claims) == 2 and len(lessons) == 2 and len(citations) == 2
    mechanics = (repo / "docs" / "mechanics.md").read_text()
    for key, citation in citations.items():
        assert (
            key == f"{citation['path']}:{citation['start']}-{citation['end']}@{citation['commit']}"
        )
        lines = mechanics.splitlines(keepends=True)[citation["start"] - 1 : citation["end"]]
        assert "".join(lines) == citation["text"]
        assert hashlib.sha256(citation["text"].encode()).hexdigest() == citation["sha256"]
    for claim in claims:
        meta = front_matter(claim.read_text())
        assert meta["id"] == f"C-{claim.name[2:28]}" and meta["evidence"][0]["ref"] in citations
        assert meta["status"] == "verified" and meta["kind"] == "mechanism"
    for lesson in lessons:
        meta = front_matter(lesson.read_text())
        assert meta["status"] == "tentative" and meta["scar"][0] in citations


def test_fakegame_build_check_and_evaluate(tmp_path):
    repo, _ = make_fakegame_repo(tmp_path)
    built = subprocess.run(["sh", "tools/build.sh"], cwd=repo, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    assert json.loads((repo / "build" / "policy.json").read_text()) == STARTER_POLICY
    checked = subprocess.run(
        ["python3", "tools/check.py"], cwd=repo, capture_output=True, text=True
    )
    assert checked.returncode == 0 and json.loads(checked.stdout)["parameters"]["patience"] == 5
    uploaded = subprocess.run(["sh", "tools/upload.sh"], cwd=repo, capture_output=True, text=True)
    artifact_hash = hashlib.sha256((repo / "build" / "policy.json").read_bytes()).hexdigest()
    assert json.loads(uploaded.stdout)["sha256"] == artifact_hash

    # The same commands from a code repository where the mixin lives under mixin/.
    code = tmp_path / "code"
    (code / "mixin").mkdir(parents=True)
    subprocess.run(["cp", "-R", f"{repo}/.", str(code / "mixin")], check=True)
    (code / "policy.json").write_text(json.dumps({"parameters": {"aggression": 4, "patience": 9}}))
    built = subprocess.run(["sh", "mixin/tools/build.sh"], cwd=code, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    assert json.loads((code / "build" / "policy.json").read_text())["parameters"]["patience"] == 9
    (code / "policy.json").write_text('{"parameters": {"aggression": "high"}}')
    bad = subprocess.run(
        ["python3", "mixin/tools/check.py"], cwd=code, capture_output=True, text=True
    )
    assert bad.returncode == 1 and "patience" in bad.stderr and "not a number" in bad.stderr

    platform = make(tmp_path)
    version = platform.upload_file_policy(name="starter", content=policy_bytes(3, 5))
    request = platform.xp_create(xp_body(version.id, 3))
    download_results(platform, request.id, tmp_path / "dl" / request.id)
    evaluated = subprocess.run(
        ["python3", "mixin/tools/evaluate.py", str(tmp_path / "dl")],
        cwd=code,
        capture_output=True,
        text=True,
    )
    rows = [json.loads(line) for line in evaluated.stdout.splitlines()]
    assert evaluated.returncode == 0 and len(rows) == 4  # two episodes, two seats each
    assert [row["seat"] for row in rows] == [0, 1, 0, 1]
    assert (
        rows[0]["seat"] == 0
        and rows[0]["seed"] == 3
        and set(rows[0]["metrics"]) == {"score", "errors"}
    )


def test_fakegame_repo_variant_declares_another_coworld(tmp_path):
    repo, _ = make_fakegame_repo(tmp_path, coworld_id="cow_other")
    assert front_matter((repo / "MIXIN.md").read_text())["coworld"] == "cow_other"


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        ({"target": {"league_id": fake.LEAGUE_ID}}, "coworld_id cannot be supplied"),
        ({"coworld_id": None}, "coworld_id or target is required"),
        ({"roster": [{"player": {"policy_ref": "pv"}, "slot": 0}]}, "this game seats 2"),
        (
            {"roster": [{"player": {"policy_ref": "a"}, "slot": 0}, {"player": {"random": True}}]},
            "require a league target",
        ),
        (
            {
                "roster": [
                    {"player": {"policy_ref": "a"}},
                    {"player": {"policy_ref": "b", "top_n": 3}},
                ]
            },
            "exactly one of",
        ),
        (
            {
                "roster": [
                    {"player": {"policy_ref": "a"}, "slot": 2},
                    {"player": {"policy_ref": "b"}},
                ]
            },
            "within the player count",
        ),
    ],
)
def test_xp_create_refuses_what_the_platform_refuses(tmp_path, change, detail):
    platform = make(tmp_path)
    with pytest.raises(PlatformError, match=detail) as refused:
        platform.xp_create({**xp_body("pv", 1), **change})
    assert refused.value.retryable is False
    assert platform.snapshot()["counts"]["xp_create"] == 0


def test_xp_create_samples_a_league_seat_for_the_field(tmp_path):
    platform = make(tmp_path)
    roster = [{"player": {"policy_ref": "pv"}, "slot": 0}, {"player": {"random": True}, "slot": -1}]
    body = {"target": {"league_id": fake.LEAGUE_ID}, "roster": roster, "num_episodes": 1}
    request = platform.xp_create(body)
    assert request.coworld_id == fake.COWORLD_ID
    (row,) = platform.snapshot()["xp"].values()
    assert (row["policy"], row["seats"]) == ("pv", ["pv", fake.LEAGUE_POLICIES[0]])
