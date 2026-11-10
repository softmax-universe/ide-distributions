"""prepare.py end to end against a real Hermes: install, prepare, verify through ``hermes``.

Builds the workspace environment with ``uv sync`` in a temporary workspace, so the first run
takes a few minutes. Run locally with
``HERMES_BIN=<hermes> uv run pytest -m hermes core/tests/hermes/test_prepare_real.py``.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CORE_SRC = REPO / "core" / "src"


@dataclass(frozen=True)
class Host:
    home: Path  # HERMES_REAL_HOME: the host home that holds profiles/
    workspace: Path  # IDE_WORKSPACE, shared by both profiles

    def profile(self, role: str) -> Path:
        return self.home / "profiles" / role

    def env(self, role: str) -> dict[str, str]:
        return {
            **os.environ,
            "HERMES_HOME": str(self.profile(role)),
            "HERMES_REAL_HOME": str(self.home),
            "IDE_WORKSPACE": str(self.workspace),
        }


def run(argv, env, timeout=120):
    return subprocess.run(
        [str(part) for part in argv], env=env, capture_output=True, text=True, timeout=timeout
    )


def hermes_python(hermes_bin: Path) -> str:
    """The interpreter Hermes runs under: the sibling ``python`` of the executable."""
    sibling = hermes_bin.parent / "python"
    return str(sibling) if sibling.exists() else sys.executable


def resolved_config(hermes_bin, host: Host, role):
    code = (
        "import json; from hermes_cli.config import load_config; print(json.dumps(load_config()))"
    )
    result = run([hermes_python(hermes_bin), "-c", code], host.env(role))
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def prepare(hermes_bin, host: Host, role, *flags):
    script = host.profile(role) / "scripts" / "prepare.py"
    argv = [hermes_python(hermes_bin), script, "--source", REPO / role, "--hermes", hermes_bin]
    result = run([*argv, *flags], host.env(role), timeout=600)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def watcher_job(host: Host):
    jobs = json.loads((host.profile("orchestrator") / "cron" / "jobs.json").read_text())["jobs"]
    return [job for job in jobs if job["id"] == "watcher"]


@pytest.fixture(scope="module")
def host(tmp_path_factory, hermes_bin) -> Host:
    root = tmp_path_factory.mktemp("hermes")
    host = Host(home=root / "host", workspace=root / "workspace")
    host.home.mkdir()
    install_env = {**os.environ, "HERMES_HOME": str(host.home)}
    for role in ("orchestrator", "researcher"):
        argv = [hermes_bin, "profile", "install", REPO / role, "--name", role, "--yes"]
        result = run(argv, install_env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (host.profile(role) / "config.yaml").is_file()
    return host


@pytest.fixture(scope="module")
def prepared_orchestrator(hermes_bin, host):
    return prepare(hermes_bin, host, "orchestrator")


@pytest.fixture(scope="module")
def prepared_researcher(hermes_bin, host, prepared_orchestrator):
    return prepare(hermes_bin, host, "researcher")


def test_prepare_orchestrator_end_to_end(hermes_bin, host, prepared_orchestrator):
    summary = prepared_orchestrator
    assert summary["role"] == "orchestrator"
    assert summary["workspace"] == str(host.workspace)
    profile = host.profile("orchestrator")

    assert (profile / "local" / "core-path.txt").read_text().strip() == str(CORE_SRC)
    assert (profile / ".softmax-prepared").is_file()

    config = resolved_config(hermes_bin, host, "orchestrator")
    assert config["approvals"]["mode"] == "off"
    assert config["approvals"]["deny"] == []
    assert config["plugins"]["enabled"] == ["orchestrator"]
    assert config["known_plugin_toolsets"]["cron"] == ["ide-orchestrator"]
    assert config["compression"]["protect_first_n"] == 6
    assert config["compression"]["proactive_prune_tokens"] == 64000
    assert config["agent"]["disabled_toolsets"] == [
        "browser",
        "connections",
        "cronjob",
        "todo",
        "session_search",
    ]
    assert config["skills"]["external_dirs"] == [
        str(CORE_SRC / "research_core" / "skills" / "common"),
        str(CORE_SRC / "research_core" / "skills" / "orchestrator"),
    ]

    venv_python = host.workspace / "tools" / "venv" / "bin" / "python"
    assert venv_python.is_file(), summary
    probe = run([venv_python, "-c", "import research_core, coworld; print('ok')"], os.environ)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "ok"

    assert not watcher_job(host)


def test_installed_script_finds_its_profile_and_reads_its_env(hermes_bin, host):
    """The README's install: no HERMES_HOME in the environment, IDE_WORKSPACE only in ``.env``."""
    profile = host.profile("researcher")
    workspace = host.workspace.parent / "workspace-from-dotenv"
    (profile / ".env").write_text(f"IDE_WORKSPACE={workspace}\n")
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME", "IDE_WORKSPACE")}
    argv = [hermes_python(hermes_bin), profile / "scripts" / "prepare.py"]
    result = run([*argv, "--source", REPO / "researcher", "--hermes", hermes_bin, "--check"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["workspace"] == str(workspace)


def test_prepare_again_keeps_environment_without_observation_cron(
    hermes_bin, host, prepared_orchestrator
):
    stamp = host.workspace / "tools" / "venv" / ".lock-sha"
    stamp_before = stamp.read_text()
    summary = prepare(hermes_bin, host, "orchestrator")
    assert stamp.read_text() == stamp_before
    assert not any("synced" in action for action in summary["actions"]), summary
    assert not watcher_job(host)


def test_prepare_researcher_shares_the_environment(hermes_bin, host, prepared_researcher):
    summary = prepared_researcher
    assert summary["role"] == "researcher"
    assert summary["venv"] == str(host.workspace / "tools" / "venv")
    config = resolved_config(hermes_bin, host, "researcher")
    assert config["plugins"]["enabled"] == ["researcher"]
    assert config["platform_toolsets"]["api_server"] == [
        "hermes-api-server",
        "ide-researcher",
    ]
    assert config["compression"]["protect_first_n"] != 6
    assert config["compression"]["proactive_prune_tokens"] == 64000
    assert config["agent"]["disabled_toolsets"] == [
        "browser",
        "connections",
        "cronjob",
        "todo",
        "session_search",
    ]
    assert not (host.profile("researcher") / "cron" / "jobs.json").exists()
    assert (host.profile("researcher") / "local" / "core-path.txt").is_file()


def test_the_kimi_pin_yields_exactly_where_hermes_applies_an_owner_entry(hermes_bin):
    """unpinned_routing defers to Hermes's own per-model matching, run on Hermes's Python."""
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]);"
        "from research_core.prepare import unpinned_routing;"
        "keys = json.loads(sys.argv[2]);"
        "print(json.dumps({k: sorted(unpinned_routing({'models': {k: e}})) "
        "for k in keys for e in [{} if k.endswith(' (empty)') else {'only': ['x']}] "
        "for k in [k.removesuffix(' (empty)')]}))"
    )
    keys = [
        "kimi-k3.1-pro",
        "moonshotai/kimi-k3-1-pro",
        "custom/kimi-k3.1-pro",
        "MOONSHOTAI/KIMI-K3.1-PRO",
        "kimi-k3 (empty)",  # an empty entry is still the owner's: no pin for kimi-k3
    ]
    result = run([hermes_python(hermes_bin), "-c", code, CORE_SRC, json.dumps(keys)], os.environ)
    assert result.returncode == 0, result.stderr
    k3, k31 = "moonshotai/kimi-k3", "moonshotai/kimi-k3.1-pro"
    # The pins still added: an owner entry Hermes would not apply to the model does not count.
    assert json.loads(result.stdout) == {
        "kimi-k3.1-pro": [k3],
        "moonshotai/kimi-k3-1-pro": [k3],
        "custom/kimi-k3.1-pro": [k3, k31],
        "MOONSHOTAI/KIMI-K3.1-PRO": [k3, k31],
        "kimi-k3": [k31],
    }


