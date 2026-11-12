"""RealPlatform against a scripted HTTP server; nothing touches the network.

The coworld clients build their own ``httpx.Client`` (behind a ``RateLimitTransport``) with no
transport parameter, and the presigned upload PUT, the manifest fetch and the episode downloads
create plain clients of their own. The one place all of them meet is where httpx builds a default
transport, ``httpx.Client._init_transport`` (plus ``_init_proxy_transport`` when proxy variables
are set). The ``server`` fixture replaces both with an ``httpx.MockTransport`` over
:class:`FakeServer`; an explicitly passed transport (the rate-limit wrapper) is kept, so the real
client chain stays in place. Production code has no test hook.
"""

import hashlib
import json
from collections.abc import Callable

import httpx
import pytest

from research_core.platform.errors import (
    CreditsRefused,
    IdentityUnavailable,
    PlatformError,
)
from research_core.platform.fake import FakePlatform
from research_core.platform.protocol import with_retries
from research_core.platform.real import RealPlatform

SERVER = "http://platform.test"
NOW = "2026-10-28T10:00:00Z"
NOW_ISO = "2026-10-28T10:00:00+00:00"
PV_ID = "11111111-1111-1111-1111-111111111111"
PV2_ID = "22222222-2222-2222-2222-222222222222"

GAME = {
    "id": "game_1",
    "name": "Fakegame",
    "coworld_id": "cow_1",
    "coworld_name": "fakegame",
    "created_at": NOW,
}
LEAGUE = {
    "id": "league_1",
    "name": "Fakegame Open",
    "game": GAME,
    "participation_url": f"{SERVER}/observatory/v2/participate?league_id=league_1",
    "forum_markdown_url": f"{SERVER}/observatory/v2/forums/fakegame.md",
    "wiki_markdown_url": None,
    "created_at": NOW,
}
DIVISION = {
    "id": "div_1",
    "name": "Bronze",
    "level": 1,
    "type": "ladder",
    "hidden": False,
    "league": LEAGUE,
    "created_at": NOW,
}
POLICY_VERSION = {
    "id": PV_ID,
    "policy": {"id": "33333333-3333-3333-3333-333333333333", "name": "best"},
    "version": 3,
    "player_id": "ply_me",
}
MEMBERSHIP = {
    "id": "mem_1",
    "status": "competing",
    "substatus": "active",
    "is_champion": True,
    "league": LEAGUE,
    "division": DIVISION,
    "policy_version": POLICY_VERSION,
    "player": {"id": "ply_me", "name": "Me"},
    "created_at": NOW,
}
SUBMISSION = {
    "id": "sub_1",
    "status": "pending",
    "league": LEAGUE,
    "policy_version": POLICY_VERSION,
    "player": {"id": "ply_me"},
    "league_policy_membership_id": None,
    "created_at": NOW,
}
XP = {
    "id": "xreq_1",
    "requester_user_id": "user_1",
    "coworld_id": "cow_1",
    "coworld_name": "fakegame",
    "coworld_version": "1",
    "title": None,
    "status": "pending",
    "episode_count": 4,
    "pending_count": 4,
    "submitted_count": 0,
    "running_count": 0,
    "completed_count": 0,
    "failed_count": 0,
    "created_at": NOW,
    "episodes": [],
}
CREDITS = {
    "status": {
        "balance_credits": 120.5,
        "max_balance_credits": 500,
        "refill_credits": 100,
        "refill_cadence": "daily",
        "next_refill_at": NOW,
        "credits_per_usd": 10,
        "spent_this_period_credits": 30,
        "enforced": True,
    },
    "recent_requests": [
        {
            "experience_request_id": "xreq_0",
            "coworld": "fakegame",
            "episodes": 4,
            "compute_cost_credits": 2.5,
            "last_activity_at": NOW,
        }
    ],
}
AUTHOR = {"type": "player", "player_id": "ply_me", "name": "Me"}
POST = {
    "id": "post_1",
    "title": "Hello",
    "author": AUTHOR,
    "content_format": "markdown",
    "body": "hi",
    "created_at": NOW,
    "score": 1,
    "vote_count": 1,
}
WIKI_PAGE = {
    "id": "page_1",
    "wiki_id": "wiki_1",
    "slug": "strategy",
    "title": "Strategy",
    "current_revision_id": "rev_2",
    "created_at": NOW,
}
WIKI_REVISION = {
    "id": "rev_2",
    "page_id": "page_1",
    "body": "text",
    "author": AUTHOR,
    "note": None,
    "parent_revision_id": "rev_1",
    "created_at": NOW,
}

Reply = Callable[[httpx.Request], httpx.Response]


def reply(status: int = 200, *, json=None, content=None, headers=None) -> Reply:
    def build(_: httpx.Request) -> httpx.Response:
        extra = {} if json is None else {"json": json}
        if content is not None:
            extra["content"] = content
        return httpx.Response(status, headers=headers, **extra)

    return build


def connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class FakeServer:
    """Replies scripted per (method, path); the last reply repeats; every request is kept."""

    def __init__(self):
        self.scripts: dict[tuple[str, str], list[Reply]] = {}
        self.calls: list[httpx.Request] = []

    def on(self, method: str, path: str, *replies: Reply) -> None:
        self.scripts.setdefault((method, path), []).extend(replies)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        queue = self.scripts.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(
                599, json={"detail": f"unscripted {request.method} {request.url.path}"}
            )
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return answer(request)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.calls]

    def body(self, index: int) -> dict:
        return json.loads(self.calls[index].content)


@pytest.fixture
def server(monkeypatch) -> FakeServer:
    fake = FakeServer()
    mock = httpx.MockTransport(fake.handle)

    def init_transport(self, *args, transport=None, **kwargs):
        return transport or mock

    monkeypatch.setattr(httpx.Client, "_init_transport", init_transport)
    monkeypatch.setattr(httpx.Client, "_init_proxy_transport", lambda self, *a, **k: mock)
    return fake


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return {
        "COGAMES_API_URL": SERVER,
        "SOFTMAX_PLAYER_CREDENTIAL": "cga_player",
        "SOFTMAX_PLAYER_ID": "ply_me",
        # No User token on disk: never read the developer's real ~/.softmax.
        "SOFTMAX_CONFIG_DIR": str(tmp_path / "softmax"),
    }


@pytest.fixture
def platform(server, env):
    with RealPlatform(env) as real:
        yield real


# -- upload -------------------------------------------------------------------------------------


def test_upload_is_the_three_step_flow(platform, server):
    content = b'{"patience": 3}'
    digest = hashlib.sha256(content).hexdigest()
    server.on(
        "POST",
        "/observatory/stats/policies/files/upload",
        reply(json={"upload_url": "http://storage.test/bucket/key?X-Amz-Signature=abc"}),
    )
    server.on("PUT", "/bucket/key", reply(200))
    server.on(
        "POST",
        "/observatory/stats/policies/files/complete",
        reply(json={"id": "pv_new", "name": "best", "version": 4}),
    )

    version = platform.upload_file_policy(name="best", content=content, tags={"title": "V-7"})

    assert version.id == "pv_new" and version.label == "best:v4"
    assert version.content_hash == digest and version.player_id == "ply_me"
    assert server.paths() == [
        "POST /observatory/stats/policies/files/upload",
        "PUT /bucket/key",
        "POST /observatory/stats/policies/files/complete",
    ]
    ticket = server.body(0)
    assert ticket == {
        "name": "best",
        "content_hash": digest,
        "size_bytes": len(content),
        "tags": {"title": "V-7"},
    }
    assert server.calls[0].headers["authorization"] == "Bearer cga_player"
    put = server.calls[1]
    assert put.content == content
    assert put.headers["content-type"] == "application/octet-stream"
    assert "authorization" not in put.headers
    assert server.body(2) == ticket
    assert platform.last_identity == "player"


def test_upload_of_known_bytes_skips_the_put(platform, server):
    server.on(
        "POST",
        "/observatory/stats/policies/files/upload",
        reply(json={"existing_policy_version": {"id": "pv_old", "name": "best", "version": 2}}),
    )
    server.on(
        "POST",
        "/observatory/stats/policies/files/complete",
        reply(json={"id": "pv_old", "name": "best", "version": 2}),
    )
    version = platform.upload_file_policy(name="best", content=b"same")
    assert version.id == "pv_old" and version.version == 2
    assert server.paths() == [
        "POST /observatory/stats/policies/files/upload",
        "POST /observatory/stats/policies/files/complete",
    ]
    assert server.body(0)["tags"] == {}


def test_failed_put_is_a_retryable_platform_error(platform, server):
    server.on(
        "POST",
        "/observatory/stats/policies/files/upload",
        reply(json={"upload_url": "http://storage.test/bucket/key?sig=1"}),
    )
    server.on("PUT", "/bucket/key", reply(503, content=b"<Error>SlowDown</Error>"))
    with pytest.raises(PlatformError) as info:
        platform.upload_file_policy(name="best", content=b"x")
    assert info.value.retryable and info.value.status == 503
    assert info.value.route == "PUT http://storage.test/bucket/key"  # the signature is not kept
    assert info.value.detail == "<Error>SlowDown</Error>"


# -- submit, credits, XP ------------------------------------------------------------------------


def test_submit_as_the_workspace_player(platform, server):
    server.on("POST", "/observatory/v2/league-submissions", reply(json=SUBMISSION))
    submission = platform.submit(
        league_id="league_1", policy_version_id=PV_ID, auto_champion="lineage"
    )
    assert server.body(0) == {
        "league_id": "league_1",
        "policy_version_id": PV_ID,
        "auto_champion": "lineage",
        "player_id": "ply_me",
    }
    assert submission.id == "sub_1" and submission.status == "pending"
    assert submission.league_id == "league_1" and submission.policy_version_id == PV_ID
    assert submission.membership_id is None and submission.created_at == NOW_ISO


