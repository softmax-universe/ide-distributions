"""``research telemetry rebuild`` against the fake Logs search, from records the real writer made.

``core/tests/hermes/test_rebuild_real.py`` compares it with a real gateway. This covers what a real
Session does not do on demand: lost and damaged objects, paging, rate limits, compaction.
"""

import dataclasses
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from research_core.agent_telemetry import ContextPart, PromptSection, TelemetryWriter
from research_core.agent_telemetry import records as rec
from research_core.agent_telemetry.rebuild import AmbiguousSession, LogsSearch, rebuild_session
from research_core.agent_telemetry.records import canonical_bytes
from research_core.plugin.capture import GatewayCapture

from .conftest import HERMES_STYLE_SECRET, insert_rows, raw_lines, scrub, stored_records
from .fake_datadog import FakeDatadog

SYSTEM = "You are a researcher. Be careful."
TOOL = {"type": "function", "function": {"name": "read_file", "parameters": {}}}
LONG_RESULT = 'café 日本語 😀 "quoted"\n' * 2000
USAGE = {"input_tokens": 1000, "output_tokens": 7}


def request_body(history: list[dict]) -> dict:
    return {
        "model": "moonshot/kimi",
        "messages": [{"role": "system", "content": SYSTEM}, *history],
        "tools": [TOOL],
        "stream": True,
    }


def two_requests(writer, session: str = "ses_1", prefix: str = "") -> list[dict]:
    """A model call asking for a tool, the tool, and the call after it."""
    user = {"role": "user", "content": "Read the notes"}
    call = {
        "id": f"{prefix}call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }
    assistant = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "thinking",
        "tool_calls": [call],
    }
    result = {"role": "tool", "tool_call_id": call["id"], "content": LONG_RESULT}
    ids = {"session_id": session, "provider": "moonshot", "run_id": f"{prefix}run_1"}
    bodies = [request_body([user]), request_body([user, assistant, result])]
    for number, body in enumerate(bodies, start=1):
        attempt = f"{prefix}att_{number}"
        fields = {
            **ids,
            "model_attempt_id": attempt,
            "api_request_id": f"{prefix}call:api:{number}",
        }
        if number == 1:
            writer.capture_context_parts(
                "Read the notes",
                [ContextPart("user_text", "Read the notes")],
                **{k: v for k, v in fields.items() if k != "provider"},
            )
        writer.capture_request(
            body,
            prompt_sections=[PromptSection("soul", "You are a researcher.")],
            native_event_type="stream",
            **fields,
        )
        reply = assistant if number == 1 else {"role": "assistant", "content": "done"}
        writer.capture_response(
            reply,
            session_id=session,
            model_attempt_id=attempt,
            run_id=ids["run_id"],
            native_event_type="stream",
            outcome="completed",
            usage=USAGE if number == 1 else None,
        )
    writer.capture_operation(
        "tool",
        "completed",
        session_id=session,
        run_id=ids["run_id"],
        tool_name="read_file",
        tool_call_id=call["id"],
        api_request_id=f"{prefix}call:api:1",
        outcome="ok",
        content={"arguments": "{}"},
    )
    writer.capture_operation(
        "tool",
        "result",
        session_id=session,
        run_id=ids["run_id"],
        tool_call_id=call["id"],
        hermes_message_id="3",
        content={"result": LONG_RESULT},
    )
    return bodies


@pytest.fixture
def datadog():
    server = FakeDatadog()
    yield server
    server.close()


def search_of(datadog: FakeDatadog, sleep=None) -> LogsSearch:
    return LogsSearch(datadog.url, "a", "b", **({"sleep": sleep} if sleep else {}))


def sent(writer, stream_file, datadog) -> list[dict]:
    writer.flush(5.0)
    datadog.take_lines(raw_lines(stream_file))
    return stored_records(stream_file)


def system_digest() -> str:
    return rec.json_object({"role": "system", "content": SYSTEM}).digest


def result_message_digest() -> str:
    message = {"role": "tool", "tool_call_id": "call_1", "content": LONG_RESULT}
    return rec.json_object(message).digest


def test_requests_responses_and_tools_come_back_exactly(writer, stream_file, datadog):
    bodies = two_requests(writer)
    sent(writer, stream_file, datadog)
    report = rebuild_session(search_of(datadog), "ses_1")
    assert report["missing"] == []
    assert report["workspace_id"] == "wrk-proof"
    assert [canonical_bytes(r["request"]) for r in report["requests"]] == [
        canonical_bytes(body) for body in bodies
    ]
    first, second = report["requests"]
    assert first["response"]["message"]["reasoning_content"] == "thinking"
    assert second["response"]["message"]["content"] == "done"
    assert [(t["stage"], t.get("result")) for t in first["tools"]] == [
        ("completed", None),
        ("result", LONG_RESULT),
    ]


