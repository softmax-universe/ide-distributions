"""Guard distribution privacy and Hermes callback correlation owned by this adapter."""

import json
import logging

import pytest
from opentelemetry.trace import StatusCode

from research_core import telemetry
from research_core.plugin import tracing


def test_exception_status_never_records_sensitive_exception_text(spans):
    with pytest.raises(RuntimeError), telemetry.span("failure"):
        raise RuntimeError("secret-credential")
    (current,) = spans.get_finished_spans()
    assert current.status.status_code == StatusCode.ERROR
    assert "secret-credential" not in current.to_json()
    assert not current.events


def test_native_callbacks_keep_concurrent_turns_and_retry_attempts_separate(monkeypatch, spans):
    active = {"t1", "t2"}
    hooks, traces = tracing.make_instrumentation("researcher")
    for turn_id in active:
        hooks["pre_llm_call"](session_id=turn_id, task_id=turn_id, turn_id=turn_id)
    for turn_id in active:
        hooks["pre_api_request"](turn_id=turn_id, api_request_id="same", retry_count=0)
    bound = {
        turn_id: traces.session_turn(turn_id).span.get_span_context().span_id for turn_id in active
    }
    hooks["api_request_error"](
        turn_id="t1",
        api_request_id="same",
        model="m",
        provider="p",
        status_code=429,
        reason="rate_limit",
        error={"type": "RateLimitError", "message": "secret-error"},
    )
    hooks["pre_api_request"](turn_id="t1", api_request_id="same", retry_count=1)
    for turn_id in active:
        hooks["post_api_request"](turn_id=turn_id, api_request_id="same", model="m", provider="p")
        hooks["on_session_end"](turn_id=turn_id, completed=True, failed=False, interrupted=False)
    records = spans.get_finished_spans()
    roots = {s.context.span_id: s for s in records if s.name == "invoke_agent"}
    models = [s for s in records if s.name == "chat"]
    assert len(roots) == 2 and len(models) == 3
    assert all(s.parent.span_id in roots for s in models)
    assert {root.attributes["session.id"]: root.context.span_id for root in roots.values()} == bound
    retries = [s for s in models if roots[s.parent.span_id].attributes["turn.id"] == "t1"]
    assert [s.attributes["retry.count"] for s in retries] == [0, 1]
    assert retries[0].status.status_code == StatusCode.ERROR
    assert (retries[0].attributes["error.type"], retries[0].attributes["exception.type"]) == (
        "rate_limit",
        "RateLimitError",
    )
    assert all("secret-error" not in s.to_json() for s in records)


def test_orphan_observation_ends_incomplete_without_reclaiming_active_tool(monkeypatch, spans):
    active = {"failed", "long-tool"}
    hooks, _ = tracing.make_instrumentation("orchestrator")
    for turn_id in active:
        hooks["pre_llm_call"](session_id=turn_id, task_id=turn_id, turn_id=turn_id)
    hooks["pre_tool_call"](turn_id="long-tool", tool_call_id="tool")
    hooks["pre_llm_call"](session_id="failed", task_id="next", turn_id="next")
    (orphan,) = spans.get_finished_spans()
    assert orphan.attributes["observation.complete"] is False
    assert orphan.attributes["observation.reason"] == "next_turn_in_session"
    hooks["post_tool_call"](
        turn_id="long-tool", tool_call_id="tool", tool_name="terminal", status="ok"
    )
    hooks["on_session_end"](turn_id="long-tool", completed=True, failed=False, interrupted=False)
    assert spans.get_finished_spans()[-1].attributes["observation.complete"] is True


