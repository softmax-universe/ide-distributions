"""The plugin's callbacks, called as Hermes's native hooks call them, with the real writer and a
stand-in scanner.

``core/tests/hermes/test_native_capture.py`` runs the same code on a real, unpatched gateway. These
cover what a stub model there does not produce: failures, compaction, approvals and odd values.
"""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from research_core.agent_telemetry import TelemetryWriter
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
from research_core.plugin.tracing import make_instrumentation
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
    return GatewayCapture(writer, "orchestrator", lambda *_: {})


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


def test_a_request_is_what_the_middleware_saw_with_its_row_ids(capture, home, writer, stream_file):
    _request(capture)
    _reply(capture)
    records = _flushed(writer, stream_file)
    started = _operations(records, "model_request", "started")[0]
    request = _operations(records, "model_attempt", "request")[0]
    response = _operations(records, "model_attempt", "response")[0]
    assert "trace_id" not in started  # This stand-in names no span.
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


def test_a_call_id_a_later_turn_reuses_has_its_result_captured(capture, writer, stream_file):
    """Kimi reuses call IDs across turns, so a result is new once per turn, not once per Session."""
    for turn, text in (("turn-1", "first"), ("turn-2", "second")):
        wire = {
            **REQUEST,
            "messages": [
                *REQUEST["messages"],
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "tool_call_id": "c1", "content": text},
            ],
        }
        _request(capture, request=wire, turn_id=turn)
        capture.on_session_end(session_id="ses_1", turn_id=turn, completed=True)
    records = _flushed(writer, stream_file)
    results = _operations(records, "tool", "result")
    assert [objects_of(records)[_digest(r, "result")] for r in results] == [b"first", b"second"]
    assert capture._results_seen == {}


def test_a_reply_with_no_request_recorded_joins_no_earlier_attempt(capture, writer, stream_file):
    """The middleware kept no request for the second call, so its reply has no attempt to join."""
    _request(capture)
    _reply(capture)
    capture.pre_api_request(session_id="ses_1", api_request_id="turn-1:api:2")
    _reply(capture, api_request_id="turn-1:api:2")
    responses = _operations(_flushed(writer, stream_file), "model_attempt", "response")
    assert [r["model_attempt_id"] for r in responses] == [f"{CALL}#1"]


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


def test_a_turns_dropped_read_leaves_its_rows_out_of_the_next_turn(
    home, writer, stream_file, monkeypatch
):
    """A full queue can drop a turn's read of its rows. The next turn's read starts at that turn's
    end, so it never names the earlier rows with its own span."""
    spans_of = {"ses_1": {"trace_id": "a" * 32, "span_id": "1" * 16}}
    capture = GatewayCapture(writer, "orchestrator", lambda session, *_: spans_of[session])

    def commit(content: str) -> None:
        with sqlite3.connect(home / "state.db") as db:
            db.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                ("ses_1", "assistant", content, time.time()),
            )

    deferred = writer.defer
    monkeypatch.setattr(writer, "defer", lambda job: None)  # The queue is full.
    commit("turn A")
    capture.on_session_end(session_id="ses_1", turn_id="a", completed=True)
    monkeypatch.setattr(writer, "defer", deferred)
    spans_of["ses_1"] = {"trace_id": "b" * 32, "span_id": "2" * 16}
    commit("turn B")
    capture.on_session_end(session_id="ses_1", turn_id="b", completed=True)
    records = _flushed(writer, stream_file)
    (row,) = _operations(records, "message", "committed")
    assert json.loads(objects_of(records)[_digest(row, "message")])["content"] == "turn B"
    assert row["trace_id"] == "b" * 32


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
            [(*row, capture._started) for row in rows],
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


def _traced(writer) -> tuple[dict, GatewayCapture]:
    """Hermes's order: the tracing hooks are registered before capture's."""
    hooks, traces = make_instrumentation("orchestrator")
    return hooks, GatewayCapture(writer, "orchestrator", traces.correlation)


