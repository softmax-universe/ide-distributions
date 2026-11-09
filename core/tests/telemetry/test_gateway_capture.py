"""The plugin's callbacks, called as Hermes calls them, with the real writer and a stand-in scanner.

``core/tests/hermes/test_rebuild_real.py`` runs the same code on a real gateway. These cover what a
stub model there does not produce: failed streams, retries, compaction, approvals and odd values.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_core.plugin.capture import (
    ACCESS_LOGGER,
    NO_SESSION,
    DiagnosticsHandler,
    GatewayCapture,
    row_keys,
    wire_request,
    with_row_ids,
)
from research_core.plugin.hermes_prompt import without_mandatory_tool_use
from tests.telemetry.conftest import (
    HERMES_STYLE_SECRET,
    objects_of,
    rebuild_request,
    scrub,
    stored_records,
)

CORE_SRC = Path(__file__).resolve().parents[2] / "src"
REQUEST = {
    "model": "moonshot/kimi-k3.1-pro",
    "messages": [
        {"role": "system", "content": "SOUL TEXT\n\nPLUGIN TEXT"},
        {"role": "user", "content": "hello\n\n[session Coworld] none"},
    ],
    "tools": [{"type": "function", "function": {"name": "goal", "parameters": {}}}],
    "stream": True,
    "stream_options": {"include_usage": True},
    "extra_body": {"provider": {"only": ["moonshotai"]}, "reasoning": {"effort": "low"}},
    "extra_headers": {"X-OpenRouter-Cache": "false"},
}
HISTORY = [
    {"role": "user", "content": "hello", "_row_id": 11},
]
STARTED = 1793817074.0


@pytest.fixture
def capture(writer) -> GatewayCapture:
    return GatewayCapture(writer, "orchestrator")


def _attempt(capture: GatewayCapture, **overrides) -> None:
    capture.on_model_attempt(
        **{
            "session_id": "ses_1",
            "request": REQUEST,
            "response": SimpleNamespace(
                id="gen-1",
                model="moonshot/kimi-k3.1-pro",
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(role="assistant", content="hi", reasoning="think"),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, cost=0.5),
            ),
            "outcome": "completed",
            "kind": "stream",
            "attempt": 1,
            "started_at": STARTED,
            "ended_at": STARTED + 2,
            "first_chunk_at": STARTED + 0.5,
            "provider": "openrouter",
            "api_request_id": "turn-1:api:1",
            "call_role": "primary",
            "run_id": "run_1",
            **overrides,
        }
    )


def _flushed(writer, stream_file) -> list[dict]:
    assert writer.flush(5.0)
    return stored_records(stream_file)


def _operations(records: list[dict], action: str, stage: str | None = None) -> list[dict]:
    return [
        record
        for record in records
        if record["family"] == "operation"
        and record["action"] == action
        and stage in (None, record["stage"])
    ]


def _digest(record: dict, name: str) -> str:
    return next(ref["digest"] for ref in record["content_refs"] if ref["name"] == name)


def test_wire_request_is_the_json_body_the_sdk_sends():
    wire = wire_request(REQUEST)
    assert "extra_body" not in wire and "extra_headers" not in wire
    assert wire["provider"] == {"only": ["moonshotai"]}
    assert wire["reasoning"] == {"effort": "low"}
    assert wire["stream"] is True


def test_row_ids_match_through_a_dropped_row_and_tool_ids():
    history = [
        {"role": "user", "_row_id": 1},
        {"role": "assistant", "_row_id": 2},
        {"role": "tool", "tool_call_id": "a", "_row_id": 3},
        {"role": "tool", "tool_call_id": "b", "_row_id": 4},
        {"role": "assistant"},
    ]
    wire = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "tool_call_id": "b", "content": "only b reached the wire"},
        {"role": "assistant", "content": "z"},
    ]
    bound = with_row_ids(wire, row_keys(history))
    assert [message.get("_row_id") for message in bound] == [None, 1, 2, 4, None]
    assert "_row_id" not in wire[1]


def test_a_retried_stream_is_one_record_per_attempt_and_keeps_its_partial_text(
    capture, writer, stream_file
):
    parts = {
        "content": "partial answer",
        "reasoning": "thinking before the drop",
        "refusal": None,
        "reasoning_details": None,
        "tool_calls": [{"id": "call_cut", "name": "goal", "arguments": '{"cut": "he'}],
    }
    _attempt(
        capture,
        response=None,
        outcome="failed",
        stream_parts=parts,
        error="connection dropped",
        error_type="APIConnectionError",
        status_code=None,
    )
    _attempt(capture, attempt=2)
    records = _flushed(writer, stream_file)
    objects = objects_of(records)
    requests = _operations(records, "model_attempt", "request")
    assert len(requests) == 2
    assert requests[0]["model_attempt_id"] != requests[1]["model_attempt_id"]

    responses = _operations(records, "model_attempt", "response")
    assert responses[0]["partial"] is True
    partial = json.loads(objects[_digest(responses[0], "message")])
    assert partial["content"] == "partial answer"
    assert partial["reasoning_content"] == "thinking before the drop"
    assert partial["tool_calls"][0]["function"]["arguments"] == '{"cut": "he'
    assert responses[0]["outcome"] == "failed"
    assert responses[0]["reason"] == "apiconnectionerror"

    failed = _operations(records, "model_attempt", "failed")[0]
    assert objects[_digest(failed, "error")] == b"connection dropped"
    # A stream that failed proposed nothing Hermes will run.
    assert not _operations(records, "tool", "proposed")


def test_a_failed_attempt_with_no_text_keeps_its_outcome(capture, writer, stream_file):
    _attempt(capture, response=None, outcome="failed", error="", error_type="TimeoutError")
    [failed] = _operations(_flushed(writer, stream_file), "model_attempt", "failed")
    assert (failed["outcome"], failed["reason"]) == ("failed", "timeouterror")
    assert "content_refs" not in failed


def test_a_failed_attempt_with_nothing_delivered_still_has_its_request_and_error(
    capture, writer, stream_file
):
    _attempt(
        capture,
        response=None,
        outcome="failed",
        stream_parts=None,
        error="429 too many",
        error_type="RateLimitError",
        status_code=429,
    )
    records = _flushed(writer, stream_file)
    assert _operations(records, "model_attempt", "request")
    assert not _operations(records, "model_attempt", "response")
    failed = _operations(records, "model_attempt", "failed")[0]
    assert failed["details"]["status_code"] == 429


def test_proposals_use_a_streams_raw_arguments(capture, writer, stream_file):
    parts = {"tool_calls": [{"id": "call_1", "name": "goal", "arguments": '{"a": 1'}]}
    _attempt(capture, stream_parts=parts)
    records = _flushed(writer, stream_file)
    proposal = _operations(records, "tool", "proposed")[0]
    assert proposal["tool_call_id"] == "call_1"
    assert objects_of(records)[_digest(proposal, "arguments")] == b'{"a": 1'


def test_proposals_from_a_whole_response(capture, writer, stream_file):
    message = SimpleNamespace(
        role="assistant",
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_2",
                type="function",
                function=SimpleNamespace(name="task", arguments="{}"),
            )
        ],
    )
    response = SimpleNamespace(
        model="m",
        usage=None,
        choices=[SimpleNamespace(finish_reason="tool_calls", message=message)],
    )
    _attempt(capture, kind="request", response=response)
    proposal = _operations(_flushed(writer, stream_file), "tool", "proposed")[0]
    assert proposal["tool_call_id"] == "call_2" and proposal["tool_name"] == "task"


def test_a_tool_result_is_what_the_model_got_and_not_the_original(capture, writer, stream_file):
    original = "x" * 1000
    capture.post_tool_call(
        tool_name="terminal",
        args={"command": "ls"},
        result=original,
        session_id="ses_1",
        tool_call_id="call_1",
        duration_ms=12,
        status="ok",
        run_id="run_1",
    )
    capture.on_tool_results_final(
        session_id="ses_1",
        task_id="task-1",
        results=[
            {
                "tool_call_id": "call_1",
                "tool_name": "terminal",
                "content": "[saved to a file]",
                "row_id": 7,
            }
        ],
        run_id="run_1",
    )
    records = _flushed(writer, stream_file)
    assert original not in json.dumps(records)
    completed = _operations(records, "tool", "completed")[0]
    assert completed["duration_ms"] == 12 and completed["outcome"] == "ok"
    objects = objects_of(records)
    assert json.loads(objects[_digest(completed, "arguments")]) == {"command": "ls"}
    final = _operations(records, "tool", "result")[0]
    assert objects[_digest(final, "result")] == b"[saved to a file]"
    assert final["hermes_message_id"] == "7"


def test_a_multimodal_tool_result_stays_a_list(capture, writer, stream_file):
    content = [{"type": "text", "text": "look"}]
    capture.on_tool_results_final(
        session_id="ses_1",
        results=[{"tool_call_id": "c", "tool_name": "v", "content": content, "row_id": None}],
    )
    records = _flushed(writer, stream_file)
    final = _operations(records, "tool", "result")[0]
    assert json.loads(objects_of(records)[_digest(final, "result")]) == content
    assert "hermes_message_id" not in final


@pytest.mark.parametrize(
    ("status", "result", "stage"),
    [
        ("error", json.dumps({"ok": False, "error": "no", "problems": ["no"]}), "refused"),
        ("blocked", "blocked by a hook", "refused"),
        ("error", json.dumps({"error": "the disk is full"}), "completed"),
        ("ok", json.dumps({"ok": False, "problems": ["not a failure Hermes saw"]}), "completed"),
    ],
)
def test_a_refusal_is_told_from_a_failure(capture, writer, stream_file, status, result, stage):
    capture.post_tool_call(tool_name="goal", args={}, result=result, session_id="s", status=status)
    assert _operations(_flushed(writer, stream_file), "tool", stage)


def test_commits_name_native_ids_and_times(capture, writer, stream_file):
    call = {"id": "c", "type": "function", "function": {"name": "goal", "arguments": "{}"}}
    capture.on_messages_committed(
        session_id="ses_1",
        rows=[
            {
                "_row_id": 3,
                "role": "assistant",
                "content": f"text {HERMES_STYLE_SECRET}",
                "finish_reason": "stop",
                "reasoning_content": "because",
                "tool_calls": [call],
                "timestamp": 1.0,
            },
            {"_row_id": 4, "role": "tool", "content": "result", "tool_call_id": "c"},
            {"_row_id": 5, "role": "user", "content": "user text"},
        ],
        committed_at=STARTED,
        run_id="run_1",
    )
    records = _flushed(writer, stream_file)
    first, second, third = _operations(records, "message", "committed")
    assert first["hermes_message_id"] == "3" and first["kind"] == "assistant"
    assert first["occurred_at"] == "2026-11-04T18:31:14.000Z"
    assert second["tool_call_id"] == "c"
    # A row carries its own text, scanned. A tool row's is on its result.
    assert HERMES_STYLE_SECRET not in json.dumps(records)
    assert json.loads(objects_of(records)[_digest(first, "message")]) == {
        "role": "assistant",
        "content": scrub(f"text {HERMES_STYLE_SECRET}"),
        "reasoning_content": "because",
        "tool_calls": [call],
    }
    assert "content_refs" not in second and '"result"' not in json.dumps(records)
    assert json.loads(objects_of(records)[_digest(third, "message")]) == {
        "role": "user",
        "content": "user text",
    }


def test_prompt_sections_are_byte_ranges_of_the_redacted_prompt(capture, writer, stream_file):
    prompt = f"SOUL {HERMES_STYLE_SECRET} é\n\nPLUGIN TEXT"
    boundary = prompt.index("\n\n")
    capture.on_system_prompt_built(
        session_id="ses_1",
        system_prompt=prompt,
        sections=[
            {"source": "soul", "start": 0, "end": boundary},
            {
                "source": "plugin_sections",
                "start": boundary + 2,
                "end": len(prompt),
            },
        ],
        run_id="run_1",
    )
    request = {"model": "m", "messages": [{"role": "system", "content": prompt}]}
    _attempt(capture, request=request)
    records = _flushed(writer, stream_file)
    assert HERMES_STYLE_SECRET not in json.dumps(records)
    objects = objects_of(records)
    manifest = json.loads(
        objects[_digest(_operations(records, "model_attempt", "request")[0], "request_manifest")]
    )
    sections = json.loads(objects[manifest["prompt_sections_digest"]])["sections"]
    system = json.loads(objects[manifest["messages"][0]])["content"].encode()
    named = {section["name"]: section for section in sections}
    plugin = named["plugin_sections"]
    start = plugin["byte_offset"]
    assert system[start : start + plugin["byte_length"]] == b"PLUGIN TEXT"
    built = _operations(records, "system_prompt", "built")[0]
    assert built["details"] == {"sections": 2}


def test_a_system_prompt_cut_in_two_for_cache_markers_keeps_every_source(
    capture, writer, stream_file
):
    prompt = "STABLE TEXT\n\nPLUGIN TEXT"
    capture.on_system_prompt_built(
        session_id="ses_1",
        system_prompt=prompt,
        sections=[
            {"source": "soul", "start": 0, "end": 11},
            {"source": "plugin_sections", "start": 13, "end": len(prompt)},
        ],
    )
    # As ``agent/prompt_caching.py`` cuts it: the second part starts right after the first.
    cut = {
        "role": "system",
        "content": [
            {"type": "text", "text": prompt[:11], "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": prompt[11:]},
        ],
    }
    request = {"model": "m", "messages": [cut]}
    _attempt(capture, request=request)
    records = _flushed(writer, stream_file)
    objects = objects_of(records)
    digest = _digest(_operations(records, "model_attempt", "request")[0], "request_manifest")
    assert rebuild_request(digest, objects) == request
    assert prompt.encode() not in objects.values()
    body = json.loads(objects[json.loads(objects[digest])["prompt_sections_digest"]])
    assert set(body) == {"sections"}
    assert [section["name"] for section in body["sections"]] == ["soul", "plugin_sections"]


def test_a_block_the_middleware_drops_leaves_every_source_attributed(capture, writer, stream_file):
    """``hermes_prompt`` drops a block from the middle of a section of the request, not the stored
    prompt. That section keeps what was sent, and the dropped text is credited to no source."""
    block = "<mandatory_tool_use>\nUSE A TOOL\n</mandatory_tool_use>\n\n"
    guidance = f"GUIDANCE BEFORE\n{block}GUIDANCE AFTER"
    prompt = f"SOUL TEXT\n\n{guidance}\n\nPLUGIN TEXT"
    start = prompt.index(guidance)
    capture.on_system_prompt_built(
        session_id="ses_1",
        system_prompt=prompt,
        sections=[
            {"source": "soul", "start": 0, "end": 9},
            {"source": "tool_guidance", "start": start, "end": start + len(guidance)},
            {"source": "plugin_sections", "start": len(prompt) - 11, "end": len(prompt)},
        ],
    )
    request = {"model": "m", "messages": [{"role": "system", "content": prompt}]}
    sent = without_mandatory_tool_use(request=request)["request"]
    _attempt(capture, request=sent)
    records = _flushed(writer, stream_file)
    objects = objects_of(records)
    manifest = json.loads(
        objects[_digest(_operations(records, "model_attempt", "request")[0], "request_manifest")]
    )
    sections = json.loads(objects[manifest["prompt_sections_digest"]])["sections"]
    system = json.loads(objects[manifest["messages"][0]])["content"].encode()
    placed = {
        section["name"]: system[
            section["byte_offset"] : section["byte_offset"] + section["byte_length"]
        ]
        for section in sections
    }
    assert placed == {
        "soul": b"SOUL TEXT",
        "tool_guidance": b"GUIDANCE BEFORE\nGUIDANCE AFTER",
        "plugin_sections": b"PLUGIN TEXT",
    }


def test_turn_context_is_placed_in_the_next_request_only(capture, writer, stream_file):
    capture.turn_context("ses_1", "hello", [("session_coworld", "[session Coworld] none")])
    _attempt(capture)
    _attempt(capture)
    records = _flushed(writer, stream_file)
    contexts = [record for record in records if record["family"] == "context"]
    assert sorted(record["source_type"] for record in contexts) == ["session_coworld", "user_text"]
    placed = next(record for record in contexts if record["source_type"] == "session_coworld")
    text = REQUEST["messages"][1]["content"].encode()
    start = placed["byte_offset"]
    assert text[start : start + placed["byte_length"]] == b"[session Coworld] none"


def test_records_after_compaction_keep_the_first_session(capture, writer, stream_file):
    capture.post_compaction(
        session_id="ses_2",
        old_session_id="ses_1",
        kind="provider",
        reason="Context Pressure",
        before_tokens=100,
        after_tokens=10,
        messages_removed=None,
    )
    capture.post_compaction(session_id="ses_3", old_session_id="ses_2", kind="provider")
    _attempt(capture, session_id="ses_3")
    records = _flushed(writer, stream_file)
    request = _operations(records, "model_attempt", "request")[0]
    assert request["session_id"] == "ses_1" and request["hermes_session_id"] == "ses_3"
    first = _operations(records, "compaction", "committed")[0]
    assert first["parent_session_id"] == "ses_1"
    assert first["reason"] == "context_pressure"
    assert first["details"] == {"before_tokens": 100, "after_tokens": 10}


def test_prompt_sources_follow_a_session_to_its_new_id_after_compaction(
    capture, writer, stream_file
):
    prompt = "SOUL TEXT"
    capture.on_system_prompt_built(
        session_id="ses_1",
        system_prompt=prompt,
        sections=[{"source": "soul", "start": 0, "end": 9}],
    )
    capture.post_compaction(session_id="ses_2", old_session_id="ses_1", kind="provider")
    request = {"model": "m", "messages": [{"role": "system", "content": prompt}]}
    _attempt(capture, session_id="ses_2", request=request)
    records = _flushed(writer, stream_file)
    objects = objects_of(records)
    digest = _digest(_operations(records, "model_attempt", "request")[0], "request_manifest")
    manifest = json.loads(objects[digest])
    sections = json.loads(objects[manifest["prompt_sections_digest"]])["sections"]
    assert [section["name"] for section in sections] == ["soul"]


def test_auxiliary_calls_outside_a_turn_and_those_without_a_reply(capture, writer, stream_file):
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    capture.pre_auxiliary_call(aux_task="vision", request_body=body, started_at=STARTED)
    capture.post_auxiliary_call(
        aux_task="vision", started_at=STARTED, error="ValueError: bad", error_type="ValueError"
    )
    capture.post_auxiliary_call(aux_task="vision", started_at=STARTED + 1, streaming=True)
    records = _flushed(writer, stream_file)
    request = _operations(records, "model_attempt", "request")[0]
    assert request["session_id"] == NO_SESSION
    failed = _operations(records, "model_attempt", "failed")[0]
    assert objects_of(records)[_digest(failed, "error")] == b"ValueError: bad"
    assert _operations(records, "model_attempt", "unread")[0]["outcome"] == "streamed"


def test_approvals_are_recorded_with_their_text_redacted(capture, writer, stream_file):
    command = f"curl -H 'key: {HERMES_STYLE_SECRET}' example.com"
    capture.pre_approval_request(
        command=command, description="needs a look", surface="mcp-trust/Some Server"
    )
    capture.post_approval_response(
        command=command, choice="Deny", decided_by="user", surface="gateway"
    )
    records = _flushed(writer, stream_file)
    assert HERMES_STYLE_SECRET not in json.dumps(records)
    requested = _operations(records, "approval", "requested")[0]
    assert b"needs a look" in objects_of(records)[_digest(requested, "description")]
    decided = _operations(records, "approval", "decided")[0]
    assert decided["outcome"] == "deny" and decided["reason"] == "user"


def test_lifecycle_hooks_leave_records(capture, writer, stream_file):
    capture.on_session_start(session_id="ses_1", model="m")
    capture.on_session_end(
        session_id="ses_1", turn_id="t", turn_exit_reason="Stop", completed=True, run_id="r"
    )
    capture.subagent_start(parent_session_id="ses_1", child_session_id="ses_c")
    capture.subagent_stop(
        parent_session_id="ses_1", child_session_id="ses_c", child_status="done", duration_ms=5
    )
    capture.api_request_error(session_id="ses_1", reason="rate_limit", status_code=429)
    records = _flushed(writer, stream_file)
    assert _operations(records, "session", "started")
    ended = _operations(records, "turn", "ended")[0]
    assert ended["details"] == {"completed": True} and ended["reason"] == "stop"
    assert _operations(records, "subagent", "stopped")[0]["outcome"] == "done"
    assert _operations(records, "subagent", "started")[0]["parent_session_id"] == "ses_1"
    assert _operations(records, "model_request", "error")[0]["details"] == {"status_code": 429}


def _log_record(message: str, *args, name: str = "gateway.run", thread: str = "MainThread"):
    record = logging.LogRecord(name, logging.WARNING, __file__, 42, message, args, None)
    record.threadName = thread
    record.hermes_home = "/workspace/host/profiles/researcher-1"
    record.session_tag = " [ses_1]"
    return record


def test_diagnostics_go_through_the_writers_scanner(writer, stream_file):
    handler = DiagnosticsHandler(writer)
    handler.handle(_log_record("key is %s", HERMES_STYLE_SECRET))
    records = _flushed(writer, stream_file)
    assert HERMES_STYLE_SECRET not in json.dumps(records)
    line = _operations(records, "diagnostic", "warning")[0]
    assert line["profile"] == "researcher-1"
    assert line["session_id"] == "ses_1"
    assert line["native_event_type"] == "gateway.run"
    assert objects_of(records)[_digest(line, "message")].startswith(b"key is ")


def test_diagnostics_keep_a_traceback(writer, stream_file):
    handler = DiagnosticsHandler(writer)
    try:
        raise ValueError("the reason")
    except ValueError:
        record = _log_record("it failed")
        record.exc_info = sys.exc_info()
    handler.handle(record)
    records = _flushed(writer, stream_file)
    line = _operations(records, "diagnostic")[0]
    assert b"ValueError: the reason" in objects_of(records)[_digest(line, "message")]


POLL_AGENT = "Python-urllib/3.12"


def _access_line(call: str, status: int = 200, referer: str = "-", agent: str = POLL_AGENT) -> str:
    """aiohttp's default format, as a real gateway writes it."""
    time = "[05/Nov/2026:16:00:00 +0000]"
    return f'127.0.0.1 {time} "{call} HTTP/1.1" {status} 12 "{referer}" "{agent}"'