def test_content_first_stored_under_an_earlier_session_is_followed(writer, stream_file, datadog):
    """The second Session's parts were stored once, under the first, and name no Session."""
    two_requests(writer, "ses_1")
    bodies = two_requests(writer, "ses_2", prefix="")
    records = sent(writer, stream_file, datadog)
    parts = [r for r in records if r.get("content_digest") == system_digest()]
    assert len(parts) == 1
    report = rebuild_session(search_of(datadog), "ses_2")
    assert report["missing"] == []
    assert [canonical_bytes(r["request"]) for r in report["requests"]] == [
        canonical_bytes(body) for body in bodies
    ]


def test_duplicates_are_dropped(writer, stream_file, datadog):
    two_requests(writer)
    sent(writer, stream_file, datadog)
    clean = rebuild_session(search_of(datadog), "ses_1")
    datadog.take(list(datadog.entries))
    twice = rebuild_session(search_of(datadog), "ses_1")
    assert twice["missing"] == []
    assert canonical_bytes(twice["requests"]) == canonical_bytes(clean["requests"])


def test_a_chunk_gap_is_named_and_blocks_only_the_requests_needing_it(writer, stream_file, datadog):
    bodies = two_requests(writer)
    sent(writer, stream_file, datadog)
    digest = result_message_digest()
    datadog.remove_chunk(digest, 1)
    report = rebuild_session(search_of(datadog), "ses_1")
    first, second = report["requests"]
    assert canonical_bytes(first["request"]) == canonical_bytes(bodies[0])
    assert second["request"] is None and second["blocked_by"] == [digest]
    gap = next(problem for problem in report["missing"] if problem["digest"] == digest)
    assert gap["kind"] == "chunk_gap" and gap["missing_chunks"] == [1]


def test_an_object_with_no_entries_is_named(writer, stream_file, datadog):
    two_requests(writer)
    sent(writer, stream_file, datadog)
    digest = system_digest()
    datadog.entries = [e for e in datadog.entries if e.get("content_digest") != digest]
    report = rebuild_session(search_of(datadog), "ses_1")
    assert {"kind": "no_content", "digest": digest} in report["missing"]
    assert all(request["request"] is None for request in report["requests"])


def test_a_failed_digest_is_named(writer, stream_file, datadog):
    two_requests(writer)
    sent(writer, stream_file, datadog)
    digest = system_digest()
    entry = next(e for e in datadog.entries if e.get("content_digest") == digest)
    entry["body"] = "x" + entry["body"]
    report = rebuild_session(search_of(datadog), "ses_1")
    assert [p["kind"] for p in report["missing"] if p["digest"] == digest] == ["digest_failure"]


def test_a_chunk_resent_later_fills_a_gap_in_the_first_sending(writer, stream_file, datadog):
    """Chunks of a body are the same whoever sends them, and however long after."""
    bodies = two_requests(writer)
    sent(writer, stream_file, datadog)
    digest = result_message_digest()
    second_sending = [dict(e) for e in datadog.entries if e.get("content_digest") == digest]
    datadog.remove_chunk(digest, 1)
    datadog.take([e for e in second_sending if e["chunk_index"] == 1])
    report = rebuild_session(search_of(datadog), "ses_1")
    assert report["missing"] == []
    assert canonical_bytes(report["requests"][1]["request"]) == canonical_bytes(bodies[1])


def test_tokens_are_shared_by_characters(writer, stream_file, datadog):
    two_requests(writer)
    sent(writer, stream_file, datadog)
    report = rebuild_session(search_of(datadog), "ses_1")
    first, second = report["requests"]
    by_source = {row["source"]: row for row in first["tokens_by_source"]}
    assert by_source["soul"]["chars"] == len("You are a researcher.")
    assert by_source["user_text"]["chars"] == len("Read the notes")
    assert by_source["system_unattributed"]["chars"] == len(SYSTEM) - len("You are a researcher.")
    total = sum(row["chars"] for row in by_source.values())
    expected = round(1000 * by_source["soul"]["chars"] / total)
    assert by_source["soul"]["estimated_tokens"] == expected
    assert abs(sum(row["estimated_tokens"] for row in by_source.values()) - 1000) <= len(by_source)
    # The second request's reply had no provider usage: characters only.
    assert all("estimated_tokens" not in row for row in second["tokens_by_source"])


