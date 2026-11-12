"""``research serve``, the local seam server (plan Task 6.2, design §10, A.4, A.11, A6.1).

Every test runs a :class:`SeamServer` on a background thread against :class:`FakeHermesRuns`
and a fresh workspace with ``cow_fake`` active, and drives it through the
:class:`ScriptedClient` (Task 6.3), so the client is exercised by the same tests.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from research_core import intake, serve, telemetry
from research_core.client import ScriptedClient, SeamError
from research_core.serve import SeamServer
from research_core.store import admissions, coworlds, goals, notices, sessions, tasks, ui_state
from research_core.store.actors import Actor
from research_core.store.db import Store, Tx
from research_core.tools.base import ToolContext
from research_core.watcher.hermes_api import FakeHermesRuns

HARNESS = Actor.harness()
COWORLD_ID = "cow_fake"


@dataclass
class Seam:
    server: SeamServer
    hermes: FakeHermesRuns
    ctx: ToolContext
    client: ScriptedClient

    @property
    def wstore(self) -> Store:
        return self.ctx.wstore

    @property
    def store(self) -> Store:
        return self.ctx.store

    def run_input(self, run_id: str) -> str:
        return self.hermes.runs[run_id].input

    def starts(self) -> int:
        return sum(1 for method, _ in self.hermes.calls if method == "start")

    def say(self, text, context=None):
        receipt = self.client.say(text, context)
        return receipt | self.client.wait_admission(receipt["op_id"])


def make_ctx(tmp_path, coworld: bool = True) -> ToolContext:
    ctx = ToolContext.from_env(
        "orchestrator",
        {
            "IDE_WORKSPACE": str(tmp_path),
            "IDE_SESSION_ID": "test-harness",
            "IDE_COWORLD_ID": COWORLD_ID if coworld else "",
        },
    )
    ctx.workspace.ensure_dirs()
    if coworld:
        coworlds.register(ctx.wstore, HARNESS, COWORLD_ID, folder=COWORLD_ID)
        coworlds.activate(ctx.wstore, HARNESS, COWORLD_ID)
        ctx.workspace.coworld(COWORLD_ID).ensure_dirs()
    return ctx


def start_seam(tmp_path, *, coworld: bool = True) -> Seam:
    ctx = make_ctx(tmp_path, coworld=coworld)
    hermes = FakeHermesRuns()
    server = SeamServer(ws=ctx.workspace, hermes=hermes, env=dict(ctx.env))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return Seam(server, hermes, ctx, ScriptedClient(server.url))


@pytest.fixture
def seam(tmp_path):
    seam = start_seam(tmp_path)
    yield seam
    seam.server.shutdown()


def context_block(seam, run_id) -> dict:
    row = seam.wstore.one("SELECT payload FROM client_admissions WHERE run_id = ?", (run_id,))
    return json.loads(row["payload"])["context"]


# --- session-open and message -------------------------------------------------------------


@pytest.mark.parametrize("origin", ["answer", "continuation", "planning", "wake"])
def test_coordinator_dispatches_internal_intents_alongside_client_inputs(tmp_path, origin):
    ctx = make_ctx(tmp_path)
    native = FakeHermesRuns()
    server = SeamServer(ws=ctx.workspace, hermes=native)
    try:
        sessions.register(
            ctx.wstore, HARNESS, session_id="worker", profile="orchestrator", kind="chat"
        )
        op_id = admissions.enqueue(
            ctx.wstore,
            HARNESS,
            key="internal",
            session_id="worker",
            profile="orchestrator",
            text="Internal work",
            origin=origin,
        )
        server.session_open(
            {
                "op_id": str(uuid4()),
                "session_id": "chat",
                "client": {"id": "test", "kind": "test"},
            }
        )
        receipt = server.message(
            {
                "op_id": str(uuid4()),
                "session_id": "chat",
                "message": {"role": "user", "content": "Client work"},
            }
        )
        asyncio.run(server.dispatch_messages())
        row = ctx.wstore.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
        assert row["status"] == "running" and row["dispatch_started"]
        assert row["run_id"] is not None
        assert (
            ctx.wstore.one(
                "SELECT status FROM client_admissions WHERE op_id=?", (receipt["op_id"],)
            )[0]
            == "running"
        )
        assert sorted(run.input for run in native.runs.values()) == ["Client work", "Internal work"]
        assert {row["session_id"] for row in admissions.snapshot(ctx.wstore)} == (
            {"chat", "worker"} if origin == "answer" else {"chat"}
        )
    finally:
        server._http.server_close()
        ctx.store.close()
        ctx.wstore.close()


def test_session_open_registers_a_chat_session_with_its_context(seam, monkeypatch):
    created = threading.Event()
    original = seam.hermes.create_session

    def native_open(*args, **kwargs):
        original(*args, **kwargs)
        created.set()

    monkeypatch.setattr(seam.hermes, "create_session", native_open)
    session_id = seam.client.open(
        client_id="ide-1",
        kind="ide",
        guide="client-ide",
        session_context={
            "contract_version": 1,
            "coworld": COWORLD_ID,
            "mode": {"name": "research"},
        },
        capabilities={"views": ["replay"], "panels": ["markdown"]},
    )
    assert session_id.startswith("api_")
    from research_core.store import sessions

    row = sessions.read(seam.wstore, session_id)
    assert row["kind"] == "chat" and row["profile"] == "orchestrator"
    assert row["coworld_id"] == COWORLD_ID
    assert row["session_context"] == {
        "contract_version": 1,
        "coworld": COWORLD_ID,
        "mode": {"name": "research"},
    }
    assert row["client_capabilities"] == {"views": ["replay"], "panels": ["markdown"]}
    assert row["client"] == {"id": "ide-1", "kind": "ide"}
    assert created.wait(3)
    assert session_id in seam.hermes.sessions
    assert seam.hermes.session_messages(session_id, profile="orchestrator") == []


def test_session_open_refuses_a_missing_client(seam):
    with pytest.raises(SeamError) as info:
        seam.client._post("/session-open", {"capabilities": {}})
    assert info.value.status == 400 and "client" in info.value.body["error"]


@pytest.mark.parametrize("stalled", ["prepare", "start", "create_session"])
def test_intake_is_durable_while_execution_is_stalled(seam, monkeypatch, stalled):
    from research_core import attachments

    entered, release = threading.Event(), threading.Event()
    owner = attachments if stalled == "prepare" else seam.hermes
    original = getattr(owner, stalled)
    calls = []

    def held(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(15), "Stalled execution was never released"
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, stalled, held)
    with ThreadPoolExecutor(max_workers=6) as callers:
        try:
            if stalled == "create_session":
                triggering = callers.submit(seam.client.open)
            else:
                seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
                triggering = callers.submit(seam.client.say, "Begin the stalled execution")
            assert entered.wait(3)
            started = time.monotonic()
            opened = callers.submit(
                seam.client._post,
                "/session-open",
                {"op_id": str(uuid4()), "client": {"id": "independent-intake", "kind": "test"}},
            ).result(timeout=1)
            body = {
                "op_id": str(uuid4()),
                "session_id": opened["session_id"],
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "file", "file": {"file_id": "a" * 64, "filename": "queued.txt"}}
                    ],
                },
            }
            retries = [callers.submit(seam.client._post, "/message", body) for _ in range(4)]
            replies = [request.result(timeout=1) for request in retries]
            assert time.monotonic() - started < 1
            assert all(
                reply["op_id"] == body["op_id"] and reply["run_id"] is None for reply in replies
            )
            # A separate connection proves committed bytes before execution is released.
            with Store.open(seam.ctx.workspace.store_path, kind="workspace") as store:
                rows = store.q(
                    "SELECT request,status FROM client_admissions WHERE op_id=?", (body["op_id"],)
                )
                assert len(rows) == 1 and rows[0]["status"] == "queued"
                assert json.loads(rows[0]["request"]) == body
            assert not release.is_set()
            assert len(calls) == 1, "Execution preparation must remain serial"
        finally:
            release.set()
        triggering.result(timeout=3)


def test_new_session_history_and_dispatch_wait_for_native_open(seam, monkeypatch):
    entered, release, started = threading.Event(), threading.Event(), threading.Event()
    create = seam.hermes.create_session
    start = seam.hermes.start

    def held_open(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return create(*args, **kwargs)

    def checked_start(*args, **kwargs):
        assert kwargs["session_id"] in seam.hermes.sessions
        result = start(*args, **kwargs)
        started.set()
        return result

    monkeypatch.setattr(seam.hermes, "create_session", held_open)
    monkeypatch.setattr(seam.hermes, "start", checked_start)

    try:
        first = seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
        assert entered.wait(3)
        # Accepted after the dispatcher's open-query snapshot: it must not run early.
        second = seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
        seam.client._post(
            "/message",
            {
                "op_id": str(uuid4()),
                "session_id": second,
                "message": {"role": "user", "content": "Wait for native creation"},
            },
        )
        for session_id in (first, second):
            with urllib.request.urlopen(
                f"{seam.server.url}/session-pending?session_id={session_id}", timeout=1
            ) as result:
                assert json.load(result) == {"pending": True}
        assert not started.is_set()
    finally:
        release.set()
    assert started.wait(3)


def test_message_stamps_client_coworld_and_version_into_the_block(seam, monkeypatch):
    # Observe every commit from another connection, including the gap between separate writes.
    committed_runs = []
    original_exit = Tx.__exit__

    def observe_commit(tx, *args):
        result = original_exit(tx, *args)
        if tx.actor == Actor.user("client-dispatch"):
            with sqlite3.connect(tx.store.path) as reader:
                committed_runs.extend(
                    reader.execute(
                        "SELECT a.run_id, s.run_id FROM client_admissions a"
                        " JOIN sessions s ON s.id=a.session_id WHERE a.status='running'"
                    ).fetchall()
                )
        return result

    monkeypatch.setattr(Tx, "__exit__", observe_commit)
    seam.client.open(
        client_id="coplay-7",
        kind="desktop",
        guide="client-coplay",
        session_context={"contract_version": 1, "coworld": COWORLD_ID},
    )
    result = seam.say(
        "what is the champion?",
        context={"contract_version": 1, "mode": {"name": "coach", "instruction": "brief"}},
    )
    assert result["delivered"] == "queued" and result["problems"] == []
    text = seam.run_input(result["run_id"])
    assert text == "what is the champion?"
    block = context_block(seam, result["run_id"])
    assert block["client"] == {"id": "coplay-7", "kind": "desktop", "guide": "client-coplay"}
    assert block["coworld"] == COWORLD_ID
    assert block["contract_version"] == 1
    assert block["mode"] == {"name": "coach", "instruction": "brief"}
    # Only the server's three stamps: the turn hook adds notices and the changed-since note.
    assert set(block) == {"client", "coworld", "contract_version", "mode"}
    from research_core.store import sessions

    assert sessions.read(seam.wstore, result["session_id"])["run_id"] == result["run_id"]
    assert committed_runs
    assert all(admission == session for admission, session in committed_runs)


def test_message_without_a_context_still_carries_the_stamps(seam):
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    result = seam.say("hello")
    block = context_block(seam, result["run_id"])
    assert block == {
        "client": {"id": "scripted", "kind": "test"},
        "coworld": COWORLD_ID,
        "contract_version": 1,
    }


def test_malformed_context_yields_200_with_problems_and_the_run_still_starts(seam):
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    result = seam.say(
        "go",
        context={
            "contract_version": 1,
            "bogus": 1,
            "mode": {"name": "x" * 41},
            "on_screen": ["G-1"],
            "client": {"id": "spoofed", "kind": "other"},
        },
    )
    assert result["delivered"] == "queued"
    assert any("bogus" in p for p in result["problems"])
    assert any("at most 40 characters" in p for p in result["problems"])
    block = context_block(seam, result["run_id"])
    assert "mode" not in block and block["on_screen"] == ["G-1"]
    assert block["client"] == {"id": "scripted", "kind": "test"}  # the server's stamp wins
    # A context that is not even an object: the stamps alone make the block.
    seam.hermes.complete(result["run_id"])
    result = seam.client._post(
        "/message",
        {
            "session_id": seam.client.session_id,
            "message": {"role": "user", "content": "x"},
            "context": "junk",
        },
    )
    assert result["delivered"] == "queued"
    assert result["problems"] == ["context: must be a JSON object"]
    result |= seam.client.wait_admission(result["op_id"])
    assert context_block(seam, result["run_id"])["coworld"] == COWORLD_ID


def test_client_showing_another_coworld_is_refused(seam):
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    with pytest.raises(SeamError) as failure:
        seam.say("hi", context={"contract_version": 1, "coworld": "cow_other"})
    assert failure.value.status == 409


def test_workspace_session_cannot_acquire_a_game_from_a_message(tmp_path):
    seam = start_seam(tmp_path, coworld=False)
    try:
        seam.client.open(session_context={"contract_version": 1, "scope": "workspace"})
        with pytest.raises(SeamError) as failure:
            seam.say("set up pudge", context={"contract_version": 1, "coworld": "cow_p"})
        assert failure.value.status == 409
        assert seam.client.health()["coworld"] is None
    finally:
        seam.server.shutdown()


def test_second_message_waits_until_the_previous_run_finishes(seam):
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    first = seam.say("start the work")
    second = seam.client.say("and also check the forum")
    assert second["delivered"] == "queued" and second["run_id"] is None
    assert seam.starts() == 1
    seam.hermes.complete(first["run_id"], "done")
    dispatched = seam.client.wait_admission(second["op_id"])
    assert dispatched["run_id"] != first["run_id"]
    assert seam.starts() == 2


def test_message_to_an_unknown_session_is_404(seam):
    with pytest.raises(SeamError) as info:
        seam.client._post(
            "/message",
            {
                "session_id": "api_0_nope",
                "message": {"role": "user", "content": "x"},
            },
        )
    assert info.value.status == 404


def test_busy_gateway_preserves_confirmed_message_until_available(seam):
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    seam.hermes.set_busy(True)
    receipt = seam.client.say("hello")
    assert receipt["delivered"] == "queued"
    with pytest.raises(TimeoutError):
        seam.client.wait_admission(receipt["op_id"], timeout=0.7)
    seam.hermes.set_busy(False)
    assert seam.client.wait_admission(receipt["op_id"])["status"] == "running"


# --- stream and snapshot --------------------------------------------------------------------


def test_stream_delivers_new_events_as_sse_with_ids_and_resumes(seam):
    before = seam.client.events("workspace", after=0)
    assert before and before[-1]["kind"] == "coworld.switched"
    assert [e["seq"] for e in before] == list(range(1, len(before) + 1))
    assert all(e["store"] == "workspace" for e in before)
    last = before[-1]["seq"]
    notices.add(seam.wstore, HARNESS, text="Submitted V-1", refs=["V-1"])
    new = seam.client.events("workspace", after=last)
    assert [e["kind"] for e in new] == ["notice"]
    assert new[0]["seq"] == last + 1 and new[0]["payload"]["text"] == "Submitted V-1"
    assert seam.client.events("workspace", after=new[0]["seq"]) == []


def test_stream_raw_frames_and_last_event_id_header(seam):
    notices.add(seam.wstore, HARNESS, text="one")
    notices.add(seam.wstore, HARNESS, text="two")
    last = seam.wstore.last_seq()
    request = urllib.request.Request(f"{seam.server.url}/stream?store=workspace&after=0")
    request.add_header("Last-Event-ID", str(last - 1))
    with urllib.request.urlopen(request, timeout=2) as response:
        assert response.headers["Content-Type"].startswith("text/event-stream")
        lines = []
        while len(lines) < 2:  # the first frame's id and data; comments and blanks skipped
            line = response.readline().decode()
            if line.strip() and not line.startswith(":"):
                lines.append(line)
    frame = lines
    assert frame[0] == f"id: {last}\n"
    assert frame[1].startswith("data: ")
    assert json.loads(frame[1][len("data: ") :])["payload"]["text"] == "two"


def test_stream_of_the_coworld_store_and_snapshot_seq_matches(seam):
    goals.create(seam.store, HARNESS, title="Win", end_state="margin > 0", reason="r")
    events = seam.client.events("coworld", after=0, coworld=COWORLD_ID)
    assert [e["kind"] for e in events] == ["goal.created"]
    assert events[0]["store"] == "coworld"
    snapshot = seam.client.snapshot("coworld", coworld=COWORLD_ID)
    assert snapshot["store"] == "coworld" and snapshot["coworld_id"] == COWORLD_ID
    assert snapshot["seq"] == events[-1]["seq"]
    assert snapshot["records"]["goals"][0]["title"] == "Win"
    workspace = seam.client.snapshot("workspace")
    assert workspace["seq"] == seam.client.events("workspace", after=0)[-1]["seq"]
    assert workspace["records"]["coworlds"][0]["coworld_id"] == COWORLD_ID


def test_stream_and_snapshot_refuse_bad_store_names(seam):
    with pytest.raises(SeamError) as info:
        seam.client.snapshot("nope")
    assert info.value.status == 400
    with pytest.raises(SeamError) as info:
        seam.client.events("workspace", after="x")
    assert info.value.status == 400


def test_coworld_stream_without_an_active_coworld_is_404(tmp_path):
    seam = start_seam(tmp_path, coworld=False)
    try:
        with pytest.raises(SeamError) as info:
            seam.client.snapshot("coworld")
        assert info.value.status == 400 and "coworld is required" in info.value.body["error"]
    finally:
        seam.server.shutdown()


# --- UI actions ----------------------------------------------------------------------------


def open_ask(seam, conversation: str, ask_id: str = "ask_1") -> None:
    ui_state.patch(
        seam.wstore,
        HARNESS,
        conversation,
        [
            {"op": "add", "path": "/asks", "value": {}},
            {
                "op": "add",
                "path": f"/asks/{ask_id}",
                "value": {"question": "Submit V-2?", "options": ["yes", "no"], "status": "open"},
            },
        ],
    )


def test_ui_action_answer_is_durable_and_steers_the_live_run(seam):
    session_id = seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    run = seam.say("propose a submission")
    open_ask(seam, session_id)
    result = seam.client.answer("ask_1", "yes")
    assert result["accepted"] is True and result["delivered"] == "queued"
    assert result["conversation"] == session_id
    ask = ui_state.get(seam.wstore, session_id)["doc"]["asks"]["ask_1"]
    assert ask["status"] == "answered" and ask["answer"] == "yes"
    assert ask["answered_by"] == "user:local"
    assert seam.client.answer("ask_1", "yes")["op_id"] == result["op_id"]
    delivered = seam.client.wait_admission(result["op_id"])
    assert delivered["run_id"] == run["run_id"]
    assert seam.hermes.steered[run["run_id"]] == [
        "[UI action] answer to ask_1: yes\nThe question: Submit V-2?"
    ]
    assert seam.starts() == 1


def test_ui_action_answer_starts_a_run_when_none_is_live_and_finds_workspace_asks(seam):
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    open_ask(seam, "coworld:" + COWORLD_ID, "ask_w")
    result = seam.client.answer("ask_w", {"choice": "no"})
    assert result["delivered"] == "queued" and result["conversation"] == "coworld:" + COWORLD_ID
    delivered = seam.client.wait_admission(result["op_id"])
    assert seam.run_input(delivered["run_id"]) == (
        '[UI action] answer to ask_w: {"choice": "no"}\nThe question: Submit V-2?'
    )
    assert ui_state.get(seam.wstore, "coworld:" + COWORLD_ID)["doc"]["asks"]["ask_w"]["answer"] == {
        "choice": "no"
    }
    with pytest.raises(SeamError) as info:  # the first answer wins
        seam.client.answer("ask_w", "yes")
    assert info.value.status == 409
    with pytest.raises(SeamError) as info:
        seam.client.answer("ask_missing", "yes")
    assert info.value.status == 404


def test_reaction_writes_a_ui_state_change_and_starts_no_run(seam):
    session_id = seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    seq = seam.wstore.last_seq()
    result = seam.client.react("msg-42", "thumbs_up")
    assert result["accepted"] is True and result["delivered"] == "none"
    assert seam.starts() == 0 and seam.hermes.runs == {}
    events = seam.wstore.events_after(seq)
    assert [e.kind for e in events] == ["ui.patch"]
    assert events[0].payload["ops"][-1]["path"] == "/reactions/msg-42"
    doc = ui_state.get(seam.wstore, session_id)["doc"]
    assert doc["reactions"]["msg-42"]["value"] == "thumbs_up"
    assert doc["reactions"]["msg-42"]["by"] == "user:local"
    seam.client.react("msg-43", "thumbs_down")
    assert set(ui_state.get(seam.wstore, session_id)["doc"]["reactions"]) == {"msg-42", "msg-43"}


def test_ui_action_unknown_action_and_bad_payload(seam):
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    with pytest.raises(SeamError) as info:
        seam.client._post(
            "/ui-action", {"session_id": seam.client.session_id, "action": "dance", "payload": {}}
        )
    assert info.value.status == 400 and "answer, reaction" in info.value.body["error"]
    with pytest.raises(SeamError) as info:
        seam.client._post(
            "/ui-action", {"session_id": seam.client.session_id, "action": "answer", "payload": {}}
        )
    assert info.value.status == 400 and "ask_id" in info.value.body["error"]


# --- store apply ----------------------------------------------------------------------------


def propose_task(seam) -> dict:
    goals.create(seam.store, HARNESS, title="Win", end_state="margin > 0", reason="r")
    return tasks.create(
        seam.store,
        Actor.session("api_1_researcher"),
        type="research",
        title="Try a wider search",
        description="d",
        goal_id=1,
        status="proposed",
    )


def test_store_apply_with_a_user_actor_approves_a_proposed_task(seam):
    task = propose_task(seam)
    assert task["status"] == "proposed"
    result = seam.client.apply(
        "user:james", "tasks.approve", {"task_id": "T-1"}, coworld=COWORLD_ID
    )
    assert result["ok"] is True and result["store"] == "coworld"
    assert result["record"]["status"] == "unclaimed"
    assert result["record"]["approved_by"] == "user:james"
    assert result["seq"] == seam.store.last_seq()
    assert seam.store.events_after(result["seq"] - 1)[0].kind == "transition.applied"


def test_store_apply_refuses_outside_the_allow_list_and_session_actors(seam):
    propose_task(seam)
    with pytest.raises(SeamError) as info:
        seam.client.apply("user:james", "goals.create", {"title": "x"})
    assert info.value.status == 400 and "not an op store apply runs" in info.value.body["error"]
    with pytest.raises(SeamError) as info:
        seam.client.apply("session:api_1_x", "tasks.approve", {"task_id": 1})
    assert info.value.status == 400 and "sessions use the tools" in info.value.body["error"]
    with pytest.raises(SeamError) as info:
        seam.client.apply("user:james", "tasks.approve", {"task_id": "G-1"})
    assert info.value.status == 400 and info.value.body["problems"]
    assert tasks.read(seam.store, 1)["status"] == "proposed"


def test_store_apply_pause_writes_the_workspace_setting(seam):
    result = seam.client.apply("user:james", "workspace.pause", {"cites": "ui:pause-button"})
    assert result["store"] == "workspace" and result["record"]["value"] is True
    from research_core.store import settings

    assert settings.get(seam.wstore, "paused") is True


# --- the rest -----------------------------------------------------------------------------


def test_health_and_run_status(seam):
    health = seam.client.health()
    assert health == {
        "ok": True,
        "profile": "orchestrator",
        "coworld": None,
        "contract_version": 1,
    }
    seam.client.open(session_context={"contract_version": 1, "coworld": None})
    run = seam.say("hi")
    status = seam.client.run(run["run_id"])
    assert status["status"] == "running" and status["terminal"] is False
    seam.hermes.complete(run["run_id"], "all done")
    status = seam.client.run(run["run_id"])
    assert status["status"] == "completed" and status["output"] == "all done"
    assert seam.client.run("run_missing")["status"] == "unknown"


def test_client_page_is_served_at_root_and_ui(seam):
    for path in ("/", "/ui"):
        with urllib.request.urlopen(f"{seam.server.url}{path}", timeout=2) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "text/html; charset=utf-8"
            page = response.read().decode("utf-8")
        assert "<title>Orchestrator desk</title>" in page
        # The page opens a Session on load and reads the two routes added for it.
        assert "/session-open" in page and "/status" in page and "/stream?store=" in page
        # Both directive id forms link to the Directives tab.
        assert 'D: "directives", W: "directives"' in page and "[GTVCXDWN]" in page


def test_status_reports_the_watcher_fields_from_the_workspace_store(seam, monkeypatch, capsys):
    from research_core.store import settings
    from research_core.store._common import now_iso
    from research_core.watcher import cli as watcher_cli

    assert seam.client._get("/status") == {
        "ok": True,
        "heartbeat": None,
        "heartbeat_age_s": None,
        "has_work": False,
        "waiting_on": [],
        "paused": False,
        "paused_drained": False,
        "sleep_started": None,
        "blocked_executions": [],
        "coordinator_instance": seam.server.coordinator.instance_id,
        "restart_required": False,
        "runtime_recovery_count": 0,
        "pending_transitions": [],
        "work": [],
    }
    seam.wstore.set_meta("heartbeat", now_iso())
    seam.wstore.set_meta("has_work", "1")
    seam.wstore.set_meta("has_work_reasons", json.dumps(["wake:queued", "task:T-1 xp"]))
    seam.wstore.set_meta("paused_drained", "1")
    settings.update(seam.wstore, HARNESS, "paused", True, cites="test")
    status = seam.client._get("/status")
    assert status["has_work"] is True and status["waiting_on"] == ["wake:queued", "task:T-1 xp"]
    assert status["paused"] is True and status["paused_drained"] is True
    assert 0 <= status["heartbeat_age_s"] < 60
    # It is what ``research watcher status`` prints for the same workspace.
    monkeypatch.setenv("IDE_WORKSPACE", str(seam.ctx.workspace.root))
    assert watcher_cli.run_status(None) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == set(status)
    assert {k: v for k, v in printed.items() if k != "heartbeat_age_s"} == {
        k: v for k, v in status.items() if k != "heartbeat_age_s"
    }


def test_client_page_awaits_only_functions_it_defines():
    """A missing function surfaces only as a false refusal in the catch around the call."""
    page = (Path(serve.__file__).parent / "ui" / "index.html").read_text()
    awaited = set(re.findall(r"\bawait (\w+)\(", page))
    defined = set(re.findall(r"\bfunction (\w+)\(", page)) | set(
        re.findall(r"\bconst (\w+) = (?:async )?\(", page)
    )
    assert awaited - defined <= {"fetch"}


def test_unknown_get_paths_still_404_beside_the_page(seam):
    for path in ("/nowhere", "/ui/anything", "/index.html", "/ui.html"):
        with pytest.raises(SeamError) as info:
            seam.client._get(path)
        assert info.value.status == 404, path


def test_fresh_workspace_storage_is_ready_before_concurrent_workers_start(tmp_path):
    ctx = make_ctx(tmp_path, coworld=False)
    assert not ctx.workspace.store_path.exists()
    server = SeamServer(ws=ctx.workspace, hermes=FakeHermesRuns(), env=dict(ctx.env))
    # Existing fixtures register a Coworld first, hiding first-open SQLite journal/schema races.
    assert ctx.workspace.store_path.exists()
    with server.stores() as stores:
        assert stores.wstore.schema_status().ok
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(SeamError) as info:
            ScriptedClient(server.url)._get("/nowhere")
        assert info.value.status == 404
    finally:
        server.shutdown()


def test_unknown_routes_and_bad_json_are_json_errors(seam):
    with pytest.raises(SeamError) as info:
        seam.client._get("/nowhere")
    assert info.value.status == 404
    request = urllib.request.Request(f"{seam.server.url}/message", data=b"{not json", method="POST")
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=2)
    assert info.value.code == 400
    assert "JSON" in json.loads(info.value.read())["error"]


def test_serve_command_is_registered():
    from research_core import cli

    parser = cli.build_parser()
    args = parser.parse_args(["serve", "--port", "0", "--profile", "orchestrator"])
    assert args.port == 0 and args.profile == "orchestrator" and callable(args.run)


def test_text_message_survives_admission_retry_and_native_history(seam):
    from uuid import uuid4

    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    message = {"role": "user", "content": "Continue this chat without changing its history."}
    body = {
        "op_id": str(uuid4()),
        "session_id": seam.client.session_id,
        "message": message,
        "context": {"contract_version": 1},
    }
    receipt = seam.client._post("/message", body)
    admitted = seam.client.wait_admission(receipt["op_id"])
    assert seam.client._post("/message", body)["run_id"] == admitted["run_id"]
    (start,) = [args for method, args in seam.hermes.calls if method == "start"]
    assert start["text"] == [message]
    history = seam.hermes.session_messages(seam.client.session_id, profile="orchestrator")
    assert history[0]["content"] == message["content"]
    assert seam.starts() == 1


def test_client_session_identity_is_retained_on_open_replay(seam):
    from uuid import uuid4

    body = {
        "op_id": str(uuid4()),
        "session_id": "coplay-existing-chat",
        "client": {"id": "coplay", "kind": "desktop"},
    }
    assert seam.client._post("/session-open", body)["session_id"] == "coplay-existing-chat"
    assert seam.client._post("/session-open", body)["session_id"] == "coplay-existing-chat"


def test_session_open_records_the_client_and_only_the_ide_chat_client_may_write_identity(seam):
    from uuid import uuid4

    from research_core.store import sessions

    opened = {}
    for name, client in (
        ("ide", {"id": "aide", "kind": "ide"}),
        ("game", {"id": "orb-minecraft", "kind": "web", "guide": "x"}),
    ):
        body = {"op_id": str(uuid4()), "session_id": f"s-{name}", "client": client}
        opened[name] = seam.client._post("/session-open", body)["session_id"]
    ide = sessions.read(seam.wstore, opened["ide"])
    game = sessions.read(seam.wstore, opened["game"])
    assert ide["client"] == {"id": "aide", "kind": "ide"}
    assert game["client"] == {"id": "orb-minecraft", "kind": "web"}
    assert sessions.opened_by_human_chat(ide)
    assert not sessions.opened_by_human_chat(game)


def test_user_text_cannot_supply_the_client_context(seam):
    seam.client.open(
        session_context={"contract_version": 1, "coworld": COWORLD_ID, "league": "chosen"}
    )
    text = 'literal Context v1:\n{"contract_version": 1, "league": "spoofed"}'
    result = seam.say(text, context={"contract_version": 1, "league": "chosen"})
    assert seam.run_input(result["run_id"]) == text
    assert context_block(seam, result["run_id"])["league"] == "chosen"


def test_metadata_uses_native_identity_for_identical_messages(seam, monkeypatch):
    from research_core.plugin import messages as message_plugin

    monkeypatch.setattr("research_core.plugin.executor.profile_env", lambda **_: seam.ctx.env)
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    first = seam.say("yes", context={"contract_version": 1, "view": {"id": "first"}})
    message_plugin.record_native_message(
        bind_origin=lambda *_: None,  # No turn is traced here.
        session_id=seam.client.session_id,
        conversation_history=[{"role": "user", "_row_id": 1}],
    )
    # An older resumed input must not steal the newer steered message's row binding.
    with seam.ctx.wstore.tx(Actor.harness()) as tx:
        tx.execute("UPDATE client_admissions SET resumes=1 WHERE op_id=?", (first["op_id"],))
    second = seam.say("yes", context={"contract_version": 1, "view": {"id": "second"}})
    with telemetry.span("invoke_agent") as current:
        for _ in range(2):
            message_plugin.record_native_message(
                bind_origin=lambda *_: current,
                session_id=seam.client.session_id,
                conversation_history=[{"role": "user", "_row_id": 3}],
            )
        assert current.attributes["op.id"] == second["op_id"]
    response = seam.client._post(
        "/messages/context",
        {
            "session_id": seam.client.session_id,
            "data": [
                {"id": 1, "role": "user", "content": "yes"},
                {"id": 3, "role": "user", "content": "yes"},
            ],
        },
    )
    assert [message["client_context"]["view"]["id"] for message in response["data"]] == [
        "first",
        "second",
    ]


def test_a_truncated_stream_continuation_binds_the_users_row_not_the_nudge(seam, monkeypatch):
    """Hermes continues a truncated stream with a user nudge it never commits, so the nudge has no
    row ID. The turn's user message is the last committed one."""
    from research_core.plugin import messages as message_plugin

    monkeypatch.setattr("research_core.plugin.executor.profile_env", lambda **_: seam.ctx.env)
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    sent = seam.say("go on", context={"contract_version": 1})
    message_plugin.record_native_message(
        bind_origin=lambda *_: None,  # Tracing is not under test.
        session_id=seam.client.session_id,
        conversation_history=[
            {"role": "user", "content": "go on", "_row_id": 7},
            {"role": "assistant", "content": "Half", "_length_continuation_fragment": True},
            {"role": "user", "content": "[continue]", "_length_continuation_nudge": True},
        ],
    )
    row = seam.wstore.one(
        "SELECT native_message_id FROM client_admissions WHERE op_id=?", (sent["op_id"],)
    )
    assert row["native_message_id"] == 7


