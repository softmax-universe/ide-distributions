"""Drive the running workspace as the platform: hosting control and AIDE 1.0.0 side by side.

The container's bootstrap names this script's port as the platform, and the orchestrator's
model points at its scripted OpenRouter mirror, so the round trip needs no credentials: lease,
control status, then session.open -> chat.send -> chunks -> chat.finished -> chat.history over
the AIDE backend socket, then the same request's record and events read again over the socket the
backend opens after the platform drops the first one.
"""

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml
from aiohttp import ClientSession, WSMsgType, web

PLATFORM_PORT, MODEL_PORT = int(sys.argv[1]), int(sys.argv[2])
WORKSPACE = {"scope": "workspace", "coworld": None, "league": None}
REPLY = ["The ", "workspace ", "answers."]
OPENER = "Help me build a better opening strategy for Generals."
TITLE = "Generals Opening Strategy"
XP_SCOPE = {"scope": "coworld", "coworld": "cow_fake", "league": "league_fake"}
XP_START = "Run the prepared experiment."
XP_NEXT = "Run the prepared experiment with the next seed."
XP_SESSION = Path("/workspace/.xp-sleep-session")
WAKE_PROOF = "--wake-proof" in sys.argv[3:]
XP_REPLY = "Both experiment arms finished and were evaluated."
XP_WAKE_REPLY = "Both experiment arms for the second seed finished and were evaluated."
XP_SEED = """
import sys

from research_core.store import experiments, versions
from research_core.tools.base import ToolContext

sys.path.insert(0, "/opt/distribution/core")
from tests.fixtures.fakegame_repo import fixture_files

ctx = ToolContext.from_env("orchestrator")
files = fixture_files("cow_fake")
mixin = ctx.coworld().mixin
(mixin / "tools").mkdir(parents=True, exist_ok=True)
(mixin / "MIXIN.md").write_text(files["MIXIN.md"])
(mixin / "tools/evaluate.py").write_text(files["tools/evaluate.py"])
platform = ctx.platform()
platform.configure(polls_to_complete=10000)
for number, patience in ((1, 5), (2, 8)):
    version = versions.create(
        ctx.store, ctx.actor, summary=f"Policy {number}", commit_sha=f"{number:040x}",
        branch="main", artifact={"kind": "file", "path": "policy.json",
                                "digest": "d" * 64, "size": 1}, based_on=[],
    )
    policy = platform.upload_file_policy(
        name=f"policy-{number}", content=(
            '{"parameters":{"aggression":3,"patience":%d}}' % patience
        ).encode(),
    )
    versions.mark_uploaded(ctx.store, ctx.actor, version["id"], policy_version_id=policy.id,
                           at=policy.created_at, digest_checked=True)
record = experiments.create(
    ctx.store, ctx.actor, summary="Does patience help?",
    hypothesis={"statement": "Patience helps", "if_true": "score rises",
                "if_false": "score stays flat", "motivation": "Compare two policies"},
    methods={"baseline": "V-1", "candidate": "V-2", "procedure": "One paired seed",
             "metrics": [{"name": "score", "direction": "up", "role": "primary", "min_effect": 1}],
             "decision_rule": {"text": "Higher score wins", "n_min": 1}},
)
experiments.start(ctx.store, ctx.actor, record["id"])
"""