def test_paging_and_a_rate_limit(writer, stream_file, datadog):
    bodies = two_requests(writer)
    for number in range(1200):
        writer.capture_operation("turn", "ended", session_id="ses_1", details={"number": number})
    sent(writer, stream_file, datadog)
    datadog.rate_limit_next = 2
    waits = []
    search = search_of(datadog, waits.append)
    report = rebuild_session(search, "ses_1")
    assert waits == [1.0, 1.0]
    assert datadog.rate_limited == 2
    assert len(datadog.searches) > 3
    # Whichever index holds the lines: no search names one, and none reaches past the retention.
    assert all("indexes" not in search["filter"] for search in datadog.searches)
    assert {search["filter"]["from"] for search in datadog.searches} == {"now-15d"}
    assert len(report["events"]) == 1200
    assert canonical_bytes(report["requests"][1]["request"]) == canonical_bytes(bodies[1])


def test_compaction_is_followed_in_both_directions_after_a_restart(writer, stream_file, datadog):
    """After a restart a process no longer knows the first ID, so records name only the new one."""
    two_requests(writer, "ses_1")
    writer.capture_operation(
        "compaction",
        "committed",
        session_id="ses_2",
        hermes_session_id="ses_2",
        parent_session_id="ses_1",
    )
    two_requests(writer, "ses_2", prefix="later-")
    sent(writer, stream_file, datadog)
    from_first = rebuild_session(search_of(datadog), "ses_1")
    from_last = rebuild_session(search_of(datadog), "ses_2")
    assert from_first["session_ids"] == from_last["session_ids"] == ["ses_1", "ses_2"]
    assert len(from_first["requests"]) == len(from_last["requests"]) == 4


def test_a_restart_sends_a_full_manifest_and_everything_it_lists(
    stream_file, producer, redactor, datadog
):
    """A new process knows nothing of what the last one sent. Its full manifest and everything it
    lists go again, chunk for chunk, and a reader drops them as duplicates."""
    body = request_body(
        [
            {"role": "user", "content": "Go"},
            {"role": "tool", "tool_call_id": "call_1", "content": LONG_RESULT},
        ]
    )
    for number in (1, 2):
        new_process = dataclasses.replace(producer, source_instance=f"gateway-{number}")
        writer = TelemetryWriter(stream_file, new_process, redactor=redactor)
        writer.capture_request(
            body,
            session_id="ses_1",
            provider="moonshot",
            model_attempt_id=f"att_{number}",
            native_event_type="stream",
        )
        assert writer.close(5.0)
    records = stored_records(stream_file)
    chunks: dict[str, set] = {"gateway-1": set(), "gateway-2": set()}
    for record in records:
        if record["family"] == "content":
            sent_chunk = (record["content_digest"], record["chunk_index"], record["body"])
            chunks[record["source_instance"]].add(sent_chunk)
    assert chunks["gateway-1"] == chunks["gateway-2"]
    datadog.take_lines(raw_lines(stream_file))

    report = rebuild_session(search_of(datadog), "ses_1")
    assert report["missing"] == []
    assert [r["operation"] for r in report["requests"]] == ["full", "full"]
    assert [r["source_instance"] for r in report["requests"]] == ["gateway-1", "gateway-2"]
    rebuilt = [canonical_bytes(r["request"]) for r in report["requests"]]
    assert rebuilt == [canonical_bytes(body)] * 2


def test_events_hold_what_joins_a_retry_and_a_held_chat_through_the_admission(
    writer, stream_file, datadog
):
    def event(kind: str, **fields) -> None:
        writer.capture_operation(
            "store_event",
            "committed",
            native_event_type=kind,
            session_id="ses_1",
            admission_id="adm_1",
            **fields,
        )

    for request_id in ("req_1", "req_2"):
        writer.capture_operation(
            "admission", "accepted", request_id=request_id, admission_id="adm_1"
        )
    event("client.accepted")
    event("client.started", run_id="run_1")
    event("client.waiting", run_id="run_1", reason="provider_unavailable")
    event("client.resumed")
    event("client.started", run_id="run_2")
    writer.capture_operation(
        "store_event",
        "committed",
        native_event_type="task.assigned",
        session_id="ses_1",
        assignment_id="adm_1",
        admission_id="adm_1",
        task_id="T-3",
    )
    # A record with only the run, as an approval has.
    writer.capture_operation("approval", "requested", run_id="run_2", content={"command": "ls"})
    sent(writer, stream_file, datadog)
    report = rebuild_session(search_of(datadog), "ses_1")
    admitted = [e for e in report["events"] if e.get("admission_id") == "adm_1"]
    assert [e["request_id"] for e in admitted if "request_id" in e] == ["req_1", "req_2"]
    kinds = [event.get("native_event_type") for event in admitted]
    assert kinds.count("client.accepted") == 1
    runs = [e["run_id"] for e in admitted if e.get("native_event_type") == "client.started"]
    assert runs == ["run_1", "run_2"]
    assert next(e for e in admitted if "assignment_id" in e)["task_id"] == "T-3"
    assert any(e.get("command") == "ls" for e in report["events"])