def test_each_record_names_the_span_it_belongs_to(spans, home, writer, stream_file):
    """``trace_id`` and ``span_id`` as Datadog joins a log to its trace: an attempt's records
    name its ``chat`` span, a tool's its ``execute_tool`` span, the rest the turn's."""
    hooks, capture = _traced(writer)
    hooks["pre_llm_call"](session_id="ses_1", task_id="task", turn_id="turn-1")
    hooks["pre_api_request"](turn_id="turn-1", api_request_id=CALL, retry_count=0)
    _request(capture)
    hooks["post_api_request"](turn_id="turn-1", api_request_id=CALL, model="m", provider="p")
    _reply(capture)
    hooks["pre_tool_call"](turn_id="turn-1", tool_call_id="call_1", tool_name="goal")
    hooks["post_tool_call"](turn_id="turn-1", tool_call_id="call_1", tool_name="goal", status="ok")
    capture.post_tool_call(tool_name="goal", session_id="ses_1", tool_call_id="call_1", status="ok")
    hooks["on_session_end"](turn_id="turn-1", completed=True, failed=False, interrupted=False)
    capture.on_session_end(session_id="ses_1", turn_id="turn-1", completed=True)
    by_name = {span.name: span.context for span in spans.get_finished_spans()}
    trace_id = f"{by_name['invoke_agent'].trace_id:032x}"
    records = [r for r in _flushed(writer, stream_file) if r["family"] == "operation"]
    assert {r["trace_id"] for r in records} == {trace_id}
    named = {(r["action"], r["stage"]): r["span_id"] for r in records}
    span_of = {name: f"{context.span_id:016x}" for name, context in by_name.items()}
    assert named[("model_attempt", "request")] == named[("model_attempt", "response")]
    assert named[("model_attempt", "response")] == span_of["chat"]
    assert named[("tool", "completed")] == span_of["execute_tool"]
    assert named[("turn", "ended")] == span_of["invoke_agent"]


def test_a_delegated_childs_records_are_in_its_parents_trace(spans, home, writer, stream_file):
    hooks, capture = _traced(writer)
    hooks["pre_llm_call"](session_id="ses_1", task_id="task", turn_id="turn-1")
    hooks["pre_api_request"](turn_id="turn-1", api_request_id=CALL, retry_count=0)
    hooks["pre_llm_call"](
        session_id="ses_child", task_id="child", turn_id="turn-c", parent_session_id="ses_1"
    )
    capture.subagent_start(parent_session_id="ses_1", child_session_id="ses_child")
    capture.post_tool_call(tool_name="goal", args={}, result="{}", session_id="ses_child")
    capture.post_tool_call(tool_name="goal", args={}, result="{}", session_id="ses_1")
    first, second = _operations(_flushed(writer, stream_file), "tool", "completed")
    assert first["trace_id"] == second["trace_id"]
    assert first["span_id"] != second["span_id"]


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


@pytest.fixture
def stalled_writer(tmp_path, producer, redactor):
    """A writer whose stream is a pipe that is open and never read, as a stalled container pipe."""
    fifo = tmp_path / "stalled"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    instance = TelemetryWriter(str(fifo), producer, redactor=redactor)
    yield instance
    os.close(reader)