def test_submit_named_player_and_bad_policy_id(platform, server):
    server.on("POST", "/observatory/v2/league-submissions", reply(json=SUBMISSION))
    platform.submit(league_id="league_1", policy_version_id=PV_ID, player_id="ply_other")
    assert server.body(0)["player_id"] == "ply_other"

    with pytest.raises(PlatformError) as info:
        platform.submit(league_id="league_1", policy_version_id="V-7")
    assert not info.value.retryable and "not a policy version id" in info.value.detail
    assert len(server.calls) == 1


def test_credits(platform, server):
    server.on("GET", "/observatory/usage/me/credits", reply(json=CREDITS))
    credits = platform.credits()
    assert credits.balance == 120.5 and credits.max == 500 and credits.refill_amount == 100
    assert credits.next_refill_at == NOW_ISO and credits.spent_this_period == 30
    assert credits.recent_requests[0]["experience_request_id"] == "xreq_0"
    assert credits.recent_requests[0]["compute_cost_credits"] == 2.5


def test_xp_create_and_get(platform, server):
    server.on("POST", "/observatory/v2/experience-requests", reply(json=XP))
    done = {**XP, "status": "completed", "completed_count": 3, "failed_count": 1, "error": "e"}
    server.on("GET", "/observatory/v2/experience-requests/xreq_1", reply(json=done))

    body = {
        "coworld_id": "cow_1",
        "num_episodes": 4,
        "title": "ide:X-1:7:pair",
        "idempotency_key": "request-operation-1",
    }
    created = platform.xp_create(body)
    assert server.body(0) == body
    assert created.id == "xreq_1" and created.status == "pending" and not created.terminal
    assert created.title == "ide:X-1:7:pair"  # from the create body (A4.4)
    assert created.episode_count == 4 and created.coworld_id == "cow_1"
    assert created.coworld_version == "1"
    assert created.created_at == NOW_ISO

    fetched = platform.xp_get("xreq_1")
    assert fetched.terminal and fetched.completed_count == 3 and fetched.failed_count == 1
    assert fetched.error == "e" and fetched.title is None


def test_xp_list_and_episodes(platform, server):
    titled = {**XP, "title": "ide:X-1:7:pair"}
    server.on(
        "GET",
        "/observatory/v2/experience-requests",
        reply(json={"entries": [titled], "next_cursor": None}),
    )
    episode = {
        "id": "ereq_1",
        "requester_user_id": "user_1",
        "status": "completed",
        "policy_version_ids": [PV_ID],
        "participants": [],
        "scores": [{"policy_version_id": PV_ID, "score": 1.5}],
        "created_at": NOW,
    }
    server.on("GET", "/observatory/v2/experience-requests/xreq_1/episodes", reply(json=[episode]))

    listed = platform.xp_list(mine=True, limit=10)
    assert [x.title for x in listed] == ["ide:X-1:7:pair"]
    assert server.calls[0].url.params["mine"] == "true"
    assert server.calls[0].url.params["limit"] == "10"

    episodes = platform.xp_episodes("xreq_1")
    assert episodes[0]["id"] == "ereq_1" and episodes[0]["scores"][0]["score"] == 1.5


def test_xp_download_returns_the_completed_files(platform, server, tmp_path):
    manifest = {
        "entries": [
            {
                "episode_request_id": "ereq_1",
                "category": "results",
                "position": None,
                "artifact_id": "art_results",
                "version": "v1",
                "filename": "results.json",
                "state": "ready",
                "media_type": "application/json",
                "inline_json": {"score": 1},
            },
            {
                "episode_request_id": "ereq_1",
                "category": "replay",
                "position": None,
                "artifact_id": "art_replay",
                "version": None,
                "filename": "replay.json.z",
                "state": "not_produced",
                "media_type": "application/octet-stream",
            },
        ],
        "unavailable_ids": [],
        "next_cursor": None,
    }
    server.on("GET", "/observatory/v2/episode-requests/download-manifest", reply(json=manifest))
    dest = tmp_path / "xp"
    files = platform.xp_download("xreq_1", include=["results", "replay"], dest=dest)
    assert len(files) == 1 and files[0].name == "results.json"
    assert json.loads(files[0].read_text()) == {"score": 1}
    assert (dest / "manifest.jsonl").exists()
    assert server.calls[0].url.params.get_list("include") == ["results", "replay"]


# -- error mapping ------------------------------------------------------------------------------


def test_503_is_retryable_with_route_status_and_request_id(platform, server):
    server.on(
        "GET",
        "/observatory/v2/leagues",
        reply(503, json={"detail": "warming up"}, headers={"x-request-id": "req-503"}),
    )
    with pytest.raises(PlatformError) as info:
        platform.leagues()
    error = info.value
    assert error.retryable and error.status == 503 and error.request_id == "req-503"
    assert error.route == "GET /v2/leagues" and error.detail == "warming up"