def test_a_call_with_no_attempt_is_named(writer, stream_file, datadog):
    """What a process killed during a call leaves."""
    writer.capture_operation(
        "model_request", "started", session_id="ses_1", api_request_id="t:api:1"
    )
    sent(writer, stream_file, datadog)
    report = rebuild_session(search_of(datadog), "ses_1")
    assert report["missing"] == [{"kind": "call_with_no_attempt", "api_request_id": "t:api:1"}]


def test_a_session_datadog_has_never_seen_says_so(datadog):
    report = rebuild_session(search_of(datadog), "ses_unknown")
    assert report["missing"] == [{"kind": "no_records", "session_id": "ses_unknown"}]


def test_workspaces_are_kept_apart(writer, stream_file, datadog):
    two_requests(writer)
    sent(writer, stream_file, datadog)
    datadog.take([{**entry, "workspace_id": "wrk-other"} for entry in list(datadog.entries)])
    with pytest.raises(AmbiguousSession, match="wrk-other, wrk-proof"):
        rebuild_session(search_of(datadog), "ses_1")
    report = rebuild_session(search_of(datadog), "ses_1", "wrk-other")
    assert report["workspace_id"] == "wrk-other" and report["missing"] == []


def _command(datadog: FakeDatadog, *arguments: str, **env: str) -> subprocess.CompletedProcess:
    allowed = {k: v for k, v in os.environ.items() if not k.startswith(("IDE_", "HERMES_", "DD_"))}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "research_core.cli",
            "telemetry",
            "rebuild",
            "--datadog-url",
            datadog.url,
            *arguments,
        ],
        env={**allowed, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_command_runs_with_only_the_two_keys(writer, stream_file, datadog, tmp_path):
    bodies = two_requests(writer)
    sent(writer, stream_file, datadog)
    keys = {"DD_API_KEY": "a", "DD_APP_KEY": "b", "HOME": str(tmp_path)}
    done = _command(datadog, "--session", "ses_1", **keys)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert canonical_bytes(report["requests"][1]["request"]) == canonical_bytes(bodies[1])

    datadog.remove_chunk(result_message_digest(), 0)
    assert _command(datadog, "--session", "ses_1", **keys).returncode == 1
    assert _command(datadog, "--session", "ses_1").returncode == 2


def test_a_datadog_failure_is_could_not_start_and_says_so_in_one_line(datadog, tmp_path):
    keys = {"DD_API_KEY": "a", "DD_APP_KEY": "b", "HOME": str(tmp_path)}
    for url in (f"{datadog.url}/no-such-route", "http://127.0.0.1:1"):
        done = _command(datadog, "--session", "ses_1", "--datadog-url", url, **keys)
        assert done.returncode == 2, done.stderr
        assert "Traceback" not in done.stderr
        assert len(done.stderr.strip().splitlines()) == 1, done.stderr


def committed(writer, row, kind="tool", run="run_1", **fields):
    writer.capture_operation(
        "message",
        "committed",
        session_id="ses_1",
        run_id=run,
        kind=kind,
        hermes_message_id=row,
        **fields,
    )


def asked(writer, attempt, reply):
    """One model call of ``run_1``, and the reply it got."""
    writer.capture_request(
        request_body([{"role": "user", "content": "Go"}]),
        model_attempt_id=attempt,
        native_event_type="stream",
        session_id="ses_1",
        provider="moonshot",
        run_id="run_1",
    )
    writer.capture_response(
        reply,
        session_id="ses_1",
        model_attempt_id=attempt,
        run_id="run_1",
        native_event_type="stream",
    )


TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "read", "arguments": "{}"},
}


