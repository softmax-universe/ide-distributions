"""The acceptance scenario on the fake platform, end to end through the seam (plan Task 6.3,
design H "Validation"): "submit the starter as my champion" on a new workspace ends in a
submission.

No model runs. A *scripted agent* stands in for it (:mod:`research_core.scripted_agent`): the
``script`` hook of :class:`FakeHermesRuns` receives every run the seam server or the watcher
starts and plays the Orchestrator with real tool calls (``run_tool``) as that run's Session,
then completes the run. The watcher tick runs inline between the runs, as the cron job would.
The seam server is ``research serve`` on a thread; the client is the :class:`ScriptedClient`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research_core import layout
from research_core.client import ScriptedClient
from research_core.platform.fake import FakePlatform
from research_core.scripted_agent import CITES, REQUEST, ScriptedAgent
from research_core.serve import SeamServer
from research_core.store import coworlds, notices, sessions
from research_core.store.actors import Actor
from research_core.tools.base import ToolContext, run_tool
from research_core.watcher import context as watcher_context
from research_core.watcher.hermes_api import FakeHermesRuns
from tests.fixtures.fakegame_repo import COWORLD_ID, make_fakegame_repo


class Scenario:
    def __init__(self, tmp_path: Path) -> None:
        self.env = {
            "IDE_WORKSPACE": str(tmp_path / "ws"),
            "IDE_PLATFORM": "fake",
            "IDE_COWORLD_ID": COWORLD_ID,
            "IDE_RESEARCHER_PROFILES": "researcher",
            "SOFTMAX_PLAYER_ID": "ply_test",
        }
        self.observed_at = datetime.now(UTC)
        self.ws = layout.from_env(self.env)
        self.ws.ensure_dirs()
        source, commit = make_fakegame_repo(tmp_path / "upstream")
        self.agent = ScriptedAgent(self.env, source, commit, coworld_id=COWORLD_ID)
        self.hermes = FakeHermesRuns(script=self.agent)
        self.agent.hermes = self.hermes
        self.server = SeamServer(ws=self.ws, hermes=self.hermes, env=self.env)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = ScriptedClient(self.server.url)
        self.session_id = self.client.open(
            "scripted", "web-ide", session_context={"contract_version": 1, "coworld": COWORLD_ID}
        )

    def tick(self) -> dict:
        self.observed_at += timedelta(seconds=60)
        ctx = watcher_context.build(self.env, hermes=self.hermes, now=self.observed_at)
        try:
            self.server.coordinator.reconcile()
            report = self.server.coordinator.advance(ctx)
            asyncio.run(self.server.dispatch_messages())
        finally:
            ctx.wstore.close()
            if ctx.store is not None:
                ctx.store.close()
        return report.to_dict() if hasattr(report, "to_dict") else report

    def starts(self) -> int:
        return sum(1 for method, _ in self.hermes.calls if method == "start")

    def ctx(self) -> ToolContext:
        ctx = ToolContext.from_env("orchestrator", {**self.env, "IDE_SESSION_ID": "test-harness"})
        sessions.register(
            ctx.wstore,
            Actor.harness(),
            session_id="test-harness",
            profile="orchestrator",
            kind="chat",
            coworld_id=COWORLD_ID,
        )
        return ctx

    def platform(self) -> FakePlatform:
        return FakePlatform(self.ws.state / "fake_platform.json", self.env)

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def scenario(tmp_path):
    scenario = Scenario(tmp_path)
    yield scenario
    scenario.close()


def submit_the_starter(scenario: Scenario) -> str:
    """The user asks on a new workspace; the chat run switches, the ticks run setup; returns
    the run id of the user's message."""
    reply = scenario.client.say(REQUEST, {"contract_version": 1, "on_screen": []}, wait=True)
    run_id = reply["run_id"]
    assert scenario.hermes.runs[run_id].status == "completed"
    for _ in range(3):  # the setup wake starts on the first tick; later ticks settle it
        scenario.tick()
    return run_id