def test_with_retries_recovers_from_a_503(platform, server):
    server.on(
        "GET",
        "/observatory/v2/leagues",
        reply(503, json={"detail": "warming up"}),
        reply(200, json=[LEAGUE]),
    )
    slept: list[float] = []
    leagues = with_retries(platform.leagues, sleep=slept.append)
    assert [league.id for league in leagues] == ["league_1"]
    assert slept == [1.0]
    assert server.paths() == ["GET /observatory/v2/leagues"] * 2


def test_402_becomes_credits_refused_with_the_body_fields(platform, server):
    body = {
        "detail": {
            "message": "Insufficient credits",
            "balance": "3.5",
            "held": 10,
            "next_refill_at": "2026-10-29T00:00:00Z",
        }
    }
    server.on("POST", "/observatory/v2/experience-requests", reply(402, json=body))
    with pytest.raises(CreditsRefused) as info:
        platform.xp_create({"coworld_id": "cow_1"})
    refused = info.value
    assert not refused.retryable and refused.status == 402
    assert refused.detail == "Insufficient credits"
    assert refused.balance == 3.5 and refused.held == 10.0
    assert refused.next_refill_at == "2026-10-29T00:00:00Z"
    assert refused.route == "POST /v2/experience-requests"


def test_402_without_credit_fields(platform, server):
    server.on("POST", "/observatory/v2/experience-requests", reply(402, json={"detail": "nope"}))
    with pytest.raises(CreditsRefused) as info:
        platform.xp_create({})
    assert info.value.detail == "nope"
    assert (info.value.balance, info.value.held, info.value.next_refill_at) == (None, None, None)


def test_404_is_not_retryable(platform, server):
    server.on(
        "GET",
        "/observatory/v2/leagues/league_x",
        reply(404, json={"detail": "League not found"}, headers={"X-Request-Id": "req-404"}),
    )
    with pytest.raises(PlatformError) as info:
        platform.league("league_x")
    assert info.value.to_dict() == {
        "error": "League not found",
        "route": "GET /v2/leagues/league_x",
        "status": 404,
        "request_id": "req-404",
        "retryable": False,
    }


def test_error_detail_falls_back_to_body_text_and_is_trimmed(platform, server):
    server.on("GET", "/observatory/v2/leagues", reply(500, content=b"x" * 5000))
    with pytest.raises(PlatformError) as info:
        platform.leagues()
    assert len(info.value.detail) == 2000 and info.value.retryable

    server.on("GET", "/observatory/v2/divisions", reply(502))
    with pytest.raises(PlatformError) as info:
        platform.divisions("league_1")
    assert info.value.detail == "HTTP 502 Bad Gateway"


def test_transport_errors_are_retryable(platform, server):
    server.on("GET", "/observatory/v2/leagues", connect_error)
    with pytest.raises(PlatformError) as info:
        platform.leagues()
    assert info.value.retryable and info.value.status is None
    assert info.value.route == "GET /v2/leagues"
    assert "ConnectError" in info.value.detail
    assert len(server.calls) == 1  # the scripted server, not the network, raised it


def test_unparseable_response_is_not_retryable(platform, server):
    server.on("GET", "/observatory/usage/me/credits", reply(json={"unexpected": True}))
    with pytest.raises(PlatformError) as info:
        platform.credits()
    assert not info.value.retryable and "unexpected response shape" in info.value.detail


# -- identities ---------------------------------------------------------------------------------


def test_user_identity_absent_is_refused_before_any_call(platform, server):
    with pytest.raises(IdentityUnavailable) as info:
        platform.forum_post(title="t", body="b", idempotency_key="k", identity="user")
    assert info.value.identity == "user" and not info.value.retryable
    with pytest.raises(IdentityUnavailable):
        platform.whoami("user")
    with pytest.raises(IdentityUnavailable):
        platform.request("GET", "/players", identity="user")
    assert server.calls == []


def test_user_identity_uses_its_own_token(server, env):
    server.on("POST", "/observatory/v2/posts", reply(json=POST))
    server.on(
        "GET",
        "/observatory/v2/forums/fakegame/posts",
        reply(json={"entries": [POST], "next_cursor": None}),
    )
    platform = RealPlatform({**env, "SOFTMAX_USER_CREDENTIAL": "user_tok"})

    post = platform.forum_post(title="Hello", body="hi", idempotency_key="k1", identity="user")
    assert post["id"] == "post_1" and post["identity"] == "user"
    assert platform.last_identity == "user"
    assert server.calls[0].headers["authorization"] == "Bearer user_tok"
    assert server.body(0) == {
        "title": "Hello",
        "idempotency_key": "k1",
        "content_format": "markdown",
        "body": "hi",
    }

    posts = platform.forum_list("fakegame")
    assert posts[0]["author"]["player_id"] == "ply_me"
    assert platform.last_identity == "player"
    assert server.calls[1].headers["authorization"] == "Bearer cga_player"
    assert server.calls[1].url.params["sort"] == "hot"