def test_ui_answer_and_delivery_obligation_roll_back_together(seam, monkeypatch):
    from research_core.store import admissions
    from research_core.store.errors import StoreError

    session_id = seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    open_ask(seam, session_id)

    def unavailable(*args, **kwargs):
        raise StoreError(["simulated admission write failure"])

    monkeypatch.setattr(admissions, "enqueue_in_transaction", unavailable)
    with pytest.raises(SeamError):
        seam.client.answer("ask_1", "yes")
    assert ui_state.get(seam.wstore, session_id)["doc"]["asks"]["ask_1"]["status"] == "open"
    assert seam.starts() == 0


def test_live_chat_followup_steers_the_original_run(seam):
    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    first = seam.say("Start working")
    followup = seam.say("Change direction")
    assert followup["run_id"] == first["run_id"]
    assert seam.hermes.steered[first["run_id"]] == ["Change direction"]
    from research_core.store import sessions

    assert sessions.read(seam.wstore, first["session_id"])["attempts"] == 1


def test_failed_attachment_retains_original_input_and_does_not_block_chat(seam):
    from uuid import uuid4

    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Read this attachment"},
            {"type": "file", "file": {"file_id": "unknown-file", "filename": "notes.txt"}},
        ],
    }
    receipt = seam.client._post(
        "/message",
        {"op_id": str(uuid4()), "session_id": seam.client.session_id, "message": message},
    )
    failed = seam.client.wait_admission(receipt["op_id"])
    assert failed["status"] == "failed"
    assert failed["run_id"] is None
    assert failed["error"] == "Unknown file ID"
    assert failed["input"] == message
    assert seam.starts() == 0
    following = seam.say("Continue without the file")
    assert following["run_id"]
    assert seam.starts() == 1