def test_no_hook_waits_for_a_stalled_stream_or_reads_a_file(
    spans, stalled_writer, home, monkeypatch
):
    """Telemetry never delays the agent: with the pipe full and the writer's thread stuck on it,
    each hook only enqueues. No hook reads a file: ``state.db`` and ``SOUL.md`` are read on the
    writer's thread, and no record needs Hermes's database to name its turn."""
    hooks, capture = _traced(stalled_writer)
    read_on = []
    connect, read_text = sqlite3.connect, Path.read_text

    def connecting(*args, **kwargs):
        read_on.append(threading.current_thread().name)
        return connect(*args, **kwargs)

    def reading(path, *args, **kwargs):
        read_on.append(threading.current_thread().name)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connecting)
    monkeypatch.setattr(Path, "read_text", reading)
    big = {**REQUEST, "messages": [*REQUEST["messages"], {"role": "user", "content": "x" * 2**20}]}
    ended = {"completed": True, "failed": False, "interrupted": False}
    tool = {"tool_name": "goal", "tool_call_id": "c", "session_id": "ses_1", "status": "ok"}
    calls = [
        lambda: hooks["pre_llm_call"](session_id="ses_1", task_id="t", turn_id="turn-1"),
        lambda: hooks["pre_api_request"](turn_id="turn-1", api_request_id=CALL, retry_count=0),
        lambda: _request(capture, request=big),
        lambda: hooks["post_api_request"](
            turn_id="turn-1", api_request_id=CALL, model="m", provider="p"
        ),
        lambda: _reply(capture),
        lambda: hooks["pre_tool_call"](turn_id="turn-1", tool_call_id="c", tool_name="goal"),
        lambda: hooks["post_tool_call"](turn_id="turn-1", **tool),
        lambda: capture.post_tool_call(**tool),
        lambda: hooks["on_session_end"](turn_id="turn-1", **ended),
        lambda: capture.on_session_end(session_id="ses_1", turn_id="turn-1", completed=True),
    ]
    slowest = 0.0
    for _ in range(20):
        for call in calls:
            started = time.monotonic()
            call()
            slowest = max(slowest, time.monotonic() - started)
    # A few milliseconds alone, more on a loaded CI runner; a wait on the writer would never end.
    assert slowest < 1.0
    assert set(read_on) <= {"telemetry-writer"}


def test_a_turns_calls_are_forgotten_at_its_end_and_its_failures_stay_attributed(
    capture, writer, stream_file
):
    """A Session's calls must not accumulate state across turns, and a failure during the turn
    still names its attempt."""
    for number in range(1, 1001):
        call = f"turn-1:api:{number}"
        capture.keep_request(request=REQUEST, session_id="ses_1")
        capture.pre_api_request(
            session_id="ses_1", api_request_id=call, conversation_history=HISTORY
        )
        capture.post_api_request(
            session_id="ses_1",
            api_request_id=call,
            assistant_message=SimpleNamespace(content="ok", tool_calls=None),
        )
    capture.keep_request(request=REQUEST, session_id="ses_1")
    capture.pre_api_request(session_id="ses_1", api_request_id="turn-1:api:1001")
    capture.api_request_error(session_id="ses_1", api_request_id="turn-1:api:1001", reason="x")
    capture.on_session_end(session_id="ses_1", turn_id="turn-1", completed=True)
    assert (capture._attempts, capture._open_attempt, capture._turn_calls) == ({}, {}, {})
    records = _flushed(writer, stream_file)
    [failed] = _operations(records, "model_attempt", "failed")
    assert failed["model_attempt_id"] == "turn-1:api:1001#1"


def test_a_stalled_log_destination_never_holds_up_the_logging_thread():
    """Behind ``telemetry``'s queue a record that does not fit is dropped and counted, and the
    destination hears the count when it takes records again."""
    from research_core.telemetry import LOG_QUEUE_SIZE, DroppingQueueHandler, DropReportingListener

    released = threading.Event()
    heard: list[str] = []

    class Stalled(logging.Handler):
        def emit(self, record):
            released.wait()
            heard.append(record.getMessage())

    handler = DroppingQueueHandler()
    listener = DropReportingListener(handler, Stalled())
    listener.start()
    logger = logging.Logger("stalled")
    logger.addHandler(handler)
    started = time.monotonic()
    for number in range(LOG_QUEUE_SIZE + 100):
        logger.warning("line %d", number)
    assert time.monotonic() - started < 1.0
    assert handler.dropped >= 99
    released.set()
    deadline = time.monotonic() + 10
    while not handler.queue.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    logger.warning("after")
    while heard[-1:] != ["after"] and time.monotonic() < deadline:
        time.sleep(0.01)
    listener.stop()
    assert heard[-1] == "after"
    assert f"logging: {handler.dropped} records dropped so far" in heard