def test_both_tool_boundaries_name_the_same_call_and_only_a_short_error_class(
    spans, caplog, tmp_path
):
    """A native and a distribution span of one failed call carry the same identity, an
    ``error.type`` read from structured fields, and log one payload-free line each."""
    from research_core.tools.base import ToolContext, run_tool

    hooks, _ = tracing.make_instrumentation("researcher")
    hooks["pre_llm_call"](session_id="ses_1", task_id="task", turn_id="turn_1")
    hooks["pre_api_request"](turn_id="turn_1", api_request_id="a1", retry_count=0)
    hooks["post_api_request"](turn_id="turn_1", api_request_id="a1", model="m", provider="p")
    calls = {
        "c1": ("terminal", '{"output": "secret-output", "exit_code": 1, "error": null}'),
        "c2": ("read_file", '{"content": "", "error": "File not found: /secret/path"}'),
        "c3": ("read_file", '{"content": "fine"}'),
    }
    env = {"IDE_WORKSPACE": str(tmp_path), "IDE_SESSION_ID": "ses_1", "IDE_TURN_ID": "turn_1"}
    ctx = ToolContext.from_env("researcher", {**env, "IDE_TOOL_CALL_ID": "c4"})
    caplog.set_level("INFO", logger="research_core.tool_call")
    for call_id, (name, result) in calls.items():
        hooks["pre_tool_call"](turn_id="turn_1", tool_call_id=call_id)
        status = "ok" if call_id == "c3" else "error"
        hooks["post_tool_call"](
            turn_id="turn_1",
            tool_call_id=call_id,
            tool_name=name,
            session_id="ses_1",
            status=status,
            error_type=None if status == "ok" else "tool_error",
            result=result,
            duration_ms=250,
        )
    hooks["pre_tool_call"](turn_id="turn_1", tool_call_id="c4")
    refused = run_tool("claim_write", {"body": "x"}, ctx)
    hooks["post_tool_call"](
        turn_id="turn_1",
        tool_call_id="c4",
        tool_name="claim_write",
        session_id="ses_1",
        status="error",
        error_type="tool_error",
        result=refused,
        duration_ms=40,
    )
    hooks["on_session_end"](turn_id="turn_1", completed=True, failed=False, interrupted=False)

    tools = [s for s in spans.get_finished_spans() if s.name == "execute_tool"]
    by_call = {
        (s.attributes["tool.boundary"], s.attributes["gen_ai.tool.call.id"]): s for s in tools
    }
    assert {key: by_call[key].attributes.get("error.type") for key in by_call} == {
        ("native", "c1"): "exit_code_nonzero",
        ("native", "c2"): "tool_error",
        ("native", "c3"): None,
        ("distribution", "c4"): "validation_rejected",
        ("native", "c4"): "validation_rejected",
    }
    for current in tools:
        assert current.attributes["session.id"] == "ses_1"
        assert current.attributes["turn.id"] == "turn_1"
        assert current.attributes["gen_ai.agent.name"] == "researcher"
    (turn,) = [s for s in spans.get_finished_spans() if s.name == "invoke_agent"]
    assert (
        turn.attributes["gen_ai.turn.model_calls"],
        turn.attributes["gen_ai.turn.tool_calls"],
        turn.attributes["gen_ai.turn.tool_failures"],
    ) == (1, 4, 3)

    lines = [telemetry.JsonLines(str).format(r) for r in caplog.records]
    assert len(lines) == 5
    assert all("secret" not in line for line in lines)
    assert all("\n" not in line for line in lines)
    statuses = [json.loads(line)["status"] for line in lines]
    assert statuses == ["warning", "warning", "info", "warning", "warning"]


def test_a_callers_attributes_cannot_replace_the_redacted_message():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token secret", None, None)
    record.attributes = {"message": "secret", "status": "error", "duration_s": 1.5}
    formatter = telemetry.JsonLines(lambda text: text.replace("secret", "***"))
    line = json.loads(formatter.format(record))
    assert (line["message"], line["status"], line["duration_s"]) == ("token ***", "info", 1.5)


def test_a_failed_tools_text_that_only_looks_like_json_stays_a_tool_error():
    """Raising here would leave the tool's span open and its failure uncounted."""
    assert tracing.native_error_type("error", "tool_error", "{not valid json}") == "tool_error"