def test_multimodal_followup_waits_for_typed_start_instead_of_text_only_steer(seam):
    from uuid import uuid4

    seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    first = seam.say("Start working")
    parts = [
        {"type": "text", "text": "Inspect this image next"},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}},
    ]
    receipt = seam.client._post(
        "/message",
        {
            "op_id": str(uuid4()),
            "session_id": seam.client.session_id,
            "message": {"role": "user", "content": parts},
        },
    )
    # Deterministically run dispatch while the original native run remains active.
    import asyncio

    asyncio.run(seam.server.dispatch_messages())
    assert seam.hermes.steered.get(first["run_id"], []) == []
    assert (
        seam.wstore.one("SELECT status FROM client_admissions WHERE op_id=?", (receipt["op_id"],))[
            0
        ]
        == "queued"
    )
    seam.hermes.complete(first["run_id"])
    following = seam.client.wait_admission(receipt["op_id"])
    assert following["run_id"] != first["run_id"]
    assert seam.hermes.runs[following["run_id"]].input == parts


def test_ui_answer_native_history_keeps_its_native_content(seam):
    session_id = seam.client.open(session_context={"contract_version": 1, "coworld": COWORLD_ID})
    open_ask(seam, session_id)
    seam.client.answer("ask_1", "yes")
    with seam.wstore.tx(HARNESS) as tx:
        tx.execute("UPDATE client_admissions SET native_message_id=42 WHERE origin='answer'")
    native = {"id": 42, "role": "user", "content": "[UI action] answer to ask_1: yes"}
    response = seam.client._post("/messages/context", {"session_id": session_id, "data": [native]})
    assert response["data"] == [{**native, "client_context": None}]


