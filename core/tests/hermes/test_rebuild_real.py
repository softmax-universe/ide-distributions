"""The goal of the feature, proven: a Session rebuilt from Datadog's API alone.

A real patched gateway runs a Session with tool calls, a prune and a restart. Every line it and
``research serve`` write to the stream goes to a fake Logs search. The rebuild command's output is
then compared with two sources it never reads: each request the stub model received, and each row of
that profile's ``state.db``. Both go through Hermes's scanner first, so "after redaction" is tested
too.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from research_core.agent_telemetry.encoder import message_text
from research_core.agent_telemetry.rebuild import LogsSearch, rebuild_session
from research_core.agent_telemetry.records import MAX_LINE_BYTES, SERVICE, canonical_bytes
from research_core.plugin.capture import ACCESS_LOGGER
from research_core.watcher.hermes_api import mint_session_id
from tests.hermes.capture_rig import (
    API_KEY,
    STUB_ANSWER,
    STUB_REASONING,
    STUB_USAGE,
    StubModel,
    start_gateway,
    tool_call,
)
from tests.hermes.rebuild_proof import Proof, redacted_by_hermes, run_session
from tests.hermes.test_spike_s1 import free_port, hermes_python
from tests.telemetry.conftest import objects_of, raw_lines, stored_records
from tests.telemetry.fake_datadog import FakeDatadog

USER_TOKEN = "usr_" + base64.urlsafe_b64encode(bytes(range(32))).decode()
FIRST_TEXT = f"Turn 0: read the settings. My token is {USER_TOKEN}, café 日本語 😀."
MAIN = ("stream", "request")
# Captured where request middleware sees a request: Hermes adds the stream options after it.
WIRE_ONLY = ("stream", "stream_options")


@pytest.fixture(scope="module", params=["orchestrator", "researcher"])
def proof(request, hermes_bin, tmp_path_factory):
    profile = request.param
    session = run_session(
        tmp_path_factory.mktemp(f"rebuild-{profile}"),
        hermes_python(hermes_bin),
        profile,
        before_restart=16,
        after_restart=3,
        result_chars=20000,
        first_text=FIRST_TEXT,
    )
    try:
        yield session
    finally:
        session.datadog.close()


def rebuilt(proof: Proof) -> dict:
    return rebuild_session(LogsSearch(proof.datadog.url, "api-key", "app-key"), proof.session_id)


@pytest.fixture(scope="module")
def report(proof):
    return rebuilt(proof)


def main_requests(report: dict) -> list[dict]:
    return [r for r in report["requests"] if r["native_event_type"] in MAIN]


def state_rows(proof: Proof) -> list[dict]:
    connection = sqlite3.connect(proof.state_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (proof.session_id,)
        ).fetchall()
    finally:
        connection.close()
    # A ``session_meta`` row is Hermes's own note and no message. It never reaches a model.
    return [dict(row) for row in rows if row["role"] != "session_meta"]


def test_the_session_did_what_this_proof_claims(proof, report):
    main = main_requests(report)
    instances = [r["source_instance"] for r in main]
    assert len(set(instances)) == 2
    first_process = [r["operation"] for r in main if r["source_instance"] == instances[0]]
    assert "full" in first_process[1:], "Hermes did not prune, so the proof would not cover a prune"
    after_restart = next(r for r in main if r["source_instance"] == instances[-1])
    assert after_restart["operation"] == "full", "the restart should start a new baseline"
    # The first request is what Hermes really builds for hosted workspaces, seeded secret included.
    body = proof.stub.main_requests()[0]
    assert USER_TOKEN in json.dumps(body)
    system = body["messages"][0]
    assert system["role"] == "system" and len(json.dumps(system["content"])) > 1000
    assert "cache_control" in json.dumps(system["content"])
    assert "settings" in {tool["function"]["name"] for tool in body["tools"]}
    assert body["stream"] is True and "stream_options" in body
    assert body["provider"] == {"only": ["moonshotai"]}
    # An ordinary conversation extends the last manifest, whatever the cache markers do.
    operations = [r["operation"] for r in report["requests"]]
    assert operations.count("append") > operations.count("full")


def test_every_line_on_the_stream_is_one_whole_record_within_the_limit(proof, report):
    lines = raw_lines(proof.gateway.stream)
    # ``raw_lines`` leaves out each line's leading newline, which counts toward the limit.
    assert max(len(b"\n" + line) for line in lines) <= MAX_LINE_BYTES
    records = [json.loads(line) for line in lines]
    assert {record["service"] for record in records} == {SERVICE}
    assert {record["workspace_id"] for record in records} == {"wrk-proof"}
    assert {record["producer"] for record in records} >= {"gateway", "research-serve"}
    assert not any("message" in record for record in records)
    assert USER_TOKEN.encode() not in b"".join(lines)
    assert "[redacted-softmax-token]" in json.dumps(report["requests"][0]["request"])
    # What every record says of where it came from, and what only the gateway writes.
    gateway = [record for record in records if record["producer"] == "gateway"]
    assert {record["image"] for record in gateway} == {"image-under-test"}
    assert {record["runtime_id"] for record in gateway} == {"runtime-under-test"}
    assert all(record["redaction_version"].startswith("hermes-") for record in gateway)
    logged = [record for record in gateway if record.get("action") == "diagnostic"]
    assert logged


def test_a_clean_session_has_nothing_missing(report):
    assert report["missing"] == []


def test_each_request_equals_what_the_stub_received_in_wire_order(hermes_bin, proof, report):
    with proof.stub.lock:
        received = [json.loads(body) for body in proof.stub.bodies]
    expected = redacted_by_hermes(hermes_bin, received)
    main = [body for body in expected if body.get("tools")]
    auxiliary = [body for body in expected if not body.get("tools")]
    got_main = [r["request"] for r in main_requests(report)]
    got_auxiliary = [r["request"] for r in report["requests"] if r["native_event_type"] not in MAIN]
    assert len(got_main) == len(main)
    for position, (got, want) in enumerate(zip(got_main, main, strict=True)):
        want = {k: v for k, v in want.items() if k not in WIRE_ONLY}
        assert canonical_bytes(got) == canonical_bytes(want), f"main request {position}"
    # An auxiliary call runs beside the turn: only its text is held to the wire, not its place.
    assert sorted(map(canonical_bytes, got_auxiliary)) == sorted(map(canonical_bytes, auxiliary))


def _comparable(row: dict) -> dict:
    """What a committed row and a message both hold, whichever shape it has."""
    content = row.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content)
    calls = row.get("tool_calls")
    if isinstance(calls, str):
        calls = json.loads(calls)
    return {
        "role": row.get("role"),
        "content": content or "",
        "tool_call_id": row.get("tool_call_id"),
        "tool_calls": [
            {
                "id": call["id"],
                "name": call["function"]["name"],
                # Hermes sends arguments as compact JSON, and the row keeps them as the model wrote
                # them. Both parse the same, and ``tool`` / ``proposed`` keeps the original.
                "arguments": json.loads(call["function"]["arguments"]),
            }
            for call in calls or ()
        ],
        "reasoning": row.get("reasoning_content") or None,
    }


def _split_by_prune(proof: Proof) -> tuple[list[dict], dict[str, dict]]:
    """Hermes's prune rewrites a row in place, and keeps the original as a new ``compacted`` row
    with an ID of its own and no commit callback. Returns live rows, and originals by tool call."""
    rows = state_rows(proof)
    return (
        [row for row in rows if not row["compacted"]],
        {row["tool_call_id"]: row for row in rows if row["compacted"]},
    )


def test_each_committed_row_equals_state_db(hermes_bin, proof, report):
    live, originals = _split_by_prune(proof)
    assert originals, "Hermes did not prune"
    got = {row["hermes_message_id"]: row for row in report["rows"]}
    assert sorted(got, key=int) == [str(row["id"]) for row in live]
    listed = {
        row for request in main_requests(report) for row in request["hermes_message_ids"].values()
    }
    assert listed and listed <= set(got)

    expected = redacted_by_hermes(hermes_bin, [_comparable(row) for row in live])
    for row, want in zip(live, expected, strict=True):
        record = got[str(row["id"])]
        assert record["kind"] == row["role"]
        assert record.get("tool_name") == row["tool_name"]
        assert record.get("finish_reason") == row["finish_reason"]
        have = _comparable(record.get("rewritten_to") or record["message"])
        if row["role"] == "user":
            # Context the plugin adds joins the text on the wire and not the row.
            assert want["content"] in have["content"], row["id"]
        else:
            assert have == want, row["id"]

    # What a pruned row said before the prune is the first form the rebuild shows.
    by_call = {row.get("tool_call_id"): row for row in report["rows"]}
    calls = sorted(originals)
    first_forms = redacted_by_hermes(hermes_bin, [_comparable(originals[c]) for c in calls])
    for call, want in zip(calls, first_forms, strict=True):
        assert "rewritten_to" in by_call[call]
        assert _comparable(by_call[call]["message"]) == want


def test_tool_results_equal_state_db_and_what_the_model_got(hermes_bin, proof, report):
    live, originals = _split_by_prune(proof)
    tools = [originals.get(row["tool_call_id"], row) for row in live if row["role"] == "tool"]
    expected = redacted_by_hermes(hermes_bin, [row["content"] for row in tools])
    results = {
        tool["tool_call_id"]: tool["result"]
        for request in report["requests"]
        for tool in request["tools"]
        if "result" in tool
    }
    assert [results[row["tool_call_id"]] for row in tools] == expected
    finished = [tool for request in report["requests"] for tool in request["tools"]]
    assert sum(tool["stage"] == "completed" for tool in finished) == len(tools)
    assert report["unattached_tools"] == []
    with proof.stub.lock:
        sent = [json.loads(body) for body in proof.stub.bodies]
    seen = {}
    for body in sent:
        for message in body["messages"]:
            if message["role"] == "tool":
                seen.setdefault(message["tool_call_id"], message_text(message))
    wire = redacted_by_hermes(hermes_bin, [seen[row["tool_call_id"]] for row in tools])
    assert wire == expected


def test_the_operation_chain_joins_admission_run_and_requests(proof, report):
    admissions: dict[str, list[dict]] = {}
    for event in report["events"]:
        if "admission_id" in event:
            admissions.setdefault(event["admission_id"], []).append(event)
    started = [e for e in report["events"] if e.get("native_event_type") == "client.started"]
    assert [e["run_id"] for e in started] == proof.run_ids
    for events in admissions.values():
        kinds = [event.get("native_event_type") for event in events]
        assert "client.started" in kinds and "client.finished" in kinds, kinds
    gateway_runs = {r["run_id"] for r in main_requests(report)}
    assert gateway_runs == set(proof.run_ids)
    tools = [tool for request in report["requests"] for tool in request["tools"]]
    assert {tool["run_id"] for tool in tools} == set(proof.run_ids)
    assert {row["run_id"] for row in report["rows"]} == set(proof.run_ids)


def test_after_a_restart_prompt_sections_are_kept(report):
    """Sections are found in each request's system prompt, so a stored prompt keeps them."""
    main = main_requests(report)
    before, after = main[0], main[-1]
    assert before["source_instance"] != after["source_instance"]

    def sources(request: dict) -> set[str]:
        return {row["source"] for row in request["tokens_by_source"]}

    assert "soul" in sources(before) and "soul" in sources(after)
    assert before["request"]["messages"][0] == after["request"]["messages"][0]