def test_a_guard_halt_rebuilds_the_text_hermes_committed_and_not_the_last_reply(
    writer, stream_file, datadog
):
    """Hermes ends the turn with its own text, and makes no further request, so the last reply is
    the tool call before it and no request lists these rows."""
    halt = {"role": "assistant", "content": "I stopped retrying."}
    calling = {"role": "assistant", "tool_calls": [TOOL_CALL]}
    asked(writer, "att_1", calling)
    committed(writer, "10", kind="assistant", content={"message": calling})
    committed(writer, "11", kind="tool", tool_call_id="call_1")
    writer.capture_operation(
        "tool",
        "result",
        session_id="ses_1",
        run_id="run_1",
        tool_call_id="call_1",
        content={"result": "it failed"},
    )
    committed(writer, "12", kind="assistant", content={"message": halt})
    sent(writer, stream_file, datadog)

    report = rebuild_session(search_of(datadog), "ses_1")
    rows = {row["hermes_message_id"]: row["message"] for row in report["rows"]}
    assert rows["12"] == halt
    assert rows["10"] == calling
    assert rows["11"] == {"role": "tool", "tool_call_id": "call_1", "content": "it failed"}
    assert report["missing"] == []


def test_a_normal_final_reply_rebuilds_from_its_commit(writer, stream_file, datadog):
    final = {"role": "assistant", "content": "All done"}
    asked(writer, "att_1", {"role": "assistant", "content": "All done, as the model wrote it"})
    committed(writer, "10", kind="assistant", content={"message": final})
    sent(writer, stream_file, datadog)

    report = rebuild_session(search_of(datadog), "ses_1")
    assert report["rows"][0]["message"] == final
    assert report["missing"] == []


def test_a_row_takes_its_own_text_and_never_the_last_reply(writer, stream_file, datadog):
    """A crash between a commit and the next call leaves a user row no request lists, with its text
    on its commit, and a tool row whose result was never written. The last reply is neither's."""
    asked(writer, "att_1", {"role": "assistant", "content": "A reply"})
    user = {"role": "user", "content": "Next question"}
    committed(writer, "10", kind="user", content={"message": user})
    committed(writer, "11", kind="tool", tool_call_id="call_gone")
    sent(writer, stream_file, datadog)

    report = rebuild_session(search_of(datadog), "ses_1")
    assert [row["message"] for row in report["rows"]] == [user, None]
    assert report["missing"] == [{"kind": "row_without_text", "row": "11"}]


@pytest.mark.parametrize("kind", ["assistant", "tool"])
def test_a_lost_final_text_is_reported_and_no_earlier_reply_takes_its_place(
    writer, stream_file, datadog, kind
):
    lost = "the text that is lost"
    asked(writer, "att_1", {"role": "assistant", "content": "FIRST reply"})
    if kind == "assistant":
        message = {"role": "assistant", "content": lost}
        committed(writer, "11", kind="assistant", content={"message": message})
    else:
        committed(writer, "11", kind="tool", tool_call_id="call_1")
        writer.capture_operation(
            "tool",
            "result",
            session_id="ses_1",
            run_id="run_1",
            tool_call_id="call_1",
            content={"result": lost},
        )
    writer.flush(5.0)
    lines = raw_lines(stream_file)
    datadog.take_lines(line for line in lines if lost.encode() not in line)

    report = rebuild_session(search_of(datadog), "ses_1")
    assert report["rows"][0]["message"] is None
    assert {"kind": "row_without_text", "row": "11"} in report["missing"]


def test_a_failed_and_an_unread_attempt_keep_their_records(writer, stream_file, datadog):
    for number in (1, 2, 3):
        writer.capture_request(
            request_body([{"role": "user", "content": "Go"}]),
            session_id="ses_1",
            provider="moonshot",
            model_attempt_id=f"att_{number}",
        )
    ids = {"session_id": "ses_1"}
    writer.capture_operation(
        "model_attempt", "failed", model_attempt_id="att_1", content={"error": "boom"}, **ids
    )
    writer.capture_operation(
        "model_attempt", "failed", model_attempt_id="att_2", reason="timeouterror", **ids
    )
    writer.capture_operation(
        "model_attempt", "unread", model_attempt_id="att_3", outcome="streamed", **ids
    )
    sent(writer, stream_file, datadog)
    first, second, third = rebuild_session(search_of(datadog), "ses_1")["requests"]
    assert first["failed"]["error"] == "boom"
    assert second["failed"]["reason"] == "timeouterror" and "error" not in second["failed"]
    assert third["unread"]["outcome"] == "streamed" and "failed" not in third


def test_a_tool_call_of_any_shape_comes_back(writer, stream_file, datadog):
    call = {"id": "call_1", "type": "custom", "custom": {"name": "grep", "input": "x"}}
    body = request_body([{"role": "assistant", "content": "", "tool_calls": [call]}])
    writer.capture_request(body, session_id="ses_1", provider="moonshot", model_attempt_id="att_1")
    sent(writer, stream_file, datadog)
    [request] = rebuild_session(search_of(datadog), "ses_1")["requests"]
    assert canonical_bytes(request["request"]) == canonical_bytes(body)
    assert "tool_call_arguments" not in {row["source"] for row in request["tokens_by_source"]}