@pytest.mark.parametrize("retryable", [False, True])
def test_failed_native_open_does_not_block_other_sessions(tmp_path, monkeypatch, retryable):
    from research_core.watcher.hermes_api import RunsError

    ctx = make_ctx(tmp_path)
    native = FakeHermesRuns()
    server = SeamServer(ws=ctx.workspace, hermes=native, env=dict(ctx.env))
    original = native.create_session
    failing = True

    def create(session_id, **kwargs):
        if session_id == "rejected" and failing:
            raise RunsError("Native opening rejected", status=503 if retryable else 400)
        return original(session_id, **kwargs)

    monkeypatch.setattr(native, "create_session", create)
    try:
        for session_id in ("rejected", "healthy"):
            server.session_open(
                {
                    "op_id": str(uuid4()),
                    "session_id": session_id,
                    "client": {"id": "failure-proof", "kind": "test"},
                }
            )
            server.message(
                {
                    "op_id": str(uuid4()),
                    "session_id": session_id,
                    "message": {"role": "user", "content": "hello"},
                }
            )
        asyncio.run(server.dispatch_messages())
        rows = ctx.wstore.q(
            "SELECT kind,session_id,status,error FROM client_admissions ORDER BY rowid"
        )
        rejected = [row for row in rows if row["session_id"] == "rejected"]
        assert all(row["status"] == ("queued" if retryable else "failed") for row in rejected)
        assert "Native opening rejected" in rejected[0]["error"]
        assert "healthy" in native.sessions
        assert any(row["session_id"] == "healthy" and row["status"] == "running" for row in rows)
        # A later input cannot bypass a terminal rejection or change a retry's session ID.
        server.message(
            {
                "op_id": str(uuid4()),
                "session_id": "rejected",
                "message": {"role": "user", "content": "later"},
            }
        )
        failing = False
        asyncio.run(server.dispatch_messages())
        if retryable:
            assert "rejected" in native.sessions
        else:
            assert "rejected" not in native.sessions
            assert all(
                row["status"] == "failed"
                for row in ctx.wstore.q(
                    "SELECT status FROM client_admissions WHERE session_id='rejected'"
                )
            )
            server.session_open(
                {
                    "op_id": str(uuid4()),
                    "session_id": "rejected",
                    "client": {"id": "recovered-proof", "kind": "test"},
                }
            )
            recovered = server.message(
                {
                    "op_id": str(uuid4()),
                    "session_id": "rejected",
                    "message": {"role": "user", "content": "after successful reopening"},
                }
            )
            asyncio.run(server.dispatch_messages())
            assert (
                ctx.wstore.one(
                    "SELECT status FROM client_admissions WHERE op_id=?", (recovered["op_id"],)
                )[0]
                == "running"
            )
    finally:
        server._http.server_close()