def test_token_estimates_add_up_to_the_providers_count(report):
    for request in report["requests"]:
        estimates = [row["estimated_tokens"] for row in request["tokens_by_source"]]
        prompt_tokens = request["response"]["usage"]["input_tokens"]
        assert abs(sum(estimates) - prompt_tokens) <= len(estimates)


def test_replies_keep_their_reasoning_and_usage(report):
    """Hermes's ``post_api_request`` passes no provider-billed cost, so there is none."""
    first, second = main_requests(report)[:2]
    message = first["response"]["message"]
    assert message["reasoning_content"] == STUB_REASONING
    assert [call["id"] for call in message["tool_calls"]] == ["call_read"]
    assert second["response"]["message"]["content"] == STUB_ANSWER
    response = first["response"]
    assert response["partial"] is False and response["outcome"] == "completed"
    assert response["finish_reason"] == "tool_calls"
    assert response["duration_ms"] >= response["time_to_first_token_ms"] >= 0
    details = STUB_USAGE["completion_tokens_details"]
    assert response["usage"]["reasoning_tokens"] == details["reasoning_tokens"]
    assert "cost" not in response


def test_a_reused_tool_call_id_joins_through_the_model_request(report):
    """Kimi and this stub reuse IDs across turns. Hermes renames the repeat after the attempt ends,
    so a proposal holds the provider's ID and arguments as sent, and later records hold ``_d2``."""
    asking = [r for r in main_requests(report) if r["tools"]]
    assert len(asking) > 2
    for request in asking:
        proposed, completed, result = request["tools"]
        assert (proposed["stage"], completed["stage"], result["stage"]) == (
            "proposed",
            "completed",
            "result",
        )
        assert proposed["tool_call_id"] == "call_read"
        assert proposed["arguments"] == '{"action": "read"}'
        assert completed["tool_call_id"] == result["tool_call_id"]
        assert result["hermes_message_id"].isdigit()
    assert asking[1]["tools"][1]["tool_call_id"] == "call_read_d2"