def test_a_reused_tool_call_id_does_not_join_two_rows(writer, stream_file, datadog):
    """Row IDs tell the two results apart when a call ID comes back after its history is gone."""
    old = {"role": "tool", "tool_call_id": "call_1", "content": "OLD result"}
    new = {"role": "tool", "tool_call_id": "call_1", "content": "NEW result"}
    user = {"role": "user", "content": "Go"}
    for number, row in ((1, {**old, "_row_id": 10}), (2, {**new, "_row_id": 77})):
        writer.capture_request(
            request_body([user, row]),
            session_id="ses_1",
            provider="moonshot",
            model_attempt_id=f"att_{number}",
            run_id="run_1",
            native_event_type="stream",
        )
    committed(writer, "10", tool_call_id="call_1")
    committed(writer, "77", tool_call_id="call_1")
    sent(writer, stream_file, datadog)

    rows = rebuild_session(search_of(datadog), "ses_1")["rows"]
    by_row = {row["hermes_message_id"]: row for row in rows}
    assert by_row["10"]["message"]["content"] == "OLD result"
    assert by_row["77"]["message"]["content"] == "NEW result"
    assert "rewritten_to" not in by_row["10"] and "rewritten_to" not in by_row["77"]


def _result(writer, row, call, text):
    writer.capture_operation(
        "tool",
        "result",
        session_id="ses_1",
        run_id="run_1",
        tool_call_id=call,
        **({"hermes_message_id": row} if row else {}),
        content={"result": text},
    )


def test_a_new_row_with_a_reused_call_id_keeps_its_own_result(writer, stream_file, datadog):
    """Only an older row's listing has this call ID, with no row ID, and no request lists the new
    row. Its own result tells it from the older row."""
    old = {"role": "tool", "tool_call_id": "call_1", "content": "OLD result"}
    writer.capture_request(
        request_body([{"role": "user", "content": "Go"}, old]),
        session_id="ses_1",
        provider="moonshot",
        model_attempt_id="att_1",
        run_id="run_1",
    )
    _result(writer, "10", "call_1", "OLD result")
    committed(writer, "10", tool_call_id="call_1")
    _result(writer, "77", "call_1", "NEW result")
    committed(writer, "77", tool_call_id="call_1")
    sent(writer, stream_file, datadog)

    report = rebuild_session(search_of(datadog), "ses_1")
    by_row = {row["hermes_message_id"]: row["message"] for row in report["rows"]}
    assert by_row["10"] == old
    assert by_row["77"] == {**old, "content": "NEW result"}
    assert report["missing"] == []


def test_results_sharing_a_call_id_stay_apart_by_row(writer, stream_file, datadog):
    """No request lists either row, as when the process dies before the next call."""
    for row, text in (("10", "first"), ("11", "second")):
        _result(writer, row, "call_1", text)
        committed(writer, row, tool_call_id="call_1")
    sent(writer, stream_file, datadog)

    report = rebuild_session(search_of(datadog), "ses_1")
    assert [row["message"]["content"] for row in report["rows"]] == ["first", "second"]
    assert report["missing"] == []


def test_results_with_no_row_id_sharing_a_call_id_stand_in_for_no_row(writer, stream_file, datadog):
    """Nothing says which is whose, so the rebuild says so rather than choosing."""
    for row, text in (("10", "first"), ("11", "second")):
        _result(writer, None, "call_1", text)
        committed(writer, row, tool_call_id="call_1")
    sent(writer, stream_file, datadog)

    report = rebuild_session(search_of(datadog), "ses_1")
    assert [row["message"] for row in report["rows"]] == [None, None]
    assert {"kind": "row_without_text", "row": "10"} in report["missing"]


@pytest.mark.parametrize(
    "second_parts",
    [
        [ContextPart("user_text", "hello")],
        [ContextPart("notice", "hello")],
    ],
    ids=["same source", "another source"],
)
def test_repeated_text_counts_each_character_once(writer, stream_file, datadog, second_parts):
    """The same text twice in a Session is one digest with two observations of its parts."""
    fields = {"session_id": "ses_1", "provider": "moonshot", "run_id": "run_1"}
    writer.capture_context_parts(
        "hello", [ContextPart("user_text", "hello")], session_id="ses_1", run_id="run_1"
    )
    writer.capture_context_parts("hello", second_parts, session_id="ses_1", run_id="run_1")
    writer.capture_request(
        {"model": "m", "messages": [{"role": "user", "content": "hello"}]},
        model_attempt_id="att_1",
        native_event_type="stream",
        **fields,
    )
    sent(writer, stream_file, datadog)

    [request] = rebuild_session(search_of(datadog), "ses_1")["requests"]
    chars = {row["source"]: row["chars"] for row in request["tokens_by_source"]}
    assert chars == {"user_text": 5}


