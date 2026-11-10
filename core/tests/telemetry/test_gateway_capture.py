"""The plugin's callbacks, called as Hermes's native hooks call them, with the real writer and a
stand-in scanner.

``core/tests/hermes/test_native_capture.py`` runs the same code on a real, unpatched gateway. These
cover what a stub model there does not produce: failures, compaction, approvals and odd values.
"""

import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from research_core.plugin.capture import (
    ACCESS_LOGGER,
    NO_SESSION,
    DiagnosticsHandler,
    GatewayCapture,
    remember_section,
    row_keys,
    wire_request,
    with_row_ids,
)
from tests.telemetry.conftest import (
    HERMES_STYLE_SECRET,
    objects_of,
    rebuild_request,
    scrub,
    stored_records,
)

REQUEST = {
    "model": "moonshot/kimi-k3.1-pro",
    "messages": [
        {"role": "system", "content": "SOUL TEXT\n\nPLUGIN TEXT\n\nHermes guidance"},
        {"role": "user", "content": "hello\n\n[session Coworld] none"},
    ],
    "tools": [{"type": "function", "function": {"name": "goal", "parameters": {}}}],
    "extra_body": {"provider": {"only": ["moonshotai"]}, "reasoning": {"effort": "low"}},
    "extra_headers": {"X-OpenRouter-Cache": "false"},
}
HISTORY = [{"role": "user", "content": "hello", "_row_id": 11}]
STARTED = 1793817074.0
CALL = "turn-1:api:1"


@pytest.fixture
def capture(writer, home) -> GatewayCapture:
    return GatewayCapture(writer, "orchestrator")


def _start_run(home: Path, session_id: str, run_id: str) -> None:
    payload = {"api_turn_v1": {"run_owner_scope": "x"}}
    with sqlite3.connect(home / "state.db") as db:
        db.execute(
            "INSERT INTO session_admissions (request_id, target_session_id, payload_json, status) "
            "VALUES (?, ?, ?, 'started')",
            (run_id, session_id, json.dumps(payload)),
        )


def _request(capture: GatewayCapture, request=REQUEST, history=HISTORY, **overrides) -> None:
    """The middleware, then ``pre_api_request``, as Hermes calls them for one attempt."""
    assert capture.keep_request(request=request, session_id="ses_1", api_request_id=CALL) is None
    capture.pre_api_request(
        **{
            "session_id": "ses_1",
            "conversation_history": history,
            "api_request_id": CALL,
            "turn_id": "turn-1",
            "provider": "openrouter",
            "started_at": STARTED,
            **overrides,
        }
    )


