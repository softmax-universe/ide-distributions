"""The dropdown binds sessions; workspace-global selection cannot change their game."""

import json
import threading

import pytest

from research_core import layout
from research_core.client import ScriptedClient, SeamError
from research_core.plugin.turn import render_turn
from research_core.serve import SeamServer
from research_core.store import coworlds, notices, sessions, tasks
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.store.errors import StoreError
from research_core.tools.base import ToolContext, ToolError
from research_core.watcher.hermes_api import FakeHermesRuns

ACTOR = Actor.harness()


def test_interleaved_clients_keep_their_coworld_after_global_selection_changes(tmp_path):
    from datetime import UTC, datetime

    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path)})
    ws.ensure_dirs()
    with Store.open(ws.store_path, kind="workspace") as wstore:
        for game in ("cow_diplomacy", "cow_arena"):
            coworlds.register(wstore, ACTOR, game, folder=game)
            ws.coworld(game).ensure_dirs()
    native = FakeHermesRuns()
    server = SeamServer(ws=ws, hermes=native, env={"IDE_WORKSPACE": str(tmp_path)})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        clients = {game: ScriptedClient(server.url) for game in ("cow_diplomacy", "cow_arena")}
        contexts = {}
        for game, client in clients.items():
            contexts[game] = {"contract_version": 1, "coworld": game, "league": f"league_{game}"}
            client.open(session_context=contexts[game])
        with Store.open(ws.store_path, kind="workspace") as wstore:
            # Retained workspace selection deliberately disagrees with the visible Arena client.
            coworlds.activate(wstore, ACTOR, "cow_diplomacy")
        for game, client in reversed(list(clients.items())):
            receipt = client.say("improve our policy", contexts[game])
            done = client.wait_admission(receipt["op_id"])
            with Store.open(ws.store_path, kind="workspace") as wstore:
                row = wstore.one(
                    "SELECT payload FROM client_admissions WHERE run_id=?", (done["run_id"],)
                )
                assert json.loads(row["payload"])["context"]["coworld"] == game
                assert sessions.read(wstore, client.session_id)["coworld_id"] == game
            ctx = ToolContext.from_env(
                "orchestrator",
                {"IDE_WORKSPACE": str(tmp_path), "IDE_SESSION_ID": client.session_id},
            )
            try:
                assert ctx.coworld().coworld_id == game
                tasks.create(
                    ctx.store,
                    ctx.actor,
                    title=game,
                    description="game-specific work",
                    type="maintenance",
                )
                notices.add(ctx.wstore, ctx.actor, text=f"secret {game}")
                prompt = render_turn(
                    ctx, client.session_id, is_first_turn=True, now=datetime.now(UTC)
                )
                assert f"[session Coworld] {game}" in prompt
                assert f"secret {game}" in prompt
                other = "cow_arena" if game == "cow_diplomacy" else "cow_diplomacy"
                assert f"secret {other}" not in prompt
                with ctx.wstore.tx(ACTOR) as tx:
                    tx.event("task.assigned", "T-999", {"coworld_id": other})
                next_prompt = render_turn(
                    ctx, client.session_id, is_first_turn=False, now=datetime.now(UTC)
                )
                assert "task.assigned" not in next_prompt
            finally:
                ctx.wstore.close()
                ctx.store.close()
        for game in clients:
            snapshot = server.snapshot("coworld", game)
            assert snapshot["coworld_id"] == game
            assert [row["title"] for row in snapshot["records"]["tasks"]] == [game]
        with pytest.raises(SeamError) as refused:
            clients["cow_arena"].say("wrong game", contexts["cow_diplomacy"])
        assert refused.value.status == 409
        with Store.open(ws.store_path, kind="workspace") as wstore:
            with pytest.raises(StoreError, match="cannot change"):
                sessions.register(
                    wstore,
                    ACTOR,
                    session_id=clients["cow_arena"].session_id,
                    profile="orchestrator",
                    kind="chat",
                    coworld_id="cow_diplomacy",
                )
    finally:
        server.shutdown()


def test_unbound_session_cannot_inherit_retained_coworld(tmp_path):
    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path)})
    ws.ensure_dirs()
    with Store.open(ws.store_path, kind="workspace") as wstore:
        coworlds.register(wstore, ACTOR, "cow_diplomacy", folder="cow_diplomacy")
        coworlds.activate(wstore, ACTOR, "cow_diplomacy")
        sessions.register(wstore, ACTOR, session_id="unbound", profile="orchestrator", kind="chat")
    ctx = ToolContext.from_env(
        "orchestrator", {"IDE_WORKSPACE": str(tmp_path), "IDE_SESSION_ID": "unbound"}
    )
    try:
        with pytest.raises(ToolError, match="no Coworld"):
            ctx.coworld()
    finally:
        ctx.wstore.close()