def test_a_lost_reply_is_named_and_its_record_stays(writer, stream_file, datadog):
    two_requests(writer)
    records = sent(writer, stream_file, datadog)
    reply = next(r for r in records if r.get("stage") == "response")
    lost = reply["content_refs"][0]["digest"]
    datadog.remove_chunk(lost, 0)

    report = rebuild_session(search_of(datadog), "ses_1")
    assert {"kind": "no_content", "digest": lost} in report["missing"]
    response = report["requests"][0]["response"]
    assert response["message"] is None and response["partial"] is False


def test_what_was_recorded_stays_in_the_report(writer, stream_file, datadog):
    two_requests(writer)
    writer.capture_operation(
        "workspace_tool",
        "completed",
        session_id="ses_1",
        run_id="run_1",
        tool_name="goal",
        tool_call_id="call_b",
        duration_ms=12.5,
    )
    writer.capture_operation(
        "store_event",
        "committed",
        session_id="ses_1",
        admission_id="adm_1",
        native_event_type="transition.requested",
        command_id="cmd_1",
        reported_actor="session:ses_1",
    )
    committed(writer, "3", tool_call_id="call_1", hermes_task_id="task_1")
    sent(writer, stream_file, datadog)

    report = rebuild_session(search_of(datadog), "ses_1")
    tool = next(e for e in report["events"] if e["action"] == "workspace_tool")
    assert (tool["tool_call_id"], tool["duration_ms"]) == ("call_b", 12.5)
    event = next(e for e in report["events"] if e["action"] == "store_event")
    assert (event["command_id"], event["reported_actor"]) == ("cmd_1", "session:ses_1")
    assert report["rows"][0]["hermes_task_id"] == "task_1"
    assert report["requests"][0]["session_id"] == "ses_1"


def test_hermes_classification_of_a_failed_attempt_stays_in_the_report(
    writer, stream_file, datadog, home
):
    capture = GatewayCapture(writer, "researcher")
    request = request_body([{"role": "user", "content": "Go"}])
    capture.keep_request(request=request, session_id="ses_1")
    capture.pre_api_request(session_id="ses_1", api_request_id="t:api:1")
    capture.api_request_error(
        session_id="ses_1",
        reason="rate_limit",
        status_code=429,
        api_request_id="t:api:1",
        error={"type": "RateLimitError", "message": "slow down"},
    )
    sent(writer, stream_file, datadog)
    report = rebuild_session(search_of(datadog), "ses_1")
    [attempt] = report["requests"]
    assert attempt["failed"]["reason"] == "rate_limit"
    assert attempt["failed"]["details"] == {"status_code": 429}
    assert report["missing"] == []


def test_a_failure_before_any_attempt_of_its_call_is_kept_and_the_rest_rebuilds(
    writer, stream_file, datadog, home
):
    """Hermes reports a call whose request could not be built, so ``pre_api_request`` never
    recorded an attempt of it. Such a failure binds to no other call's attempt, and the rebuild
    keeps it as an event beside the rest of the Session."""
    capture = GatewayCapture(writer, "researcher")

    def unbuilt(call: str) -> None:
        capture.api_request_error(
            session_id="ses_1",
            reason="unknown",
            api_request_id=call,
            error={"type": "ValueError", "message": f"could not build {call}"},
        )

    unbuilt("t:api:1")
    capture.keep_request(
        request=request_body([{"role": "user", "content": "Go"}]), session_id="ses_1"
    )
    capture.pre_api_request(session_id="ses_1", api_request_id="t:api:2")
    capture.post_api_request(
        session_id="ses_1",
        api_request_id="t:api:2",
        assistant_message=SimpleNamespace(content="done", tool_calls=None),
    )
    unbuilt("t:api:3")
    records = sent(writer, stream_file, datadog)
    failures = [r for r in records if r.get("stage") == "failed"]
    assert [("model_attempt_id" in r, r["api_request_id"]) for r in failures] == [
        (False, "t:api:1"),
        (False, "t:api:3"),
    ]

    report = rebuild_session(search_of(datadog), "ses_1")
    [attempt] = report["requests"]
    assert attempt["model_attempt_id"] == "t:api:2#1" and "failed" not in attempt
    assert attempt["response"]["message"] == {"role": "assistant", "content": "done"}
    events = [e for e in report["events"] if e["action"] == "model_attempt"]
    assert [(e["stage"], e["error"]) for e in events] == [
        ("failed", "could not build t:api:1"),
        ("failed", "could not build t:api:3"),
    ]