@pytest.mark.parametrize(
    "record",
    [
        _log_record("dropped", name="research_core.agent_telemetry.writer"),
        _log_record("from sqlite", thread="telemetry-writer"),
        _log_record(_access_line("GET /p/researcher/v1/runs/run_1"), name=ACCESS_LOGGER),
    ],
)
def test_the_writer_and_a_seam_poll_stay_out(writer, stream_file, record):
    DiagnosticsHandler(writer).handle(record)
    assert writer.flush(5.0)
    assert stored_records(stream_file) == []


POLL_TEXT = '"GET /p/researcher/v1/runs/run_1 HTTP/1.1" 200 12 "-" "Python-urllib/3.12"'


@pytest.mark.parametrize(
    "line",
    [
        _access_line("GET /p/researcher/api/sessions/s1/messages?order=latest"),
        _access_line("GET /p/researcher/v1/runs/run_1/events?last_seq=0"),
        _access_line("POST /p/researcher/v1/runs/run_1/approval"),
        _access_line("POST /p/researcher/v1/runs/run_1/stop"),
        _access_line("POST /p/researcher/v1/runs"),
        # The bridge's ``run.get``: the same call from another client.
        _access_line("GET /p/researcher/v1/runs/run_1", agent="python-httpx/0.28.1"),
        _access_line("GET /p/researcher/v1/runs/run_1", status=404),
        _access_line("GET /p/researcher/v1/runs/run_1", status=500),
        _access_line("GET /p/researcher/v1/runs/run_1?detail=1"),
        _access_line("GET /p/researcher/api/sessions/s1/messages?next=/v1/runs/run_1"),
        _access_line("GET /p/researcher/api/v1/runs/run_1"),
        _access_line("GET /v1/runs/run_1"),
        _access_line("POST /p/researcher/v1/runs/run_1"),
        _access_line("POST /p/researcher/v1/runs", status=500, agent=POLL_TEXT.replace('"', "'")),
        _access_line("POST /p/researcher/v1/runs", status=500, agent=POLL_TEXT),
        _access_line("POST /p/researcher/v1/runs", status=500, referer=POLL_TEXT),
    ],
)
def test_every_other_access_line_is_sent(writer, stream_file, line):
    DiagnosticsHandler(writer).handle(_log_record(line, name=ACCESS_LOGGER))
    records = _flushed(writer, stream_file)
    logged = _operations(records, "diagnostic")[0]
    assert line.encode() in objects_of(records)[_digest(logged, "message")]


def test_the_log_handler_stays_on_the_callers_thread_after_logging_moves_to_its_queue():
    """Hermes's root handlers move behind ``telemetry``'s queue at the first span. This one must not
    follow them: a stuck handler there holds its records, and an exit can lose them."""
    code = (
        "import logging\n"
        "from research_core import telemetry\n"
        "from research_core.plugin.capture import DiagnosticsHandler\n"
        "root = logging.getLogger()\n"
        "other = logging.NullHandler()\n"
        "ours = DiagnosticsHandler(None)\n"
        "root.handlers = [other, ours]\n"
        "telemetry.configure_logging()\n"
        "assert other not in root.handlers and ours in root.handlers, root.handlers\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