@pytest.mark.parametrize(
    "invalid", [{"session_context": []}, {"capabilities": 7}, {"session_id": 17}]
)
def test_invalid_open_metadata_is_not_admitted(seam, invalid):
    op_id = str(uuid4())
    with pytest.raises(SeamError) as info:
        seam.client._post(
            "/session-open",
            {"op_id": op_id, "client": {"id": "bad-metadata", "kind": "test"}, **invalid},
        )
    assert info.value.status == 400
    assert seam.wstore.one("SELECT op_id FROM client_admissions WHERE op_id=?", (op_id,)) is None


def test_open_receipt_and_local_session_roll_back_together(seam, monkeypatch):
    from research_core.store import sessions
    from research_core.store.errors import StoreError

    original = sessions.register_in_transaction

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise StoreError(["Injected failure after local registration"])

    monkeypatch.setattr(sessions, "register_in_transaction", interrupted)
    op_id = str(uuid4())
    with pytest.raises(SeamError) as info:
        seam.client._post(
            "/session-open",
            {
                "op_id": op_id,
                "session_id": "atomic-open",
                "client": {"id": "atomic-proof", "kind": "test"},
            },
        )
    assert info.value.status == 400
    assert seam.wstore.one("SELECT op_id FROM client_admissions WHERE op_id=?", (op_id,)) is None
    assert sessions.read(seam.wstore, "atomic-open") is None