def test_prompt_sources_and_context_parts_are_attributed(proof, report):
    objects = objects_of(stored_records(proof.gateway.stream))
    first = main_requests(report)[0]
    manifest = json.loads(objects[first["manifest_digest"]])
    sections = json.loads(objects[manifest["prompt_sections_digest"]])["sections"]
    system = message_text(first["request"]["messages"][0]).encode()
    plugin = next(section for section in sections if section["name"] == "plugin_sections")
    start = plugin["byte_offset"]
    # The plugin's own text, after the heading Hermes gives it.
    assert b"Plugin Context: ide-briefing" in system[:start] and plugin["byte_length"] > 0
    assert {section["name"] for section in sections} >= {"soul", "plugin_sections"}
    # Hermes's own text (guidance, project rules, the skill index) is in no section.
    chars = {row["source"]: row["chars"] for row in first["tokens_by_source"]}

    # Only the Orchestrator's turn hook adds to a user message, so only it reports the parts.
    if proof.profile != "orchestrator":
        return
    assert "user_text" in chars
    text = next(m for m in first["request"]["messages"] if m["role"] == "user")
    place = next(c for c in report["context"] if c["source_type"] == "user_text")
    data = message_text(text).encode()
    assert hashlib.sha256(data).hexdigest() == place["target_digest"]
    assert place["target_digest"] not in objects
    assert data[place["byte_offset"] :].decode().startswith("Turn 0")


