"""The ``softmax_api`` escape hatch: every call logged, User writes noticed, the Researcher is
Player only."""

from __future__ import annotations

import json

import pytest

import research_core.tools.softmax_api as softmax_api  # noqa: F401  (registers the tool)
from research_core.platform import fake
from research_core.store import notices
from research_core.tools.base import run_tool

LEAGUE = fake.LEAGUE_ID
POLICY = json.dumps({"parameters": {"aggression": 3, "patience": 5}}).encode()


@pytest.fixture
def workspace_env(tmp_path):
    return {
        "IDE_WORKSPACE": str(tmp_path / "ws"),
        "IDE_PLATFORM": "fake",
        "SOFTMAX_USER_CREDENTIAL": "usr-token",
    }


def request_events(store) -> list[dict]:
    return [e.payload for e in store.events_after(0) if e.kind == "platform.request"]


def membership_id(platform) -> str:
    version = platform.upload_file_policy(name="starter", content=POLICY)
    submission = platform.submit(league_id=LEAGUE, policy_version_id=version.id)
    platform.memberships(league_id=LEAGUE)
    return submission.membership_id


def test_a_call_with_no_coworld_is_logged_in_the_workspace_store(ctx_factory):
    ctx = ctx_factory(coworld=None)
    out = json.loads(run_tool("softmax_api", {"method": "GET", "path": "/whoami"}, ctx))
    assert out["ok"] is True, out
    assert request_events(ctx.wstore)[-1]["path"] == "/whoami"


def test_a_refused_write_as_the_user_says_nothing_was_done(call, ctx_factory):
    ctx = ctx_factory()
    out = call(
        "softmax_api",
        {"method": "POST", "path": "/v2/nope", "identity": "user", "json": {"a": 1}},
    )
    assert out["ok"] is False and out["context"]["status"] == 404
    assert not [n for n in notices.unread(ctx.wstore) if "User identity" in n["text"]]


def test_spec_is_flat_and_workspace_for_both_roles():
    spec = softmax_api.SPEC
    assert spec.executor == "workspace" and spec.actions is None
    assert spec.roles == {"orchestrator", "researcher"}
    assert spec.required == ("method", "path")


def test_get_writes_an_event_and_no_notice(call, ctx_factory):
    result = call("softmax_api", {"method": "GET", "path": "/v2/leagues"}, role="researcher")
    assert result["ok"] is True
    assert result["method"] == "GET" and result["identity"] == "player"
    assert [row["id"] for row in result["response"]["entries"]] == [LEAGUE]
    ctx = ctx_factory()
    assert request_events(ctx.store) == [
        {
            "method": "GET",
            "path": "/v2/leagues",
            "identity": "player",
            "outcome": "ok",
            "status": None,
        }
    ]
    assert notices.unread(ctx.wstore) == []


def test_user_post_writes_one_notice_and_one_event(call, ctx_factory):
    ctx = ctx_factory()
    mid = membership_id(ctx.platform())
    path = f"/v2/league-policy-memberships/{mid}/champion"
    result = call("softmax_api", {"method": "POST", "path": path, "identity": "user"})
    assert result["ok"] is True
    assert result["response"]["identity"] == "user"
    (notice,) = notices.unread(ctx.wstore, coworld_id="cow_fake")
    assert notice["text"] == f"Acted as your User identity: POST {path}"
    (event,) = request_events(ctx.store)
    assert event == {
        "method": "POST", "path": path, "identity": "user", "outcome": "ok", "status": None
    }  # fmt: skip


def test_user_get_writes_no_notice(call, ctx_factory):
    result = call("softmax_api", {"method": "GET", "path": "/whoami", "identity": "user"})
    assert result["ok"] is True and result["response"]["subject_type"] == "user"
    ctx = ctx_factory()
    assert notices.unread(ctx.wstore) == []
    assert request_events(ctx.store)[0]["identity"] == "user"


def test_researcher_cannot_act_as_the_user(call, ctx_factory):
    result = call(
        "softmax_api", {"method": "GET", "path": "/whoami", "identity": "user"}, role="researcher"
    )
    assert result["ok"] is False and "Player only" in result["error"]
    ctx = ctx_factory()
    assert request_events(ctx.store) == []
    assert notices.unread(ctx.wstore) == []


def test_failed_call_is_logged_with_its_status(call, ctx_factory):
    result = call("softmax_api", {"method": "GET", "path": "/v2/nothing-here"})
    assert result["ok"] is False
    assert result["context"]["status"] == 404
    assert result["context"]["route"] == "GET /v2/nothing-here"
    (event,) = request_events(ctx_factory().store)
    assert event["outcome"] == "error" and event["status"] == 404


def test_observatory_prefix_and_missing_slash_are_normalised(call):
    with_prefix = call("softmax_api", {"method": "GET", "path": "/observatory/v2/leagues"})
    assert with_prefix["ok"] is True and with_prefix["path"] == "/v2/leagues"
    without_slash = call("softmax_api", {"method": "GET", "path": "v2/leagues"})
    assert without_slash["ok"] is True and without_slash["path"] == "/v2/leagues"


def test_method_and_required_fields_are_validated(call):
    result = call("softmax_api", {"method": "FETCH", "path": "/v2/leagues"})
    assert result["ok"] is False and "must be one of" in result["error"]
    result = call("softmax_api", {"path": "/v2/leagues"})
    assert result["ok"] is False and "method is required" in result["error"]


def test_a_read_without_a_limit_asks_for_twenty(call, monkeypatch):
    sent = []
    real = fake.FakePlatform.request

    def recording(self, method, path, *, params=None, **kwargs):
        sent.append((method, params))
        return real(self, method, path, params=params, **kwargs)

    monkeypatch.setattr(fake.FakePlatform, "request", recording)
    call("softmax_api", {"method": "GET", "path": "/v2/leagues"})
    call("softmax_api", {"method": "GET", "path": "/v2/leagues", "params": {"limit": 5}})
    call("softmax_api", {"method": "POST", "path": "/v2/nope", "json": {"a": 1}})
    assert sent == [("GET", {"limit": 20}), ("GET", {"limit": 5}), ("POST", None)]