def test_canonical_session_binding_is_immutable(tmp_path):
    from research_core.serve import Refused

    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path)})
    ws.ensure_dirs()
    server = SeamServer(ws=ws, hermes=FakeHermesRuns(), env={"IDE_WORKSPACE": str(tmp_path)})
    try:
        request = {
            "op_id": "00000000-0000-4000-8000-000000000001",
            "session_id": "bound",
            "client": {"id": "web", "kind": "test"},
            "session_context": {
                "contract_version": 1,
                "scope": "coworld",
                "coworld": "cow_arena",
                "league": "league_arena",
            },
        }
        opened = server.session_open(request)
        assert opened["session_scope"] == {
            "scope": "coworld",
            "coworld": "cow_arena",
            "league": "league_arena",
        }
        with pytest.raises(Refused) as changed:
            server.session_open(
                {
                    **request,
                    "op_id": "00000000-0000-4000-8000-000000000002",
                    "session_context": {**request["session_context"], "league": "league_other"},
                }
            )
        assert changed.value.status == 409 and changed.value.body["kind"] == "scope_mismatch"
        workspace = server.session_open(
            {
                "op_id": "00000000-0000-4000-8000-000000000003",
                "client": request["client"],
                "session_context": {
                    "contract_version": 1,
                    "scope": "workspace",
                    "coworld": None,
                    "league": None,
                },
            }
        )
        assert workspace["session_scope"] == {"scope": "workspace", "coworld": None, "league": None}
        with Store.open(ws.store_path, kind="workspace") as store:
            assert len(store.q("SELECT * FROM client_admissions")) == 2
    finally:
        server._http.server_close()


def test_workspace_activity_remains_scoped_across_idle_games(tmp_path):
    from datetime import UTC, datetime

    from research_core.watcher import heartbeat
    from research_core.watcher.context import TickContext

    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path)})
    ws.ensure_dirs()
    with Store.open(ws.store_path, kind="workspace") as wstore:
        for game in ("cow_diplomacy", "cow_arena"):
            coworlds.register(wstore, ACTOR, game, folder=game)
            with Store.open(ws.coworld(game).store_path, kind="coworld") as store:
                ctx = TickContext(
                    ws=ws,
                    wstore=wstore,
                    store=store,
                    coworld=ws.coworld(game),
                    hermes=FakeHermesRuns(),
                    platform=None,
                    env={},
                    now=datetime.now(UTC),
                )
                if game == "cow_diplomacy":
                    with store.tx(ACTOR) as tx:
                        tx.event("task.completed", "T-1", {})
                # Arena is idle and iterates last. It must not erase Diplomacy's queued wake.
                if game == "cow_arena":
                    assert heartbeat.run(ctx)["has_work"] is True


def test_runtime_filters_chat_history_and_rejects_wrong_game_reads(tmp_path):
    from uuid import uuid4

    from research_core.serve import Refused

    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path)})
    ws.ensure_dirs()
    server = SeamServer(ws=ws, hermes=FakeHermesRuns(), env={})
    try:
        for sid, coworld, league in (
            ("arena", "cow_arena", "league_arena"),
            ("diplomacy", "cow_diplomacy", "league_diplomacy"),
            ("other_league", "cow_arena", "league_other"),
        ):
            server.session_open(
                {
                    "op_id": str(uuid4()),
                    "session_id": sid,
                    "client": {"id": "web", "kind": "test"},
                    "session_context": {
                        "contract_version": 1,
                        "coworld": coworld,
                        "league": league,
                    },
                }
            )
            server.message(
                {
                    "op_id": str(uuid4()),
                    "session_id": sid,
                    "message": {"role": "user", "content": f"private {sid}"},
                    "context": {"contract_version": 1, "coworld": coworld, "league": league},
                }
            )
        # The runtime returns only the requested game's chats, even with mixed retained state.
        listed = server.snapshot("workspace", "cow_arena", league="league_arena")
        assert [row["id"] for row in listed["records"]["sessions"]] == ["arena"]
        assert listed["records"]["admissions"] == []
        assert server.snapshot("workspace", league="league_arena")["records"]["sessions"] == []
        selected = server.snapshot(
            "workspace", "cow_arena", session_id="arena", league="league_arena"
        )
        assert [row["id"] for row in selected["records"]["sessions"]] == ["arena"]
        assert [row["session_id"] for row in selected["records"]["admissions"]] == ["arena"]
        for coworld, league in (
            ("cow_diplomacy", "league_diplomacy"),
            ("cow_arena", "league_other"),
            (None, None),
        ):
            with pytest.raises(Refused) as refused:
                server.snapshot("workspace", coworld, session_id="arena", league=league)
            assert refused.value.status == 409
            assert refused.value.body["kind"] == "scope_mismatch"
    finally:
        server._http.server_close()