def test_an_auxiliary_call_is_captured_whole(report):
    auxiliary = [r for r in report["requests"] if r["native_event_type"] not in MAIN]
    assert auxiliary
    assert auxiliary[0]["response"]["message"]["content"] == "a title"


@pytest.fixture(scope="module")
def refusals(hermes_bin, tmp_path_factory):
    """One refused call per turn, repeated, so that the middleware drops the earlier ones."""
    session = run_session(
        tmp_path_factory.mktemp("refusals"),
        hermes_python(hermes_bin),
        "orchestrator",
        before_restart=3,
        after_restart=0,
        result_chars=0,
        tool_calls=[tool_call(0, "call_refused", "settings", '{"action": "no-such-action"}')],
    )

    try:
        yield session
    finally:
        session.datadog.close()


def test_a_refused_call_the_middleware_drops_keeps_its_own_row(hermes_bin, refusals):
    report = rebuilt(refusals)
    assert report["missing"] == []
    last = refusals.stub.main_requests()[-1]
    roles = [message["role"] for message in last["messages"]]
    assert roles.count("user") == 3 and roles.count("tool") == 1, "the middleware dropped nothing"
    stages = {tool["stage"] for request in report["requests"] for tool in request["tools"]}
    assert "refused" in stages and "completed" not in stages

    rows = [row for row in state_rows(refusals) if row["role"] == "tool" or row["tool_calls"]]
    assert len(rows) == 6
    expected = redacted_by_hermes(hermes_bin, [_comparable(row) for row in rows])
    got = {row["hermes_message_id"]: row for row in report["rows"]}
    for row, want in zip(rows, expected, strict=True):
        record = got[str(row["id"])]
        assert "rewritten_to" not in record, row["id"]
        assert _comparable(record["message"]) == want