@pytest.mark.parametrize("opening_status", ["queued", "failed"])
@pytest.mark.parametrize("lost_reply", [False, True])
def test_reopening_preserves_existing_execution_and_history(
    tmp_path, monkeypatch, opening_status, lost_reply
):
    from research_core.watcher.hermes_api import RunsUnavailable

    ctx = make_ctx(tmp_path)
    native = FakeHermesRuns()
    server = SeamServer(ws=ctx.workspace, hermes=native, env=dict(ctx.env))
    start = native.start
    lose_reply = lost_reply

    def uncertain_start(*args, **kwargs):
        nonlocal lose_reply
        run_id = start(*args, **kwargs)
        if lose_reply:
            lose_reply = False
            raise RunsUnavailable("Lost start response")
        return run_id

    monkeypatch.setattr(native, "start", uncertain_start)
    try:
        session_id = server.session_open(
            {"op_id": str(uuid4()), "client": {"id": "existing-proof", "kind": "test"}}
        )["session_id"]
        receipt = server.message(
            {
                "op_id": str(uuid4()),
                "session_id": session_id,
                "message": {"role": "user", "content": "existing work"},
            }
        )
        asyncio.run(server.dispatch_messages())
        intake.accept(
            ctx.wstore,
            HARNESS,
            op_id="reopening",
            kind="open",
            request={},
            session_id=session_id,
            message="",
            payload="",
        )
        with ctx.wstore.tx(HARNESS) as tx:
            tx.execute(
                "UPDATE client_admissions SET status=?,error='Opening refused'"
                " WHERE op_id='reopening'",
                (opening_status,),
            )

        def unavailable(*args, **kwargs):
            raise RunsUnavailable("Gateway unavailable")

        monkeypatch.setattr(native, "create_session", unavailable)
        asyncio.run(server.dispatch_messages())
        row = ctx.wstore.one(
            "SELECT status,run_id FROM client_admissions WHERE op_id=?", (receipt["op_id"],)
        )
        assert row["status"] == "running" and row["run_id"]
        assert len(native.runs) == 1
        # Reopening must not replace an established conversation with an empty pending chat.
        assert server.session_pending(session_id) == {"pending": False}
    finally:
        server._http.server_close()