def _reply(capture: GatewayCapture, **overrides) -> None:
    capture.post_api_request(
        **{
            "session_id": "ses_1",
            "api_request_id": CALL,
            "assistant_message": SimpleNamespace(
                role="assistant", content="hi", reasoning_content="think", tool_calls=None
            ),
            "finish_reason": "stop",
            "response_model": "moonshot/kimi-k3.1-pro",
            "usage": {"prompt_tokens": 5, "output_tokens": 2, "cache_read_tokens": 3},
            "started_at": STARTED,
            "ended_at": STARTED + 2,
            "first_chunk_at": STARTED + 0.5,
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


def test_a_request_is_what_the_middleware_saw_with_its_row_ids_and_run(
    capture, home, writer, stream_file
):
    _start_run(home, "ses_1", "run_1")
    _request(capture)
    _reply(capture)
    records = _flushed(writer, stream_file)
    started = _operations(records, "model_request", "started")[0]
    request = _operations(records, "model_attempt", "request")[0]
    response = _operations(records, "model_attempt", "response")[0]
    assert started["run_id"] == request["run_id"] == response["run_id"] == "run_1"
    assert request["model_attempt_id"] == response["model_attempt_id"] == f"{CALL}#1"
    rebuilt = rebuild_request(_digest(request, "request_manifest"), objects_of(records))
    assert rebuilt == wire_request(REQUEST)
    manifest = json.loads(objects_of(records)[_digest(request, "request_manifest")])
    assert manifest["hermes_message_ids"] == {"1": "11"}
    assert response["usage"] == {
        "input_tokens": 5,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
    }
    assert response["finish_reason"] == "stop" and response["duration_ms"] == 2000.0
    assert response["time_to_first_token_ms"] == 500.0
    assert json.loads(objects_of(records)[_digest(response, "message")]) == {
        "role": "assistant",
        "content": "hi",
        "reasoning_content": "think",
    }


def test_each_outer_attempt_of_a_call_is_its_own_record(capture, writer, stream_file):
    _request(capture)
    capture.api_request_error(
        session_id="ses_1",
        api_request_id=CALL,
        reason="rate_limit",
        status_code=429,
        error={"type": "RateLimitError", "message": "slow down"},
        started_at=STARTED,
        ended_at=STARTED + 1,
    )
    _request(capture, retry_count=1)
    _reply(capture)
    records = _flushed(writer, stream_file)
    attempts = [r["model_attempt_id"] for r in _operations(records, "model_attempt", "request")]
    assert attempts == [f"{CALL}#1", f"{CALL}#2"]
    failed = _operations(records, "model_attempt", "failed")[0]
    assert failed["model_attempt_id"] == f"{CALL}#1" and failed["reason"] == "rate_limit"
    assert failed["details"] == {"status_code": 429}
    assert objects_of(records)[_digest(failed, "error")] == b"slow down"


def test_a_broken_stream_keeps_its_text_and_streamed_reasoning(capture, writer, stream_file):
    _request(capture)
    capture.on_stream_start(session_id="ses_1")
    capture.on_stream_delta(session_id="ses_1", delta="thinking ", kind="reasoning")
    capture.on_stream_delta(session_id="ses_1", delta="hard", kind="reasoning")
    capture.on_stream_delta(session_id="ses_1", delta="ignored", kind="text")
    capture.on_stream_end(session_id="ses_1", final_text="partial", finished=False, error="reset")
    records = _flushed(writer, stream_file)
    response = _operations(records, "model_attempt", "response")[0]
    assert response["partial"] is True and response["outcome"] == "failed"
    assert json.loads(objects_of(records)[_digest(response, "message")]) == {
        "role": "assistant",
        "content": "partial",
        "reasoning_content": "thinking hard",
    }


def test_a_finished_stream_leaves_the_reply_to_post_api_request(capture, writer, stream_file):
    _request(capture)
    capture.on_stream_delta(session_id="ses_1", delta="r", kind="reasoning")
    capture.on_stream_end(session_id="ses_1", final_text="hi", finished=True, error=None)
    records = _flushed(writer, stream_file)
    assert _operations(records, "model_attempt", "response") == []


def test_proposals_from_the_reply(capture, writer, stream_file):
    # Hermes's own flat shape, as its assembled message holds it.
    call = SimpleNamespace(id="call_1", name="goal", arguments='{"a": 1}', provider_data=None)
    _request(capture)
    _reply(
        capture,
        assistant_message=SimpleNamespace(role="assistant", content=None, tool_calls=[call]),
        finish_reason="tool_calls",
    )
    records = _flushed(writer, stream_file)
    proposed = _operations(records, "tool", "proposed")[0]
    assert proposed["tool_call_id"] == "call_1" and proposed["tool_name"] == "goal"
    assert proposed["api_request_id"] == CALL
    assert objects_of(records)[_digest(proposed, "arguments")] == b'{"a": 1}'


def test_a_tool_result_is_what_the_next_request_sent_once(capture, writer, stream_file):
    """The form the model got, after spill and budget, and only the first time it is sent."""
    tool_row = {"role": "tool", "tool_call_id": "c1", "content": "spilled: see file", "_row_id": 13}
    history = [*HISTORY, {"role": "assistant", "_row_id": 12, "tool_calls": [{"id": "c1"}]}]
    history.append(tool_row)
    wire = {
        **REQUEST,
        "messages": [
            *REQUEST["messages"],
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [{"type": "text", "text": "spilled: see file"}],
            },
        ],
    }
    _request(capture, request=wire, history=history)
    _request(capture, request=wire, history=history)
    records = _flushed(writer, stream_file)
    (result,) = _operations(records, "tool", "result")
    assert result["tool_call_id"] == "c1" and result["hermes_message_id"] == "13"
    assert objects_of(records)[_digest(result, "result")] == b"spilled: see file"


@pytest.mark.parametrize(
    ("status", "result", "stage"),
    [
        ("error", '{"ok": false, "error": "no", "problems": ["no"]}', "refused"),
        ("blocked", "blocked by a hook", "refused"),
        ("error", '{"error": "the disk is full"}', "completed"),
        ("ok", '{"ok": false, "problems": ["not a failure Hermes saw"]}', "completed"),
    ],
)
def test_a_refusal_is_told_from_a_failure(capture, writer, stream_file, status, result, stage):
    capture.post_tool_call(tool_name="goal", args={}, result=result, status=status)
    assert _operations(_flushed(writer, stream_file), "tool")[0]["stage"] == stage


def test_commits_are_read_from_state_db_at_the_end_of_a_turn(capture, home, writer, stream_file):
    call = {"id": "c", "type": "function", "function": {"name": "goal", "arguments": "{}"}}
    with sqlite3.connect(home / "state.db") as db:
        db.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("ses_1", "user", "before this process", STARTED - 1e9),
        )
        rows = [
            (
                "ses_1",
                "assistant",
                f"text {HERMES_STYLE_SECRET}",
                None,
                json.dumps([call]),
                "stop",
                "because",
            ),
            ("ses_1", "tool", "result", "c", None, None, None),
            ("ses_other", "user", "another session", None, None, None, None),
        ]
        db.executemany(
            "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, "
            "finish_reason, reasoning_content, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(*row, capture._started + 1) for row in rows],
        )
    capture.on_session_end(session_id="ses_1", turn_id="t", completed=True)
    capture.on_session_end(session_id="ses_1", turn_id="t2", completed=True)
    records = _flushed(writer, stream_file)
    first, second = _operations(records, "message", "committed")
    assert first["hermes_message_id"] == "2" and first["kind"] == "assistant"
    assert second["tool_call_id"] == "c"
    assert json.loads(objects_of(records)[_digest(second, "message")])["content"] == "result"
    assert HERMES_STYLE_SECRET not in json.dumps(records)
    assert json.loads(objects_of(records)[_digest(first, "message")]) == {
        "role": "assistant",
        "content": scrub(f"text {HERMES_STYLE_SECRET}"),
        "reasoning_content": "because",
        "tool_calls": [call],
    }