def test_player_session_json_and_whoami(server, env):
    credential = json.dumps({"token": "ply_session", "expires_at": "2099-01-01T00:00:00Z"})
    me = {
        "user_email": "me@example.test",
        "subject_type": "player",
        "subject_id": "ply_me",
        "owner_user_id": "user_1",
    }
    server.on("GET", "/observatory/whoami", reply(json=me))
    platform = RealPlatform({**env, "SOFTMAX_PLAYER_CREDENTIAL": credential})
    who = platform.whoami()
    assert who.kind == "player" and who.subject_id == "ply_me"
    assert who.email == "me@example.test" and who.owner_user_id == "user_1"
    assert who.expires_at == "2099-01-01T00:00:00Z"
    assert server.calls[0].headers["authorization"] == "Bearer ply_session"


def test_expired_player_session_never_reaches_the_platform(server, env):
    credential = json.dumps({"token": "ply_old", "expires_at": "2020-01-01T00:00:00Z"})
    platform = RealPlatform({**env, "SOFTMAX_PLAYER_CREDENTIAL": credential})
    with pytest.raises(PlatformError, match="Player session expired") as info:
        platform.credits()
    assert not info.value.retryable
    assert server.calls == []


# -- reads: leagues, memberships, policies, episodes, manifest ----------------------------------


def test_league_mapping_and_dict_reads(platform, server):
    server.on("GET", "/observatory/v2/leagues", reply(json=[LEAGUE]))
    server.on("GET", "/observatory/v2/leagues/league_1", reply(json=LEAGUE))
    server.on("GET", "/observatory/v2/divisions", reply(json=[DIVISION]))
    entry = {"rank": 1, "player_id": "ply_me", "score": 1500.0, "rounds_played": 3}
    server.on("GET", "/observatory/v2/divisions/div_1/leaderboard", reply(json=[entry]))

    leagues = platform.leagues(game_id="game_1")
    assert server.calls[0].url.params["game_id"] == "game_1"
    league = leagues[0]
    assert league.id == "league_1" and league.game_id == "game_1"
    assert league.coworld_id == "cow_1" and league.coworld_name == "fakegame"
    assert league.wiki_markdown_url is None
    assert league.forum_markdown_url == LEAGUE["forum_markdown_url"]
    assert platform.league("league_1") == league

    divisions = platform.divisions("league_1")
    assert server.calls[2].url.params["league_id"] == "league_1"
    assert divisions[0]["id"] == "div_1" and divisions[0]["league"]["id"] == "league_1"
    assert platform.standings("div_1") == [{**entry, "player_name": None, "recent_rounds": None}]


def test_memberships_follow_the_cursor_header(platform, server):
    second = {**MEMBERSHIP, "id": "mem_2", "is_champion": False, "player": None}
    server.on(
        "GET",
        "/observatory/v2/league-policy-memberships",
        reply(json=[MEMBERSHIP], headers={"X-Next-Cursor": "page-2"}),
        reply(json=[second]),
    )
    rows = platform.memberships(mine=True, champions_only=True, policy_version_id=PV_ID)
    assert [m.id for m in rows] == ["mem_1", "mem_2"]
    first = rows[0]
    assert first.league_id == "league_1" and first.division_id == "div_1"
    assert first.status == "competing" and first.substatus == "active" and first.is_champion
    assert first.player_id == "ply_me" and first.policy_version.label == "best:v3"
    assert first.policy_version.id == PV_ID
    assert rows[1].player_id == "ply_me"  # falls back to the policy version's player
    params = server.calls[0].url.params
    assert params["mine"] == "true" and params["champions_only"] == "true"
    assert params["active_only"] == "false" and params["policy_version_id"] == PV_ID
    assert "cursor" not in params and server.calls[1].url.params["cursor"] == "page-2"


def test_submissions(platform, server):
    placed = {**SUBMISSION, "status": "placed", "league_policy_membership_id": "mem_1"}
    server.on("GET", "/observatory/v2/league-submissions", reply(json=[placed]))
    rows = platform.submissions(league_id="league_1", policy_version_id=PV_ID, mine=True)
    assert rows[0].status == "placed" and rows[0].membership_id == "mem_1"
    params = server.calls[0].url.params
    assert params["league_id"] == "league_1" and params["mine"] == "true"


