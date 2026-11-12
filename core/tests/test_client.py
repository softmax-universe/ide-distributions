"""The :class:`ScriptedClient` (plan Task 6.3): its own behaviour over a running seam server.

The routes themselves are covered in ``test_serve.py`` (which drives them through the same
client); these tests cover what the client adds: run waiting, the bounded stream read, the
notices view, errors as :class:`SeamError`, and the session bookkeeping.
"""

from __future__ import annotations

import threading
import time

import pytest

from research_core.client import ScriptedClient, SeamError
from research_core.store import notices
from research_core.store.actors import Actor
from research_core.store.db import Store
from tests.test_serve import start_seam

HARNESS = Actor.harness()


@pytest.fixture
def seam(tmp_path):
    seam = start_seam(tmp_path)
    yield seam
    seam.server.shutdown()


def add_notice_later(seam, delay: float, text: str) -> None:
    """Write a notice from another thread after ``delay`` s, on that thread's own connection."""

    def add() -> None:
        with Store.open(seam.ctx.workspace.store_path, kind="workspace") as wstore:
            notices.add(wstore, HARNESS, text=text)

    threading.Timer(delay, add).start()


def test_say_before_open_is_a_runtime_error(seam):
    with pytest.raises(RuntimeError, match="open"):
        seam.client.say("hi")
    seam.client.open()
    assert seam.client.say("hi")["delivered"] == "queued"


def test_wait_run_polls_until_terminal_and_times_out(seam):
    seam.client.open()
    run = seam.say("work")
    with pytest.raises(TimeoutError):
        seam.client.wait_run(run["run_id"], timeout=0.3, poll=0.05)
    threading.Timer(0.3, seam.hermes.complete, args=(run["run_id"], "finished")).start()
    status = seam.client.wait_run(run["run_id"], timeout=5, poll=0.05)
    assert status["status"] == "completed" and status["output"] == "finished"
    assert status["terminal"] is True


def test_events_is_bounded_by_idle_and_until(seam):
    started = time.monotonic()
    events = seam.client.events("workspace", after=0, idle=0.5)
    assert events and time.monotonic() - started < 3
    seen = {e["seq"] for e in events}
    assert len(seen) == len(events)  # every frame carries its id once
    # until() stops the read as soon as the matching event arrives, before idle elapses, and
    # a long idle keeps the connection open while the store is quiet.
    add_notice_later(seam, 0.4, "V-2 submitted")
    deadline = time.monotonic() + 5
    got = seam.client.events(
        "workspace", after=events[-1]["seq"], idle=5, until=lambda e: e["kind"] == "notice"
    )
    assert [e["payload"]["text"] for e in got] == ["V-2 submitted"]
    assert time.monotonic() < deadline
    # And the timeout bound returns whatever arrived.
    assert seam.client.events("workspace", after=10**6, idle=5, timeout=0.3) == []


def test_notices_are_the_notice_events_of_the_workspace_stream(seam):
    notices.add(seam.wstore, HARNESS, text="first", refs=["T-1"])
    notices.add(seam.wstore, HARNESS, text="second", urgency="high")
    got = seam.client.notices(idle=0.5)
    assert [n["payload"]["text"] for n in got] == ["first", "second"]
    assert got[0]["payload"]["refs"] == ["T-1"] and got[1]["payload"]["urgency"] == "high"
    assert got[0]["subject"] == "N-1"
    assert seam.client.notices(after=got[-1]["seq"], idle=0.5) == []


def test_seam_error_carries_status_body_headers_and_route(seam):
    seam.client.session_id = "api_0_missing"
    with pytest.raises(SeamError) as info:
        seam.client.say("hello")
    error = info.value
    assert error.status == 404 and error.route == "POST /message"
    assert error.body["ok"] is False and "unknown session" in error.body["error"]
    assert error.headers["Content-Type"].startswith("application/json")
    assert "HTTP 404" in str(error)


def test_client_actor_is_recorded_on_writes(seam):
    client = ScriptedClient(seam.server.url, actor="user:james")
    client.open(client_id="ide-2", kind="ide")
    client.say("hello")
    assert seam.wstore.one("SELECT actor FROM events ORDER BY seq DESC LIMIT 1")[0] == "user:james"
    with pytest.raises(SeamError) as info:
        ScriptedClient(seam.server.url, actor="session:api_1_x").open()
    assert info.value.status == 400 and "sessions use the tools" in info.value.body["error"]


def test_apply_passes_store_through(seam):
    from research_core.store import directives

    result = seam.client.apply(
        "user:james",
        "directives.add",
        {"text": "Prefer short experiments", "scope": "workspace", "source": "ui"},
    )
    assert result["store"] == "workspace" and result["record"]["id"] == "W-1"
    retired = seam.client.apply(
        "user:james", "directives.retire", {"directive_id": "W-1"}, store="workspace"
    )
    assert retired["record"]["status"] == "retired"
    assert directives.read(seam.wstore, 1)["status"] == "retired"