@pytest.mark.parametrize("profile", ["orchestrator", "researcher"])
def test_a_guard_halt_rebuilds_as_committed_beside_real_stdout(hermes_bin, tmp_path, profile):
    """A failing tool until Hermes's guard ends the turn with its own text and no further request,
    on a stream the gateway's own unbuffered prints share, as in the image."""
    stub = StubModel([tool_call(0, "call_refused", "settings", '{"action": "no-such-action"}')])
    stub.repeat_tool_calls = True
    gateway = start_gateway(
        tmp_path, hermes_python(hermes_bin), stub.url, free_port(), stdout_on_stream=True
    )
    session_id = mint_session_id()
    try:
        gateway.request("POST", profile, "/api/sessions", {"id": session_id})
        run_id = gateway.run_turn(profile, session_id, "keep trying")
    finally:
        gateway.stop()
        stub.close()
    lines = raw_lines(gateway.stream)
    telemetry = [line for line in lines if SERVICE.encode() in line]
    assert telemetry and all(json.loads(line)["service"] == SERVICE for line in telemetry)
    ordinary = b"".join(line for line in lines if SERVICE.encode() not in line)
    assert b"I stopped retrying" in ordinary, "the guard's print should share the stream"

    datadog = FakeDatadog()
    datadog.take_lines(lines)
    report = rebuild_session(LogsSearch(datadog.url, "a", "b"), session_id)
    datadog.close()
    assert report["missing"] == []
    ended = [e for e in report["events"] if (e["action"], e["stage"]) == ("turn", "ended")]
    assert [e["reason"] for e in ended] == ["guardrail_halt"]

    rows = state_rows(Proof(profile, session_id, stub, gateway, datadog, [run_id]))
    assert rows[-1]["role"] == "assistant" and "I stopped retrying" in rows[-1]["content"]
    assert rows[-2]["role"] == "tool" and rows[-3]["tool_calls"]
    got = {row["hermes_message_id"]: row for row in report["rows"]}
    assert sorted(got, key=int) == [str(row["id"]) for row in rows]
    want = redacted_by_hermes(hermes_bin, [_comparable(row) for row in rows])
    for row, expected in zip(rows, want, strict=True):
        have = _comparable(got[str(row["id"])]["message"])
        if row["role"] == "user":
            assert expected["content"] in have["content"], row["id"]
        else:
            assert have == expected, row["id"]
    # Requests are what the stub received, and the halt added none.
    received = [
        {k: v for k, v in body.items() if k not in WIRE_ONLY}
        for body in redacted_by_hermes(hermes_bin, stub.main_requests())
    ]
    sent = [r["request"] for r in main_requests(report)]
    assert list(map(canonical_bytes, sent)) == list(map(canonical_bytes, received))