@pytest.mark.parametrize("existing_history", [False, True])
def test_installed_agent_text_chat_and_resume(
    hermes_bin, host, prepared_orchestrator, existing_history
):
    """Real pinned agent and persistence, with only the external model replaced by HTTP."""
    script = """
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hermes_state_registry import acquire, release
from run_agent import AIAgent
from hermes_cli.middleware import apply_llm_request_middleware
from research_core import intake, layout
from research_core.plugin.executor import profile_env
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.telemetry import span
from uuid import uuid4
workspace = layout.from_env(profile_env())
request = {"model": "proof",
    "messages": [{"role": "user", "content": "Unchanged native text"}], "max_tokens": 131072}
bounded = apply_llm_request_middleware(request, provider="openrouter").payload
assert bounded == {**request, "max_tokens": 16384}, bounded
for body, key in [({"messages": []}, "max_tokens"), ({"input": []}, "max_output_tokens")]:
    request = {"model": "proof", **body}
    bounded = apply_llm_request_middleware(request, provider="openrouter").payload
    assert bounded == {**request, key: 16384}, bounded
from agent.prompt_builder import execution_guidance_text
guidance = execution_guidance_text()
assert "<mandatory_tool_use>" in guidance and "</mandatory_tool_use>" in guidance
request = {"model": "proof", "messages": [{"role": "system", "content": guidance}]}
sent = apply_llm_request_middleware(request, provider="openrouter").payload
sent = sent["messages"][0]["content"]
assert "mandatory_tool_use" not in sent and "NEVER answer" not in sent, sent
assert "<tool_persistence>" in sent and "<act_dont_ask>" in sent, sent
captured = []
class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/chat/completions") and not request.get("response_format"):
            captured.append(request)
            if len(captured) == 1:
                body = b'{"error":{"message":"fixture rate limit"}}'
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        message = {"role": "assistant", "content": "Chat received"}
        choice = {"index": 0, "finish_reason": "stop", "message": message}
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                 "prompt_tokens_details": {"cached_tokens": 5}}
        body = json.dumps({"id": "proof", "object": "chat.completion", "created": 1,
            "model": "proof", "choices": [choice],
            "usage": usage}).encode()
        if request.get("stream"):
            chunk = {"id": "proof", "object": "chat.completion.chunk", "created": 1,
                "model": "proof", "usage": usage, "choices": [
                    {"index": 0, "finish_reason": "stop", "delta": message}]}
            body = ("data: " + json.dumps(chunk) + "\\n\\ndata: [DONE]\\n\\n").encode()
        self.send_response(200)
        mime = "text/event-stream" if request.get("stream") else "application/json"
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass
server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
threading.Thread(target=server.serve_forever, daemon=True).start()
db = acquire()
sid = "openai-text-proof-" + sys.argv[1]
prior = []
if sys.argv[1] == "1":
    db.create_session(sid, "api_server", model="proof")
    db.append_message(sid, "user", "A message from before the protocol upgrade")
    db.append_message(sid, "assistant", "A saved answer from before the upgrade")
    prior = [(m["id"], m["role"], m["content"]) for m in db.get_messages(sid)]
try:
    for text in ("Hello from the OpenAI message contract", "Continue this same chat"):
        op_id = str(uuid4())
        with Store.open(workspace.store_path, kind="workspace") as store:
            with span("fixture.client_request"):
                intake.accept(store, Actor.harness(), op_id=op_id, kind="message",
                    request={"message": {"role": "user", "content": text}}, session_id=sid,
                    message=text, payload=json.dumps({"input": [{"role": "user", "content": text}],
                        "instructions": None}))
            with store.tx(Actor.harness()) as tx:
                tx.execute("UPDATE client_admissions SET dispatch_started=1 WHERE op_id=?",
                    (op_id,))
        initial = db.get_messages_as_conversation(sid, include_row_ids=True)
        agent = AIAgent(model="proof", provider="custom", api_mode="chat_completions",
            base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="fixture",
            enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
            skip_memory=True, skip_background_review=True, session_id=sid,
            session_db=db, max_iterations=2)
        result = agent.run_conversation(text, conversation_history=initial)
        assert result["final_response"] == "Chat received", result
        with Store.open(workspace.store_path, kind="workspace") as store:
            with store.tx(Actor.harness()) as tx:
                tx.execute("UPDATE client_admissions SET status='completed' WHERE op_id=?",
                    (op_id,))
        assert any(m["role"] == "user" and isinstance(m["content"], str)
            and m["content"].startswith(text) for m in captured[-1]["messages"]), captured[-1]
    saved = db.get_messages(sid)
    assert [(m["id"], m["role"], m["content"]) for m in saved[:len(prior)]] == prior
    assert [(m["role"], m["content"]) for m in saved[len(prior):]] == [
        ("user", "Hello from the OpenAI message contract"), ("assistant", "Chat received"),
        ("user", "Continue this same chat"), ("assistant", "Chat received"),
    ], saved
    print("PASS pinned native text chat and resume without content middleware or history rewrites")
finally:
    server.shutdown()
    server.server_close()
    release(db)
    # Test-only receipt barrier: application/tool paths never flush telemetry.
    from opentelemetry import trace
    assert trace.get_tracer_provider().force_flush(timeout_millis=5000)
"""
    result = run(
        [hermes_python(hermes_bin), "-c", script, str(int(existing_history))],
        host.env("orchestrator"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS pinned native text chat and resume" in result.stdout

    # Registration, retry boundaries and usage mapping are our Hermes integration contract.
    events = [
        json.loads(line) for line in result.stderr.splitlines() if line.startswith('{"name":')
    ]
    models = [e for e in events if e["name"] == "chat"]
    turns = [e for e in events if e["name"] == "invoke_agent"]
    assert len(turns) == 2 and len(models) == 3, events
    assert [e["attributes"]["gen_ai.turn.model_calls"] for e in turns] == [2, 1]
    assert [e["attributes"]["gen_ai.usage.reported"] for e in models] == [False, True, True]
    assert all(e["attributes"]["gen_ai.usage.cache_write.input_tokens"] == 0 for e in models[1:])
    admitted = [e for e in events if e["name"] == "intake.transaction"]
    assert len(admitted) == 2
    # One message, one trace: each turn is a child of the admission of the message it served.
    assert [e["context"]["trace_id"] for e in turns] == [e["context"]["trace_id"] for e in admitted]
    assert [e["parent_id"] for e in turns] == [e["context"]["span_id"] for e in admitted]
    assert [e["attributes"]["retry.count"] for e in models] == [0, 1, 0]
    assert models[0]["attributes"]["http.response.status_code"] == 429
    assert models[0]["status"]["status_code"] == "ERROR"
    assert all(e["attributes"]["gen_ai.usage.input_tokens"] == 10 for e in models[1:])
    assert all(e["attributes"]["gen_ai.usage.cache_read.input_tokens"] == 5 for e in models[1:])
    assert all(e["attributes"]["observation.complete"] for e in turns)
    assert all("Hello from the OpenAI message contract" not in json.dumps(e) for e in events)


def test_installed_agent_reads_attachment_after_resume(hermes_bin, host, prepared_orchestrator):
    """A real native read_file call, repeated after rebuilding the agent on retained history."""
    import base64

    from research_core import layout
    from research_core.attachments import prepare as prepare_attachment
    from research_core.messages import UserMessage

    message = prepare_attachment(
        UserMessage.model_validate(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read the attached notes."},
                    {
                        "type": "file",
                        "file": {
                            "filename": "retained-notes.txt",
                            "file_data": "data:text/plain;base64,"
                            + base64.b64encode(b"cedar-7381-native-read").decode(),
                        },
                    },
                ],
            }
        ),
        ws=layout.Workspace(host.workspace, host.workspace / "tools" / "venv"),
        env=host.env("orchestrator"),
        hermes_bin=str(hermes_bin),
    )
    target = next(host.workspace.rglob("retained-notes.txt"))
    script = r"""
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hermes_state_registry import acquire, release
from run_agent import AIAgent
reads = []
class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["messages"][-1]["role"] == "tool":
            assert "cedar-7381-native-read" in request["messages"][-1]["content"], request
            reads.append(request["messages"][-1]["content"])
            message = {"role": "assistant", "content": "Attachment read"}
            reason = "stop"
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "read_"+str(len(reads)), "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path":sys.argv[1]})}}]}
            reason = "tool_calls"
        choice = {"index": 0, "finish_reason": reason, "message": message}
        reply = {"id":"attachment-proof","object":"chat.completion","created":1,
            "model":"proof","choices":[choice],"usage":{
                "prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}
        body = json.dumps(reply).encode()
        if request.get("stream"):
            if "tool_calls" in message:
                message["tool_calls"][0]["index"] = 0
            reply["object"] = "chat.completion.chunk"
            reply["choices"] = [{"index":0,"finish_reason":reason,"delta":message}]
            body = ("data: "+json.dumps(reply)+"\n\ndata: [DONE]\n\n").encode()
        self.send_response(200)
        mime = "text/event-stream" if request.get("stream") else "application/json"
        self.send_header("Content-Type",mime)
        self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass
server = ThreadingHTTPServer(("127.0.0.1",0),Provider)
threading.Thread(target=server.serve_forever,daemon=True).start()
db = acquire()
try:
    for content in (json.loads(sys.argv[2])["content"], "Read that same file again."):
        agent = AIAgent(model="proof",provider="custom",api_mode="chat_completions",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",api_key="fixture",
            enabled_toolsets=["file"],quiet_mode=True,skip_context_files=True,skip_memory=True,
            skip_background_review=True,session_id="native-attachment-read-proof",session_db=db,max_iterations=3)
        result = agent.run_conversation(content,
            conversation_history=db.get_messages_as_conversation(
            "native-attachment-read-proof",include_row_ids=True))
        assert result["final_response"] == "Attachment read", result
    assert len(reads) == 2, reads
    print("PASS native file read before and after agent resume")
finally:
    server.shutdown();server.server_close();release(db)
    from opentelemetry import trace
    assert trace.get_tracer_provider().force_flush(timeout_millis=5000)
"""
    result = run(
        [hermes_python(hermes_bin), "-c", script, str(target), message.model_dump_json()],
        host.env("orchestrator"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS native file read before and after agent resume" in result.stdout

    events = [
        json.loads(line) for line in result.stderr.splitlines() if line.startswith('{"name":')
    ]
    tools = [e for e in events if e["name"] == "execute_tool"]
    assert len(tools) == 2 and all(
        e["attributes"]["gen_ai.tool.name"] == "read_file" for e in tools
    ), events
    assert all("cedar-7381-native-read" not in json.dumps(e) for e in events)


def test_disabled_toolsets_remove_the_named_tools_and_keep_the_working_ones(
    hermes_bin, host, prepared_researcher
):
    """Hermes ignores an unknown toolset name, so a rename would silently restore its tools."""
    script = r"""
import model_tools
from hermes_cli.config import load_config
from toolsets import resolve_toolset
disabled = load_config()["agent"]["disabled_toolsets"]
removed = {tool for name in disabled for tool in resolve_toolset(name)}
assert {"browser_exec", "manage_connections", "cronjob_manage", "todo_list",
    "session_search"} <= removed, removed
kept = {"terminal", "read_file", "write_file", "patch", "search_files", "skill_view",
    "skill_manage", "skills_list", "execute_code", "delegate_task", "process_manage",
    "web_extract", "web_search", "vision_analyze"}
assert not kept & removed, kept & removed
names = {tool["function"]["name"] for tool in model_tools.get_tool_definitions(
    enabled_toolsets=["hermes-api-server"], disabled_toolsets=disabled, quiet_mode=True)}
assert not names & removed, names & removed
print("PASS")
"""
    result = run([hermes_python(hermes_bin), "-c", script], host.env("researcher"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_prepared_config_prunes_an_old_tool_result_during_one_turn(
    hermes_bin, host, prepared_orchestrator, tmp_path
):
    """A real agent on the prepared config, reporting 70,000 prompt tokens: an old large result
    leaves later requests within the same turn, its row stays in place, and the original survives
    in an archived row. A small result stays as it was."""
    large = tmp_path / "large.txt"
    large.write_text("".join(f"large-file-line {line}\n" for line in range(300)))
    for index in range(22):
        text = f"small-file-{index}\n"
        if index < 5:
            text += "older output\n" * 400
        (tmp_path / f"small-{index}.txt").write_text(text)
    script = r"""
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hermes_state_registry import acquire, release
from run_agent import AIAgent
folder = sys.argv[1]
requests = []
paths = [f"{folder}/large.txt"] + [f"{folder}/small-{i}.txt" for i in range(22)]
class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Hermes also sends tool-less requests of its own, such as for a Session title.
        if request.get("tools"):
            requests.append(request)
        index = sum(m["role"] == "tool" for m in request.get("messages", []))
        if request.get("tools") and index < len(paths):
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"read_{index}",
                "type": "function", "function": {"name": "read_file",
                "arguments": json.dumps({"path": paths[index]})}}]}
            reason = "tool_calls"
        else:
            message, reason = {"role": "assistant", "content": "Finished"}, "stop"
        reply = {"id": "prune-proof", "object": "chat.completion", "created": 1, "model": "proof",
            "choices": [{"index": 0, "finish_reason": reason, "message": message}],
            "usage": {"prompt_tokens": 70000, "completion_tokens": 2, "total_tokens": 70002}}
        if request.get("stream"):
            if "tool_calls" in message:
                message["tool_calls"][0]["index"] = 0
            reply["object"] = "chat.completion.chunk"
            reply["choices"] = [{"index": 0, "finish_reason": reason, "delta": message}]
            body = ("data: " + json.dumps(reply) + "\n\ndata: [DONE]\n\n").encode()
        else:
            body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type",
            "text/event-stream" if request.get("stream") else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
threading.Thread(target=server.serve_forever, daemon=True).start()
db = acquire()
session = "prune-proof"
try:
    agent = AIAgent(model="proof", provider="custom", api_mode="chat_completions",
        base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="fixture",
        enabled_toolsets=["file"], quiet_mode=True, skip_context_files=True, skip_memory=True,
        skip_background_review=True, session_id=session, session_db=db, max_iterations=30)
    assert agent.context_compressor.proactive_prune_tokens == 64000
    assert agent.context_compressor.proactive_prune_min_result_chars == 2000
    result = agent.run_conversation("Read the files.", conversation_history=[])
    assert result["final_response"] == "Finished", result
    def tool_texts(request):
        return [m["content"] for m in request["messages"] if m["role"] == "tool"]
    assert "large-file-line 199" in tool_texts(requests[1])[0], requests[1]["messages"]
    final = tool_texts(requests[-1])
    assert "large-file-line 199" not in final[0] and len(final[0]) < 500, final[0]
    assert "small-file-6" in final[7], final[7]
    live = [m for m in db.get_messages(session) if m["role"] == "tool"]
    assert "large-file-line 199" not in live[0]["content"], live[0]
    archived = db._conn.execute(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'tool' AND active = 0",
        (session,)).fetchall()
    assert any("large-file-line 199" in row[0] for row in archived), archived
    print("PASS")
finally:
    server.shutdown()
    server.server_close()
    release(db)
"""
    result = run(
        [hermes_python(hermes_bin), "-c", script, str(tmp_path)],
        host.env("orchestrator"),
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