async def main() -> None:
    control_sockets, backend, done = asyncio.Queue(), asyncio.Queue(), asyncio.Event()
    title_model = None

    async def lease(request: web.Request) -> web.Response:
        expires = datetime.now(UTC) + timedelta(hours=1)
        return web.json_response(
            {
                "key": "sk-or-scripted",
                "expires_at": expires.isoformat(),
                "renew_at": expires.isoformat(),
                "allowance_usd": 1,
            }
        )

    async def socket(request: web.Request) -> web.WebSocketResponse:
        aide = request.path.endswith("/aide/1.0.0/backend")
        if aide:
            assert request.headers["X-Softmax-Execution-Authority"] == "restricted-conversations"
        response = web.WebSocketResponse(protocols=("aide.1.0.0",) if aide else ())
        await response.prepare(request)
        if aide:
            binding = {"backend_id": str(uuid4()), "version": "1.0.0", "max_message_bytes": 1048576}
            await response.send_json({"binding": binding})
        await (backend if aide else control_sockets).put(response)
        await done.wait()  # the scenario reads through its own handle
        return response

    async def completions(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        last = body["messages"][-1]
        text = str(last.get("content"))
        finish = "stop"
        tool_call = None
        reply = REPLY
        starting = last["role"] == "user" and (XP_START in text or XP_NEXT in text)
        following = (
            last["role"] == "user"
            and "The watched experience requests for X-1 have reached terminal states." in text
        )
        if starting or following:
            args = {"action": "request" if starting else "status", "experiment": "X-1"}
            if starting:
                args["opponents"] = ["pv_other"]
                if XP_NEXT in text:
                    args["seeds"] = [2]
            action = "experience" if starting else "evaluate"
            tool_call = {
                "id": f"call_{action}_{uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": "xp", "arguments": json.dumps(args)},
            }
            finish = "tool_calls"
            chunks = [{"role": "assistant", "tool_calls": [{"index": 0, **tool_call}]}]
        else:
            if last["role"] == "tool" and last["tool_call_id"].startswith("call_experience_"):
                assert json.loads(text)["followup_scheduled"] is True, text
                reply = ["I will report when they finish."]
            elif last["role"] == "tool" and last["tool_call_id"].startswith("call_evaluate_"):
                evaluated = json.loads(text)
                assert evaluated["complete"] is True, evaluated
                assert {r["arm"] for r in evaluated["runs"] if r["results"]["rows"]} == {
                    "baseline",
                    "candidate",
                }, evaluated
                reply = [XP_WAKE_REPLY if len(evaluated["runs"]) == 4 else XP_REPLY]
            chunks = [{"role": "assistant", "content": reply[0]}] + [
                {"content": t} for t in reply[1:]
            ]
        frames = [
            {
                "id": "scripted",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            for delta in chunks
        ]
        frames[-1]["choices"][0]["finish_reason"] = finish
        frames.append(
            {
                **frames[-1],
                "choices": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4},
            }
        )
        if not body.get("stream"):
            if body["model"] == title_model:
                message = {"role": "assistant", "content": json.dumps({"title": TITLE})}
            elif tool_call is not None:
                message = {"role": "assistant", "content": None, "tool_calls": [tool_call]}
            else:
                message = {"role": "assistant", "content": "".join(reply)}
            return web.json_response(
                {
                    "id": "scripted",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body["model"],
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": frames[-1]["usage"],
                }
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for frame in frames:
            await response.write(f"data: {json.dumps(frame)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    platform = web.Application()
    platform.router.add_put("/v2/workspaces/{workspace}/inference", lease)
    platform.router.add_get("/v2/workspaces/{workspace}/connection", socket)
    platform.router.add_get("/v2/workspaces/{workspace}/aide/1.0.0/backend", socket)
    model = web.Application()
    model.router.add_post("/api/v1/chat/completions", completions)
    runners = [web.AppRunner(platform), web.AppRunner(model)]
    for runner, port in zip(runners, (PLATFORM_PORT, MODEL_PORT), strict=True):
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()

    async def exercise() -> None:
        nonlocal title_model
        control = await control_sockets.get()
        await control.send_json(
            {"stream_id": "status", "envelope": {"id": str(uuid4()), "control": "status"}}
        )
        header = await control.receive_json()
        assert header["status"] == 200, header
        print("PASS control connection answers beside the AIDE backend", flush=True)
        # Hermes treats a config base_url under provider openrouter as a mirror (#10622).
        config = Path("/workspace/host/profiles/orchestrator/config.yaml")
        settings = yaml.safe_load(config.read_text())
        mirror = f"http://127.0.0.1:{MODEL_PORT}/api/v1"
        settings["model"]["base_url"] = mirror
        title_config = settings["auxiliary"]["title_generation"]
        title_model = title_config["model"]
        title_config["base_url"] = mirror
        config.write_text(yaml.safe_dump(settings))

        relay = await backend.get()
        frontend = str(uuid4())
        notes: list[dict] = []

        async def request(method: str, params: dict) -> dict:
            request_id = str(uuid4())
            message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            await relay.send_json(
                {"frontend_id": frontend, "message": message, "authority": {"mode": "owner"}}
            )
            while True:
                received = await relay.receive()
                assert received.type == WSMsgType.TEXT, received
                frame = json.loads(received.data)
                assert frame["frontend_id"] == frontend, frame
                if frame["message"].get("id") == request_id:
                    assert "result" in frame["message"], frame
                    return frame["message"]["result"]
                notes.append(frame["message"])

        if WAKE_PROOF:
            target = {"session_id": XP_SESSION.read_text(), "scope": XP_SCOPE}
            # The retained chat may have received its reply before this client reconnected.
            # History reads never send a message or start a turn.
            while True:
                history = await request("chat.history", target)
                replies = [i["message"]["content"] for i in history["items"]]
                if XP_WAKE_REPLY in replies:
                    assert replies.count(XP_REPLY) == replies.count(XP_WAKE_REPLY) == 1, replies
                    break
                await asyncio.sleep(1)
            print("PASS AIDE checks retained XP on wake and follows up without a send", flush=True)
            return

        session = (await request("session.open", {"scope": WORKSPACE}))["session"]
        assert session["scope"] == WORKSPACE, session
        sent = {"session_id": session["id"], "scope": WORKSPACE}
        sent["messages"] = [{"role": "user", "content": OPENER}]
        accepted = await request("chat.send", sent)
        request_id = accepted["request_id"]
        assert accepted["accepted"] is True, accepted
        while not any(note["method"] == "chat.finished" for note in notes):
            frame = json.loads((await relay.receive()).data)
            notes.append(frame["message"])
        (finished,) = [note["params"] for note in notes if note["method"] == "chat.finished"]
        assert finished["status"] == "completed", finished
        chunks = [n["params"] for n in notes if n["method"] == "chat.chunk"]
        text = "".join(c["chunk"]["choices"][0]["delta"]["content"] for c in chunks)
        assert text == "".join(REPLY), notes
        history = await request("chat.history", {"session_id": session["id"], "scope": WORKSPACE})
        roles = [(i["message"]["role"], i["message"]["content"]) for i in history["items"]]
        assert ("user", OPENER) in roles and ("assistant", "".join(REPLY)) in roles, roles
        print(
            "PASS AIDE session.open, chat.send, chunks, chat.finished and chat.history", flush=True
        )

        # Hermes upgrades its initial title in the background.
        async with asyncio.timeout(30):
            while True:
                listed = await request("state.list", {"resource": "sessions"})
                item = next(item for item in listed["items"] if item["id"] == session["id"])
                if item.get("title") == TITLE:
                    break
                await asyncio.sleep(0.5)
        print("PASS AIDE lists a title generated from the opening turn", flush=True)

        # Reads are authoritative: a new backend socket reads what the first one pushed.
        await relay.close()
        relay = await backend.get()
        target = {"session_id": session["id"], "request_id": request_id, "scope": WORKSPACE}
        read = await request("chat.events", target | {"after": {"attempt": 0, "seq": 0}})
        pushed = [n["params"]["event"] for n in notes if n["method"] == "chat.event"]
        assert read["events"] == pushed and read["done"] is True, (read, pushed)
        assert read["next"] == finished["cursor"], (read, finished)
        got = {"resource": "requests", "id": request_id, "scope": WORKSPACE}
        item = (await request("state.get", got))["item"]
        assert (item["status"], item["revision"]) == ("completed", finished["revision"]), item

        xp_session = (await request("session.open", {"scope": XP_SCOPE}))["session"]["id"]
        seed = await asyncio.create_subprocess_exec(
            "/opt/workspace-env/bin/python",
            "-c",
            XP_SEED,
            env={**os.environ, "IDE_SESSION_ID": xp_session},
        )
        assert await seed.wait() == 0
        xp_target = {"session_id": xp_session, "scope": XP_SCOPE}
        xp_start = await request(
            "chat.send", xp_target | {"messages": [{"role": "user", "content": XP_START}]}
        )
        while not any(
            n["method"] == "chat.finished" and n["params"]["request_id"] == xp_start["request_id"]
            for n in notes
        ):
            notes.append(json.loads((await relay.receive()).data)["message"])
        async with ClientSession() as http, http.get("http://127.0.0.1:8650/status") as status:
            waiting = await status.json()
        assert not waiting["has_work"], waiting

        # Complete the fake platform requests without sending another user message.
        complete = await asyncio.create_subprocess_exec(
            "/opt/workspace-env/bin/python",
            "-c",
            "from research_core.platform import from_env; "
            "from_env().configure(polls_to_complete=1)",
        )
        assert await complete.wait() == 0
        while not any(
            n["method"] == "chat.finished"
            and n["params"]["session_id"] == xp_session
            and n["params"]["request_id"] != xp_start["request_id"]
            for n in notes
        ):
            notes.append(json.loads((await relay.receive()).data)["message"])
        replies = [
            n["params"]["message"]["content"]
            for n in notes
            if n["method"] == "chat.message" and n["params"]["session_id"] == xp_session
        ]
        assert replies.count(XP_REPLY) == 1, replies
        history = await request("chat.history", xp_target)
        assert sum(i["message"]["content"] == XP_REPLY for i in history["items"]) == 1, history
        (trigger,) = [
            i
            for i in history["items"]
            if "This is an automatic follow-up" in str(i["message"]["content"])
        ]
        assert trigger["kind"] == "continuation", trigger
        print("PASS pending XP allows sleep and follows up while awake", flush=True)

        # Leave a second batch pending on the same volume for the next boot.
        pending = await asyncio.create_subprocess_exec(
            "/opt/workspace-env/bin/python",
            "-c",
            "from research_core.platform import from_env; "
            "from_env().configure(polls_to_complete=10000)",
        )
        assert await pending.wait() == 0
        started = await request(
            "chat.send", xp_target | {"messages": [{"role": "user", "content": XP_NEXT}]}
        )
        while not any(
            n["method"] == "chat.finished" and n["params"]["request_id"] == started["request_id"]
            for n in notes
        ):
            notes.append(json.loads((await relay.receive()).data)["message"])
        XP_SESSION.write_text(xp_session)

    async with asyncio.timeout(300):
        await exercise()
    print(
        "PASS AIDE request record and chat.events read again after the socket is replaced",
        flush=True,
    )
    done.set()
    for runner in runners:
        await runner.cleanup()


asyncio.run(main())