def test_direct_gateway_calls_have_access_lines_and_a_run_poll_has_none(hermes_bin, tmp_path):
    stub = StubModel([])
    gateway = start_gateway(tmp_path, hermes_python(hermes_bin), stub.url, free_port())
    session_id = mint_session_id()
    profile = "researcher"
    try:
        gateway.request("POST", profile, "/api/sessions", {"id": session_id})
        run_id = gateway.run_turn(profile, session_id, "hello")
        messages = f"/api/sessions/{session_id}/messages"
        gateway.request("GET", profile, messages)
        for call in ("approval", "stop"):
            # The run has ended, so the gateway refuses these. A refusal has its access line too.
            with contextlib.suppress(urllib.error.HTTPError):
                gateway.request("POST", profile, f"/v1/runs/{run_id}/{call}", {})
        # A poll that fails, and the AIDE backend's run read, which is not the seam's poll.
        with contextlib.suppress(urllib.error.HTTPError):
            gateway.request("GET", profile, "/v1/runs/no-such-run")
        aide_read = urllib.request.Request(
            f"http://127.0.0.1:{gateway.port}/p/{profile}/v1/runs/{run_id}",
            headers={"Authorization": f"Bearer {API_KEY}", "User-Agent": "python-httpx/0.28.1"},
        )
        urllib.request.urlopen(aide_read, timeout=30).close()
    finally:
        gateway.stop()
    records = stored_records(gateway.stream)
    objects = objects_of(records)
    access = [
        objects[record["content_refs"][0]["digest"]].decode()
        for record in records
        if record.get("native_event_type") == ACCESS_LOGGER
    ]
    for call in (f"GET /p/{profile}{messages} ", f"/v1/runs/{run_id}/approval ", "/stop "):
        assert any(call in line for line in access), (call, access)
    assert any("/v1/runs/no-such-run " in line and '" 404 ' in line for line in access), access
    polls = [line for line in access if f"GET /p/{profile}/v1/runs/{run_id} " in line]
    assert len(polls) == 1 and "python-httpx" in polls[0], polls


def test_a_kill_during_a_call_keeps_its_request(hermes_bin, tmp_path):
    """The request is written before the call, so a gateway killed during it keeps the request.
    SIGKILL loses what the writer still had queued, so the test waits for it on the stream."""
    stub = StubModel([tool_call(0, "call_read", "settings", '{"action": "read"}')])
    stub.hang_tool_followups = threading.Event()
    gateway = start_gateway(tmp_path, hermes_python(hermes_bin), stub.url, free_port())
    session_id = mint_session_id()
    request = b'"action":"model_attempt","stage":"request"'
    try:
        gateway.request("POST", "researcher", "/api/sessions", {"id": session_id})
        gateway.request(
            "POST", "researcher", "/v1/runs", {"input": "read it", "session_id": session_id}
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            calls = sum(request in line for line in raw_lines(gateway.stream))
            if calls == 2 and stub.tool_followups_at:
                break
            time.sleep(0.05)
        gateway.process.send_signal(signal.SIGKILL)
        gateway.process.wait(timeout=30)
    finally:
        stub.hang_tool_followups.set()
        stub.close()
    datadog = FakeDatadog()
    datadog.take_lines(raw_lines(gateway.stream))

    report = rebuild_session(LogsSearch(datadog.url, "a", "b"), session_id)
    datadog.close()
    first, second = main_requests(report)
    assert first["response"] and not second.get("response")
    assert second["request"]["messages"][-1]["role"] == "tool"
    assert not [p for p in report["missing"] if p["kind"] == "call_with_no_attempt"]


def test_a_kill_during_the_first_call_keeps_the_users_message(hermes_bin, tmp_path):
    """The first request holds the user's message before the model answers."""
    stub = StubModel([])
    stub.hang_user_requests = threading.Event()
    gateway = start_gateway(tmp_path, hermes_python(hermes_bin), stub.url, free_port())
    session_id = mint_session_id()
    request = b'"action":"model_attempt","stage":"request"'
    try:
        gateway.request("POST", "researcher", "/api/sessions", {"id": session_id})
        gateway.request(
            "POST", "researcher", "/v1/runs", {"input": "keep this", "session_id": session_id}
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any(request in line for line in raw_lines(gateway.stream)):
                break
            time.sleep(0.05)
        gateway.process.send_signal(signal.SIGKILL)
        gateway.process.wait(timeout=30)
    finally:
        stub.hang_user_requests.set()
        stub.close()
    datadog = FakeDatadog()
    datadog.take_lines(raw_lines(gateway.stream))

    report = rebuild_session(LogsSearch(datadog.url, "a", "b"), session_id)
    datadog.close()
    [first] = main_requests(report)
    assert "keep this" in message_text(first["request"]["messages"][-1])
