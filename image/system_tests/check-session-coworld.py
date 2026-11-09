"""Two session-bound games through the packaged AIDE backend, tools, prompts and terminals."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from dotenv import dotenv_values

sys.path.insert(0, "/opt/distribution/image")
sys.path.insert(0, "/opt/hermes")
from aide import Backend, History, Open, Refusal, StateList
from gateway.session_context import scoped_current_session_id
from hermes_cli.plugins import discover_plugins
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from inference import Availability
from run_agent import AIAgent
from tools.terminal_tool import get_session_cwd, terminal_tool

from research_core.plugin.session_context import render
from research_core.plugin.turn import make_pre_llm_call
from research_core.store import sessions, tasks
from research_core.tools.base import ToolContext


async def main():
    home = Path("/workspace/host/profiles/orchestrator")
    env = {**dotenv_values(home / ".env"), "HERMES_HOME": str(home)}
    os.environ.update(env)
    token = set_hermes_home_override(home)
    try:
        async with (
            httpx.AsyncClient(base_url="http://127.0.0.1:8650") as seam,
            httpx.AsyncClient(
                base_url="http://127.0.0.1:8642/p/orchestrator/",
                headers={
                    "Authorization": "Bearer "
                    + Path("/workspace/host/.gateway-key").read_text().strip(),
                },
            ) as gateway,
            asyncio.TaskGroup() as work,
        ):
            backend = Backend(seam, gateway, Availability(), work)
            frontend = uuid4()
            bound = []
            for game in ("cow_scope_diplomacy", "cow_scope_arena"):
                scope = {"scope": "coworld", "coworld": game, "league": "league_" + game}
                opening = Open.model_validate(
                    {"scope": scope, "client": {"id": "scope-proof", "kind": "test"}}
                )
                request_id = str(uuid5(NAMESPACE_URL, f"packaged-session-scope:{game}"))
                opened = await backend.open(frontend, request_id, opening)
                session_id = opened["session"]["id"]
                ctx = ToolContext.from_env("orchestrator", {**env, "IDE_SESSION_ID": session_id})
                try:
                    assert ctx.coworld().coworld_id == game
                    code = ctx.coworld().code
                    skill = ctx.coworld().mixin / "skills" / (game + "-skill") / "SKILL.md"
                    skill.parent.mkdir(parents=True, exist_ok=True)
                    skill.write_text("---\nname: " + game + "-skill\n---\n" + game)
                    prompt = render(ctx, session_id)
                    assert str(skill) in prompt
                    for role in ("orchestrator", "researcher"):
                        role_home = home.parent / role
                        role_env = {
                            **dotenv_values(role_home / ".env"),
                            "HERMES_HOME": str(role_home),
                        }
                        os.environ.update(role_env)
                        role_token = set_hermes_home_override(role_home)
                        try:
                            discover_plugins(force=True)
                            agent = AIAgent(
                                base_url="http://127.0.0.1:9/v1",
                                api_key="fixture-unused",
                                provider="custom",
                                model="gpt-4.1-mini",
                                session_id=session_id,
                                enabled_toolsets=["ide-" + role, "terminal"],
                                quiet_mode=True,
                                skip_memory=True,
                                skip_background_review=True,
                            )
                            try:
                                native_prompt = agent._build_system_prompt()
                                assert str(skill) in native_prompt, (role, native_prompt)
                                foreign = (
                                    "cow_scope_arena"
                                    if "diplomacy" in game
                                    else "cow_scope_diplomacy"
                                )
                                assert foreign not in native_prompt, (role, native_prompt)
                            finally:
                                agent.close()
                        finally:
                            reset_hermes_home_override(role_token)
                            os.environ.update(env)
                    other = "cow_scope_arena" if "diplomacy" in game else "cow_scope_diplomacy"
                    assert other not in prompt
                    task_id = "scope-proof-" + session_id
                    make_pre_llm_call("orchestrator")(session_id=session_id, task_id=task_id)
                    assert get_session_cwd(task_id) == str(code)
                    result = json.loads(terminal_tool(command="pwd", task_id=task_id))
                    assert result["exit_code"] == 0, result
                    assert str(code) in result["output"], result
                    if game not in [task["title"] for task in tasks.list(ctx.store)]:
                        tasks.create(
                            ctx.store,
                            ctx.actor,
                            title=game,
                            description="isolated packaged work",
                            type="maintenance",
                        )
                    bound.append((game, session_id, task_id))
                finally:
                    ctx.wstore.close()
                    ctx.store.close()
            # Interleave the two native terminals after both have initialized.
            for game, session_id, task_id in reversed(bound):
                with scoped_current_session_id(session_id):
                    ctx = ToolContext.from_env(
                        "orchestrator", {**env, "IDE_SESSION_ID": session_id}
                    )
                    try:
                        assert sessions.read(ctx.wstore, session_id)["coworld_id"] == game
                        result = json.loads(terminal_tool(command="pwd", task_id=task_id))
                        assert str(ctx.coworld().code) in result["output"], result
                        scoped = json.loads(
                            terminal_tool(
                                command=f"research store snapshot --store coworld --coworld {game}",
                                task_id=task_id,
                            )
                        )
                        assert scoped["exit_code"] == 0, scoped
                        implicit = json.loads(
                            terminal_tool(
                                command="research store snapshot --store coworld", task_id=task_id
                            )
                        )
                        assert (
                            implicit["exit_code"] == 0
                            and json.loads(implicit["output"].strip())["coworld_id"] == game
                        ), implicit
                        assert json.loads(scoped["output"].strip())["coworld_id"] == game, scoped
                        missing = json.loads(
                            terminal_tool(
                                command=(
                                    "env -u IDE_SESSION_ID -u HERMES_SESSION_ID -u IDE_COWORLD_ID "
                                    "research store snapshot --store coworld"
                                ),
                                task_id=task_id,
                            )
                        )
                        assert missing["exit_code"] != 0 and "--coworld" in missing["output"], (
                            missing
                        )
                        foreign_game = next(other for other, _, _ in bound if other != game)
                        refused = json.loads(
                            terminal_tool(
                                command=(
                                    f"env IDE_SESSION_ID={session_id} research store snapshot "
                                    f"--store coworld --coworld {foreign_game}"
                                ),
                                task_id=task_id,
                            )
                        )
                        assert (
                            refused["exit_code"] != 0
                            and "differs from this session" in refused["output"]
                        ), refused
                        snapshot = await seam.get(
                            "/snapshot", params={"store": "coworld", "coworld": game}
                        )
                        snapshot.raise_for_status()
                        titles = [task["title"] for task in snapshot.json()["records"]["tasks"]]
                        assert game in titles and all(
                            other_game not in titles
                            for other_game, _, _ in bound
                            if other_game != game
                        ), titles
                    finally:
                        ctx.wstore.close()
            for game, session_id, _ in bound:
                scope = {"scope": "coworld", "coworld": game, "league": "league_" + game}
                listing = await backend.list_state(
                    frontend,
                    str(uuid4()),
                    StateList.model_validate({"resource": "sessions", "scope": scope}),
                )
                assert [item["id"] for item in listing["items"]] == [session_id], listing
            first_game = bound[0][0]
            foreign_scope = {
                "scope": "coworld",
                "coworld": first_game,
                "league": "league_" + first_game,
            }
            foreign_read = History.model_validate(
                {"session_id": bound[1][1], "scope": foreign_scope}
            )
            refusal = None
            try:
                await backend.history(uuid4(), str(uuid4()), foreign_read)
            except Refusal as refused:
                refusal = refused
            assert refusal is not None and refusal.code == "forbidden", refusal
            mismatch = await seam.post(
                "/message",
                json={
                    "op_id": str(uuid4()),
                    "session_id": bound[1][1],
                    "message": {"role": "user", "content": "wrong game"},
                    "context": {"contract_version": 1, "coworld": bound[0][0]},
                },
            )
            assert mismatch.status_code == 409 and mismatch.json()["kind"] == "scope_mismatch", (
                mismatch.text
            )
    finally:
        reset_hermes_home_override(token)
    print("PASS packaged two-game sessions, prompts, tasks, terminals and mismatch rejection")


asyncio.run(main())