def test_lookup_policy_upload_answers(platform, server):
    inputs = {
        "name": "best",
        "player_id": "ply_me",
        "file_hash": "a" * 64,
        "file_size": 3,
        "attributes": {},
    }
    server.on(
        "POST",
        "/observatory/stats/policies/lookup",
        reply(content=b"null"),  # the platform's definite "no committed version matches"
        reply(json={"id": "pv_1", "name": "best", "version": 2}),
        reply(403, json={"detail": "not this player's upload"}),
    )
    assert platform.lookup_policy_upload(inputs) is None
    found = platform.lookup_policy_upload(inputs)
    assert found.id == "pv_1" and found.label == "best:v2"
    assert found.content_hash == "a" * 64 and found.player_id == "ply_me"
    with pytest.raises(PlatformError) as info:
        platform.lookup_policy_upload(inputs)
    assert info.value.status == 403 and not info.value.retryable
    assert "not this player's upload" in info.value.detail
    assert server.body(0) == inputs
    assert server.calls[0].headers["authorization"] == "Bearer cga_player"


def test_upload_inputs_are_coworlds_and_the_fake_matches(platform, server, tmp_path):
    content = b'{"patience": 3}'
    artifact = tmp_path / "policy.json"
    artifact.write_bytes(content)
    inputs = platform.upload_inputs(name="best", artifact=artifact)
    assert inputs == {
        "name": "best",
        "player_id": "ply_me",
        "file_hash": hashlib.sha256(content).hexdigest(),
        "file_size": len(content),
        "attributes": {},
    }
    fake = FakePlatform(tmp_path / "fake.json", {"SOFTMAX_PLAYER_ID": "ply_me"})
    assert fake.upload_inputs(name="best", artifact=artifact) == inputs
    assert server.calls == []


def test_upload_inputs_read_the_player_id_when_the_env_has_none(server, env, tmp_path):
    artifact = tmp_path / "policy.json"
    artifact.write_bytes(b"x")
    me = {
        "user_email": "me@example.test",
        "subject_type": "player",
        "subject_id": "ply_from_token",
        "owner_user_id": "user_1",
    }
    server.on("GET", "/observatory/whoami", reply(json=me))
    without_id = {k: v for k, v in env.items() if k != "SOFTMAX_PLAYER_ID"}
    with RealPlatform(without_id) as platform:
        inputs = platform.upload_inputs(name="best", artifact=artifact)
    assert inputs["player_id"] == "ply_from_token" and inputs["file_size"] == 1
    assert server.paths() == ["GET /observatory/whoami"]


def test_policy_versions_by_name_paginate(platform, server):
    # The listing's hash column is ``player_file_content_hash`` (team callers only).
    row1 = {
        "id": PV_ID,
        "name": "best",
        "version": 3,
        "player_file_content_hash": "abc",
        "player_id": "ply_me",
    }
    row2 = {"id": PV2_ID, "name": "best", "version": 2}
    server.on(
        "GET",
        "/observatory/stats/policy-versions",
        reply(json={"entries": [row1], "next_cursor": "c1"}),
        reply(json={"entries": [row2]}),
    )
    versions = platform.policy_versions(name="best")
    assert [(v.id, v.version, v.content_hash) for v in versions] == [
        (PV_ID, 3, "abc"),
        (PV2_ID, 2, None),
    ]
    assert versions[0].player_id == "ply_me"
    params = server.calls[0].url.params
    assert params["name_exact"] == "best" and params["mine"] == "true" and "cursor" not in params
    assert server.calls[1].url.params["cursor"] == "c1"


def test_policy_versions_without_a_name_lists_mine(platform, server):
    server.on("GET", "/observatory/stats/policy-versions", reply(json={"entries": []}))
    assert platform.policy_versions(player_id="ply_me", mine=False) == []
    params = server.calls[0].url.params
    assert "name_exact" not in params and params["mine"] == "false"
    assert params["player_id"] == "ply_me"


def file_ticket(content: bytes, content_hash: str | None = None) -> Reply:
    return reply(
        json={
            "policy_version_id": PV_ID,
            "content_hash": content_hash or hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "download_url": "http://storage.test/policy?signature=secret",
            "expires_in_seconds": 300,
        }
    )


def test_policy_file_follows_the_presigned_url(platform, server):
    server.on("GET", f"/observatory/v2/policy-files/{PV_ID}/download", file_ticket(b"bytes"))
    server.on("GET", "/policy", reply(content=b"bytes"))
    assert platform.policy_file(PV_ID) == b"bytes"
    assert server.calls[0].headers["authorization"] == "Bearer cga_player"
    assert "authorization" not in server.calls[1].headers  # the URL carries its own signature


def test_policy_file_refuses_bytes_that_do_not_match_the_hash(platform, server):
    server.on("GET", f"/observatory/v2/policy-files/{PV_ID}/download", file_ticket(b"a", "0" * 64))
    server.on("GET", "/policy", reply(content=b"a"))
    with pytest.raises(PlatformError) as info:
        platform.policy_file(PV_ID)
    assert not info.value.retryable and "does not match" in info.value.detail