def test_prompt_sections_name_soul_and_plugin_sections_as_sent(capture, writer, stream_file):
    rendered = remember_section("ide-briefing", lambda info, **_: "PLUGIN TEXT")
    assert rendered(MappingProxyType({"session_id": "ses_1"})) == "PLUGIN TEXT"
    _request(capture)
    records = _flushed(writer, stream_file)
    request = _operations(records, "model_attempt", "request")[0]
    manifest = json.loads(objects_of(records)[_digest(request, "request_manifest")])
    sections = json.loads(objects_of(records)[manifest["prompt_sections_digest"]])["sections"]
    assert sections == [
        {"name": "soul", "byte_offset": 0, "byte_length": 9},
        {"name": "plugin_sections", "byte_offset": 11, "byte_length": 11},
    ]


def test_turn_context_is_placed_in_the_next_request_only(capture, writer, stream_file):
    capture.turn_context("ses_1", "hello", [("coworld", "[session Coworld] none")])
    _request(capture)
    _request(capture)
    records = _flushed(writer, stream_file)
    contexts = [r for r in records if r["family"] == "context"]
    assert {r["source_type"] for r in contexts} == {"user_text", "coworld"}


def test_records_after_compaction_keep_the_first_session(capture, writer, stream_file):
    capture.post_compaction(session_id="ses_2", old_session_id="ses_1", kind="manual")
    capture.post_compaction(session_id="ses_3", old_session_id="ses_2", kind="auto")
    capture.post_tool_call(tool_name="goal", args={}, result="{}", session_id="ses_3")
    records = _flushed(writer, stream_file)
    tool = _operations(records, "tool", "completed")[0]
    assert tool["session_id"] == "ses_1" and tool["hermes_session_id"] == "ses_3"


def test_a_delegated_child_names_its_parents_run(capture, home, writer, stream_file):
    _start_run(home, "ses_1", "run_1")
    capture.subagent_start(parent_session_id="ses_1", child_session_id="ses_child")
    capture.post_tool_call(tool_name="goal", args={}, result="{}", session_id="ses_child")
    capture.subagent_stop(parent_session_id="ses_1", child_session_id="ses_child")
    capture.post_tool_call(tool_name="goal", args={}, result="{}", session_id="ses_child")
    records = _flushed(writer, stream_file)
    first, second = _operations(records, "tool", "completed")
    assert first["run_id"] == "run_1" and "run_id" not in second


def test_auxiliary_calls_outside_a_turn_and_those_without_a_reply(capture, writer, stream_file):
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    capture.pre_auxiliary_call(
        aux_task="title", request={"method": "POST", "body": body}, started_at=STARTED
    )
    capture.post_auxiliary_call(
        aux_task="title",
        started_at=STARTED,
        ended_at=STARTED + 1,
        response={"assistant_message": {"role": "assistant", "content": "A title"}},
        usage={"prompt_tokens": 4, "output_tokens": 2},
    )
    capture.post_auxiliary_call(
        aux_task="vision", started_at=STARTED, error="ValueError: bad", error_type="ValueError"
    )
    capture.post_auxiliary_call(aux_task="vision", started_at=STARTED + 1, streaming=True)
    records = _flushed(writer, stream_file)
    request = _operations(records, "model_attempt", "request")[0]
    assert request["session_id"] == NO_SESSION
    response = _operations(records, "model_attempt", "response")[0]
    assert response["model_attempt_id"] == request["model_attempt_id"]
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
    capture.on_session_end(session_id="ses_1", turn_id="t", turn_exit_reason="Stop", completed=True)
    capture.subagent_start(parent_session_id="ses_1", child_session_id="ses_c")
    capture.subagent_stop(
        parent_session_id="ses_1", child_session_id="ses_c", child_status="done", duration_ms=5
    )
    records = _flushed(writer, stream_file)
    assert _operations(records, "session", "started")
    ended = _operations(records, "turn", "ended")[0]
    assert ended["details"] == {"completed": True} and ended["reason"] == "stop"
    assert _operations(records, "subagent", "stopped")[0]["outcome"] == "done"
    assert _operations(records, "subagent", "started")[0]["parent_session_id"] == "ses_1"


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
        # The AIDE backend's run read: the same call from another client.
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