def test_a_stream_retried_inside_its_attempt_keeps_the_partial_and_the_final_reply(
    writer, stream_file, datadog, home
):
    """Hermes retries a broken stream inside one attempt: what the broken one showed and the
    retry's reply share the attempt and the ``response`` stage, and both come back."""
    capture = GatewayCapture(writer, "researcher")
    capture.keep_request(
        request=request_body([{"role": "user", "content": "Go"}]), session_id="ses_1"
    )
    capture.pre_api_request(session_id="ses_1", api_request_id="t:api:1")
    capture.on_stream_start(session_id="ses_1")
    capture.on_stream_delta(session_id="ses_1", delta="first thoughts", kind="reasoning")
    capture.on_stream_end(session_id="ses_1", final_text="Half an", finished=False)
    capture.on_stream_start(session_id="ses_1")
    capture.post_api_request(
        session_id="ses_1",
        api_request_id="t:api:1",
        assistant_message=SimpleNamespace(content="Half an answer, whole", tool_calls=None),
    )
    sent(writer, stream_file, datadog)

    [attempt] = rebuild_session(search_of(datadog), "ses_1")["requests"]
    assert attempt["response"]["partial"] is False
    assert attempt["response"]["message"]["content"] == "Half an answer, whole"
    [partial] = attempt["partial_responses"]
    assert partial["partial"] is True and partial["outcome"] == "failed"
    assert partial["message"] == {
        "role": "assistant",
        "content": "Half an",
        "reasoning_content": "first thoughts",
    }


def test_a_users_message_survives_a_kill_during_its_first_call(writer, stream_file, datadog, home):
    """The request is written before the call, so a process killed during it keeps the text."""
    capture = GatewayCapture(writer, "researcher")
    text = f"My key is {HERMES_STYLE_SECRET}"
    capture.keep_request(request=request_body([{"role": "user", "content": text}]), session_id="s")
    capture.pre_api_request(session_id="s", api_request_id="t:api:1")
    sent(writer, stream_file, datadog)

    assert HERMES_STYLE_SECRET.encode() not in stream_file.read_bytes()
    report = rebuild_session(search_of(datadog), "s")
    [attempt] = report["requests"]
    assert attempt["request"]["messages"][-1] == {"role": "user", "content": scrub(text)}


@pytest.mark.parametrize(
    ("listed_row", "expected"),
    [(1, "What the model got"), (None, "The row")],
    ids=["a listing with its row ID wins", "a listing with none, as after a restart"],
)
def test_a_user_row_joins_its_listing_by_row_id_or_keeps_its_commit(
    writer, stream_file, datadog, home, listed_row, expected
):
    capture = GatewayCapture(writer, "researcher")
    listing = {"role": "user", "content": "What the model got"}
    if listed_row:
        listing["_row_id"] = listed_row
    writer.capture_request(
        request_body([listing]), session_id="ses_1", provider="moonshot", model_attempt_id="att_1"
    )
    insert_rows(
        home,
        [{"session_id": "ses_1", "role": "user", "content": "The row", "timestamp": time.time()}],
    )
    capture.on_session_end(session_id="ses_1")
    sent(writer, stream_file, datadog)

    report = rebuild_session(search_of(datadog), "ses_1")
    assert report["rows"][0]["message"] == {"role": "user", "content": expected}
    assert report["missing"] == []


def test_content_past_retention_is_reported_missing(writer, stream_file, datadog):
    """One process running past 15 days does not send old content again, and says it is gone."""
    first = {"role": "user", "content": "from the first day"}
    day_0 = 1793817074.0
    writer.capture_request(
        request_body([first]),
        occurred_at=day_0,
        session_id="ses_1",
        provider="moonshot",
        model_attempt_id="att_1",
    )
    retention_start = rec.format_time(day_0 + 86400)
    writer.capture_request(
        request_body([first, {"role": "assistant", "content": "much later"}]),
        occurred_at=day_0 + 16 * 86400,
        session_id="ses_1",
        provider="moonshot",
        model_attempt_id="att_2",
    )
    assert writer.flush(5.0)
    lines = raw_lines(stream_file)
    datadog.take_lines(line for line in lines if json.loads(line)["occurred_at"] >= retention_start)

    report = rebuild_session(search_of(datadog), "ses_1")
    [request] = report["requests"]
    assert request["model_attempt_id"] == "att_2" and request["request"] is None
    [digest] = request["blocked_by"]
    assert {"kind": "no_content", "digest": digest} in report["missing"]