def test_policy_file_of_an_image_policy_is_a_409(platform, server):
    no_file = reply(409, json={"detail": "Policy version has no player file"})
    server.on("GET", f"/observatory/v2/policy-files/{PV_ID}/download", no_file)
    with pytest.raises(PlatformError) as info:
        platform.policy_file(PV_ID)
    assert info.value.status == 409 and not info.value.retryable


def test_episodes(platform, server):
    summary = {
        "id": "ereq_1",
        "status": "completed",
        "policy_version_ids": [PV_ID],
        "created_at": NOW,
    }
    server.on(
        "GET",
        f"/observatory/v2/policy-versions/{PV_ID}/episode-requests",
        reply(json={"entries": [summary], "next_cursor": None}),
    )
    row = {
        "id": "ereq_1",
        "requester_user_id": "user_1",
        "status": "completed",
        "policy_version_ids": [PV_ID],
        "participants": [],
        "scores": [],
        "created_at": NOW,
        "game_config": {"seed": 7},
    }
    server.on("GET", "/observatory/v2/episode-requests/ereq_1", reply(json=row))
    server.on(
        "GET", "/observatory/v2/episode-requests/ereq_1/artifacts/replay", reply(content=b"\x00z")
    )
    assert platform.episodes_for_policy(PV_ID).entries[0]["id"] == "ereq_1"
    assert platform.episode("ereq_1")["game_config"] == {"seed": 7}
    assert platform.episode_artifact("ereq_1", "replay") == b"\x00z"


def test_coworld_manifest_is_fetched_as_the_player(platform, server):
    coworld = {
        "id": "cow_1",
        "name": "fakegame",
        "version": "1.2.0",
        "visibility": "private",
        "manifest": {"game": {"player_runtime": "game-hosted", "runnable": {"source_url": "u"}}},
        "manifest_hash": "h",
        "size_bytes": 1,
        "canonical": True,
        "forum_markdown_url": "f",
        "wiki_markdown_url": "w",
        "documentation_url": "d",
        "certification_status_url": "c",
        "certification_transcript_url": "t",
    }
    server.on("GET", "/observatory/v2/coworlds/cow_1", reply(json=coworld))
    manifest = platform.coworld_manifest("cow_1")
    assert manifest == {
        "id": "cow_1",
        "name": "fakegame",
        "version": "1.2.0",
        "manifest": coworld["manifest"],
    }
    assert server.calls[0].headers["authorization"] == "Bearer cga_player"


# -- forum, wiki, request -----------------------------------------------------------------------


def test_forum_reads_comments_and_votes(platform, server):
    server.on("GET", "/observatory/v2/posts/post_1.md", reply(content=b"# Hello\n\nhi"))
    server.on(
        "GET",
        "/observatory/v2/forums/search",
        reply(
            json={
                "entries": [{"post": POST, "rank": 0.9}],
                "next_cursor": None,
                "result_quality": "newest_5000_candidates",
                "candidate_limit": 5000,
                "older_matches_may_be_omitted": False,
            }
        ),
    )
    comment = {"id": "c_1", "parent_id": None, "author": AUTHOR, "body": "+1", "created_at": NOW}
    server.on("POST", "/observatory/v2/posts/post_1/comments", reply(json=comment))
    server.on(
        "PUT",
        "/observatory/v2/posts/post_1/comments/c_1/vote",
        reply(json={"value": 1, "score": 2, "vote_count": 2}),
    )

    assert platform.forum_read("post_1") == "# Hello\n\nhi"
    hits = platform.forum_search("hello")
    assert hits[0]["post"]["id"] == "post_1" and server.calls[1].url.params["q"] == "hello"
    made = platform.forum_comment("post_1", body="+1", idempotency_key="k2", parent_id=None)
    assert made["id"] == "c_1" and made["identity"] == "player"
    assert server.body(2) == {"body": "+1", "idempotency_key": "k2"}
    vote = platform.forum_vote("post_1", 1, comment_id="c_1")
    assert vote == {"value": 1, "score": 2, "vote_count": 2, "identity": "player"}


def test_wiki_edit_conflict_is_a_result_not_an_error(platform, server):
    conflict = {
        "detail": {
            "type": "wiki_edit_conflict",
            "current_revision_id": "rev_9",
            "current_body": "theirs",
        }
    }
    server.on("PUT", "/observatory/v2/wikis/fakegame/pages/strategy", reply(409, json=conflict))
    result = platform.wiki_edit(
        "fakegame",
        "strategy",
        title="Strategy",
        body="mine",
        base_revision_id="rev_2",
        idempotency_key="k3",
    )
    assert result == {
        "conflict": True,
        "current_revision_id": "rev_9",
        "current_body": "theirs",
        "identity": "player",
    }
    assert server.body(0) == {
        "title": "Strategy",
        "body": "mine",
        "idempotency_key": "k3",
        "base_revision_id": "rev_2",
    }