def test_acceptance_submit_starter(scenario):
    submit_the_starter(scenario)
    ctx = scenario.ctx()

    # The fake platform holds a submission of the uploaded starter.
    submissions = scenario.platform().submissions(mine=True)
    assert len(submissions) == 1, submissions
    read = json.loads(run_tool("version", {"action": "read", "version": "V-1"}, ctx))
    assert read["ok"] is True, read
    record = read["record"]
    assert record["uploaded"]["policy_version_id"] == submissions[0].policy_version_id
    assert record["submission_status"] in ("submitted", "qualifying", "competing", "champion")

    # The Coworld is active, the request reached the user as a notice, and the submission
    # event cites the user's message.
    assert coworlds.active(ctx.wstore) is None
    texts = [n["text"] for n in notices.unread(ctx.wstore, limit=20, coworld_id=COWORLD_ID)]
    assert any("Submitted V-1" in t for t in texts), texts
    submitted = [
        json.loads(row["payload"])
        for row in ctx.store.q("SELECT payload FROM events WHERE kind = 'version.submitted'")
    ]
    assert submitted and submitted[0]["cites"] == CITES and submitted[0]["user_requested"]
    # The setup wake ran in its own Session of the Orchestrator profile, not in the chat.
    wake_runs = [r for r in scenario.hermes.runs.values() if "# Setup wake" in r.input]
    assert len(wake_runs) == 1 and wake_runs[0].session_id != scenario.session_id
    assert wake_runs[0].status == "completed"
    # Every other run the watcher started was a wake in its own Session, never the chat.
    chat_runs = [r for r in scenario.hermes.runs.values() if r.session_id == scenario.session_id]
    assert len(chat_runs) == 1, [r.input[:60] for r in chat_runs]
    assert all("wake" in line.lower() for line in scenario.agent.other_wakes), (
        scenario.agent.other_wakes
    )


def test_notice_opens_next_turn(scenario):
    submit_the_starter(scenario)
    reply = scenario.client.say("thanks", wait=True)
    rendered = scenario.agent.rendered[reply["run_id"]]
    assert "[notices since your last turn]" in rendered
    assert "Submitted V-1" in rendered
    assert "[session Coworld] cow_fake" in rendered
    # Read once: the next turn does not repeat it.
    again = scenario.client.say("anything else?", wait=True)
    assert "Submitted V-1" not in scenario.agent.rendered[again["run_id"]]


def test_concurrent_chat_and_wakes_never_touch_the_chat_session(scenario):
    """A6.1 and B.13: while the chat run is live, the watcher's wakes run in their own
    Sessions, and a second user message steers the live run instead of starting another."""
    scenario.agent.hold_chat_runs = True
    first = scenario.client.say(REQUEST, {"contract_version": 1, "on_screen": []}, wait=True)
    assert first["delivered"] == "queued"
    assert scenario.hermes.runs[first["run_id"]].status == "running"

    for _ in range(3):
        scenario.tick()
    wake_runs = [r for r in scenario.hermes.runs.values() if r.session_id != scenario.session_id]
    assert any("# Setup wake" in r.input for r in wake_runs), [r.input[:40] for r in wake_runs]
    assert all(r.status == "completed" for r in wake_runs)
    assert len(scenario.platform().submissions(mine=True)) == 1  # the pending request ran

    second = scenario.client.say("Also: who is the champion right now?")
    assert second["delivered"] == "queued" and second["run_id"] is None
    chat_runs = [r for r in scenario.hermes.runs.values() if r.session_id == scenario.session_id]
    assert len(chat_runs) == 1

    scenario.hermes.complete(first["run_id"], "Done.")
    scenario.agent.hold_chat_runs = False
    third = scenario.client.say("thanks", wait=True)
    assert third["delivered"] == "queued" and third["run_id"] != first["run_id"]
    dispatched_second = scenario.client.wait_admission(second["op_id"])
    assert "Submitted V-1" in scenario.agent.rendered[dispatched_second["run_id"]]


def test_management_wake_without_heading_preserves_chat_notices(scenario):
    ctx = scenario.ctx()
    wake_id = "management-wake"
    sessions.register(
        ctx.wstore,
        Actor.harness(),
        session_id=wake_id,
        profile="orchestrator",
        kind="wake",
        coworld_id=COWORLD_ID,
    )
    notices.add(ctx.wstore, Actor.harness(), text="Game news", coworld_id=COWORLD_ID)
    run_id = scenario.hermes.start(
        profile="orchestrator",
        session_id=wake_id,
        text="Assign research work using task.assign.",
    )
    assert run_id not in scenario.agent.rendered
    assert scenario.hermes.runs[run_id].status == "completed"
    assert [n["text"] for n in notices.unread(ctx.wstore, coworld_id=COWORLD_ID)] == ["Game news"]