def test_owner_is_persisted_before_http_while_dispatch_startup_is_delayed(
    tmp_path, monkeypatch, capsys
):
    from research_core.watcher import cli as watcher_cli

    ctx = make_ctx(tmp_path)
    server = SeamServer(ws=ctx.workspace, hermes=FakeHermesRuns(), env=dict(ctx.env))
    ctx.wstore.set_meta("coordinator_instance", "retired-owner")
    entered, release = threading.Event(), threading.Event()
    dispatch = server.dispatch_forever

    async def delayed_dispatch():
        entered.set()
        await asyncio.to_thread(release.wait)
        await dispatch()

    monkeypatch.setattr(server, "dispatch_forever", delayed_dispatch)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        result = ScriptedClient(server.url)._get("/status")
        owner = server.coordinator.instance_id
        assert result["coordinator_instance"] == owner
        assert ctx.wstore.get_meta("coordinator_instance") == owner
        monkeypatch.setenv("IDE_WORKSPACE", str(ctx.workspace.root))
        assert watcher_cli.run_status(None) == 0
        assert json.loads(capsys.readouterr().out)["coordinator_instance"] == owner
    finally:
        release.set()
        server.shutdown()
        thread.join(5)
        ctx.store.close()
        ctx.wstore.close()
    assert not thread.is_alive()