def test_wiki_edit_with_no_base_creates_a_page(platform, server):
    edited = {"page": WIKI_PAGE, "revision": {**WIKI_REVISION, "id": "rev_1", "note": None}}
    server.on("PUT", "/observatory/v2/wikis/fakegame/pages/strategy", reply(json=edited))
    result = platform.wiki_edit(
        "fakegame",
        "strategy",
        title="Strategy",
        body="first",
        base_revision_id=None,
        idempotency_key="k5",
    )
    assert result["conflict"] is False and result["revision"]["id"] == "rev_1"
    assert server.body(0) == {"title": "Strategy", "body": "first", "idempotency_key": "k5"}


def test_a_post_to_a_named_forum_uses_its_route(platform, server):
    server.on("POST", "/observatory/v2/forums/fakegame/posts", reply(201, json=POST))
    post = platform.forum_post(title="Hello", body="hi", idempotency_key="k1", slug="fakegame")
    assert post["id"] == "post_1" and post["identity"] == "player"
    assert server.body(0) == {
        "title": "Hello",
        "idempotency_key": "k1",
        "content_format": "markdown",
        "body": "hi",
    }


def test_uploads_and_submissions_name_the_player_whoami_gives(server, env):
    """Hosting does not set SOFTMAX_PLAYER_ID, so the Player comes from the credential, once."""
    me = {
        "user_email": "me@example.test",
        "subject_type": "player",
        "subject_id": "ply_from_token",
        "owner_user_id": "user_1",
    }
    server.on("GET", "/observatory/whoami", reply(json=me))
    without_id = {k: v for k, v in env.items() if k != "SOFTMAX_PLAYER_ID"}
    with RealPlatform(without_id) as platform:
        assert platform._player_id() == "ply_from_token"
        assert platform._player_id() == "ply_from_token"
    assert server.paths() == ["GET /observatory/whoami"]


def test_a_credential_that_is_not_a_players_names_no_player(server, env):
    me = {"subject_type": "user", "subject_id": "user_1", "user_email": "me@example.test"}
    server.on("GET", "/observatory/whoami", reply(json=me))
    without_id = {k: v for k, v in env.items() if k != "SOFTMAX_PLAYER_ID"}
    with RealPlatform(without_id) as platform:
        assert platform._player_id() is None


def test_wiki_read_history_search_and_edit(platform, server):
    server.on(
        "GET",
        "/observatory/v2/wikis/fakegame/pages/strategy",
        reply(json={**WIKI_PAGE, "current_revision": WIKI_REVISION}),
    )
    server.on(
        "GET",
        "/observatory/v2/wiki-pages/page_1/revisions",
        reply(json={"entries": [WIKI_REVISION]}),
    )
    server.on(
        "GET",
        "/observatory/v2/wikis/fakegame/search",
        reply(
            json={
                "entries": [{"page": WIKI_PAGE, "current_author": AUTHOR, "rank": 1.0}],
                "next_cursor": None,
                "result_quality": "exact_within_wiki",
            }
        ),
    )
    server.on(
        "PUT",
        "/observatory/v2/wikis/fakegame/pages/strategy",
        reply(json={"page": WIKI_PAGE, "revision": {**WIKI_REVISION, "id": "rev_3", "note": "n"}}),
    )

    page = platform.wiki_read("fakegame", "strategy")
    assert page["current_revision_id"] == "rev_2" and page["current_revision"]["body"] == "text"
    assert platform.wiki_history("page_1")[0]["id"] == "rev_2"
    assert platform.wiki_search("fakegame", "open")[0]["page"]["slug"] == "strategy"
    edited = platform.wiki_edit(
        "fakegame",
        "strategy",
        title="Strategy",
        body="mine",
        base_revision_id="rev_2",
        idempotency_key="k4",
        note="n",
    )
    assert edited["conflict"] is False and edited["revision"]["id"] == "rev_3"
    assert edited["identity"] == "player"


def test_request_shapes_and_set_champion(platform, server):
    server.on(
        "GET",
        "/observatory/v2/league-policy-memberships",
        reply(json=[{"id": "mem_1"}], headers={"X-Next-Cursor": "c2"}),
    )
    server.on(
        "POST",
        "/observatory/v2/league-policy-memberships/mem_1/champion",
        reply(json={"id": "mem_1", "is_champion": True}),
    )
    server.on("DELETE", "/observatory/v2/things/1", reply(204))
    server.on("GET", "/observatory/v2/text", reply(content=b"plain"))

    listing = platform.request("get", "/v2/league-policy-memberships", params={"mine": "true"})
    assert listing == {"entries": [{"id": "mem_1"}], "next_cursor": "c2"}
    assert server.calls[0].url.params["mine"] == "true"
    assert platform.set_champion("mem_1") == {"id": "mem_1", "is_champion": True}
    assert server.calls[1].method == "POST"
    assert platform.request("DELETE", "/v2/things/1") == {}
    assert platform.request("GET", "/v2/text") == {"text": "plain"}
    assert platform.last_identity == "player"