def test_contending_server_cannot_publish_owner_or_serve_http(tmp_path, monkeypatch):
    from research_core.coordinator import ownership

    ctx = make_ctx(tmp_path)
    server = SeamServer(ws=ctx.workspace, hermes=FakeHermesRuns(), env=dict(ctx.env))
    ctx.wstore.set_meta("coordinator_instance", "active-owner")
    serving = threading.Event()
    monkeypatch.setattr(server._http, "serve_forever", lambda **kwargs: serving.set())
    try:
        with ownership(ctx.workspace), pytest.raises(BlockingIOError):
            server.serve_forever()
        assert not serving.is_set()
        assert ctx.wstore.get_meta("coordinator_instance") == "active-owner"
    finally:
        server._http.server_close()
        ctx.store.close()
        ctx.wstore.close()


def test_dispatch_failure_before_callback_registration_exits_and_releases_owner(tmp_path):
    # Bound the whole process: the regression blocks before the HTTP loop can start.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from unittest.mock import patch

import pytest
from opentelemetry import trace

from research_core import serve
from research_core.coordinator import ownership
from research_core.layout import Workspace
from research_core.watcher.hermes_api import FakeHermesRuns

class CompletedExecutor(ThreadPoolExecutor):
    def submit(self, *args, **kwargs):
        future = super().submit(*args, **kwargs)
        wait([future], timeout=2)
        assert future.done()
        return future

async def failed_dispatch():
    raise RuntimeError("immediate dispatcher failure")

root = Path(sys.argv[1])
ws = Workspace(root=root, venv=root / "tools/venv")
ws.ensure_dirs()
server = serve.SeamServer(ws=ws, hermes=FakeHermesRuns(), env={})
with (
    patch.object(serve, "ThreadPoolExecutor", CompletedExecutor),
    patch.object(server, "dispatch_forever", failed_dispatch),
    pytest.raises(RuntimeError, match="immediate dispatcher failure"),
):
    server.serve_forever()
server._http.server_close()
with ownership(ws):
    print("dispatcher error propagated; owner released")
""",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dispatcher error propagated; owner released" in result.stdout


def test_pending_edit_has_accepted_http_status_and_readable_terminal_receipt(
    tmp_path, monkeypatch, capsys
):
    from research_core import transitions

    ctx = make_ctx(tmp_path)
    record = tasks.create(ctx.store, HARNESS, type="maintenance", title="Before", description="Fix")
    server = SeamServer(ws=ctx.workspace, hermes=FakeHermesRuns(), env=dict(ctx.env))
    apply_commands = server.coordinate_once
    monkeypatch.setattr(server, "coordinate_once", lambda: None)
    monkeypatch.setattr(transitions, "REQUEST_WAIT_SECONDS", 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        request = urllib.request.Request(
            server.url + "/store/apply",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(
                {
                    "actor": "user:local",
                    "op": "tasks.update",
                    "coworld": COWORLD_ID,
                    "args": {"task_id": record["id"], "title": "After", "operation_id": "edit"},
                }
            ).encode(),
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
            pending = json.load(response)
        assert pending["status"] == "pending" and "ok" not in pending and "record" not in pending
        with ctx.store.tx(HARNESS) as tx:
            tx.execute(
                "UPDATE transition_commands SET last_error=?,retry_at=? WHERE id=?",
                (
                    "RuntimeError: coordinator fault",
                    "2000-01-01T00:00:00Z",
                    pending["command"]["command_id"],
                ),
            )
        from research_core.watcher import cli as watcher_cli

        monkeypatch.setenv("IDE_WORKSPACE", str(ctx.workspace.root))
        assert watcher_cli.run_status(None) == 0
        printed = json.loads(capsys.readouterr().out)
        status = ScriptedClient(server.url)._get("/status")
        assert printed["pending_transitions"] == status["pending_transitions"]
        [waiting] = status["pending_transitions"]
        assert waiting["last_error"] == "RuntimeError: coordinator fault"
        assert waiting["retry_at"] == "2000-01-01T00:00:00Z"
        apply_commands()
        url = (
            f"{server.url}/command?coworld_id={COWORLD_ID}"
            f"&command_id={pending['command']['command_id']}"
        )
        with urllib.request.urlopen(url, timeout=5) as response:
            result = json.load(response)
        assert result["transition_state"] == "applied" and result["record"]["title"] == "After"
    finally:
        server.shutdown()
        thread.join(5)
        ctx.store.close()
        ctx.wstore.close()
