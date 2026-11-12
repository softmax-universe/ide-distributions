"""The writer, the flush boundary, and what a stuck stream can and cannot hold up."""

import hashlib
import json
import logging
import os
import select
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from research_core.agent_telemetry import ContextPart, Producer, PromptSection, TelemetryWriter
from research_core.agent_telemetry import records as rec
from research_core.agent_telemetry import writer as writer_module
from research_core.agent_telemetry.redaction import SOFTMAX_TOKEN_MASK
from research_core.agent_telemetry.stream import LogStream

from .conftest import HERMES_STYLE_SECRET, processed, raw_lines, stored_records
from .test_redaction import PLAYER_ID, PLAYER_TOKEN, USER_TOKEN

CORE_SRC = Path(__file__).resolve().parents[2] / "src"
ATTEMPT = {"session_id": "ses_1", "model_attempt_id": "att_1", "provider": "moonshot"}


def request_with(*messages):
    return {"model": "moonshot/kimi-k3.1-pro", "messages": list(messages)}


def by_digest(records):
    return {r["content_digest"]: r for r in records if r["family"] == "content"}


def test_an_operation_is_one_line_with_the_shared_fields(writer, stream_file):
    writer.capture_operation(
        "tool",
        "completed",
        occurred_at=1793817000.5,
        outcome="success",
        tool_name="read_file",
        session_id="ses_1",
        profile="researcher-1",
        usage={"input_tokens": 3},
        cost={"usd": 0.1, "source": "estimated"},
        details={"held": False, "message_bytes": 4},
    )
    assert processed(writer, 5.0)
    [record] = stored_records(stream_file)
    assert record["event_id"]
    assert record["occurred_at"] == "2026-11-04T18:30:00.500Z"
    assert record["redaction_version"] == "stand-in+softmax-1"
    assert record["sequence"] == 0
    assert record["service"] == "ide-agent-telemetry"
    assert record["workspace_id"] == "wrk-proof"
    assert "message" not in record


def test_operation_content_is_redacted_hashed_byte_exact_and_referenced(writer, stream_file):
    raw = f"é before {USER_TOKEN} and {HERMES_STYLE_SECRET} lone\ud83d after"
    writer.capture_operation("tool", "completed", content={"result_text": raw, "empty": ""})
    processed(writer, 5.0)
    records = stored_records(stream_file)
    text, operation = records

    expected = f"é before {SOFTMAX_TOKEN_MASK} and sk-abc...2345 lone� after"
    assert text["body"] == expected
    assert "encoding" not in text
    assert text["content_digest"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert text["size_bytes"] == len(expected.encode("utf-8"))

    refs = {ref["name"]: ref["digest"] for ref in operation["content_refs"]}
    assert refs["result_text"] == text["content_digest"]
    # An empty object has no record, and its digest is still the digest of nothing.
    assert refs["empty"] == hashlib.sha256(b"").hexdigest()
    assert operation["sequence"] == text["sequence"] + 1


def test_context_offsets_refer_to_the_redacted_text(writer, stream_file, redactor):
    notice = f"Notice with {USER_TOKEN} inside.\n"
    target = f"{notice}The user's own words. {HERMES_STYLE_SECRET} end"
    words = "The user's own words."
    writer.capture_context_parts(
        target,
        [
            ContextPart("notice", notice),
            ContextPart("user_message", words),
            ContextPart("lost", "text that is not in the target"),
        ],
        session_id="ses_1",
    )
    processed(writer, 5.0)
    records = stored_records(stream_file)
    # The target's text stays out of the stream: a reader has it in the request.
    assert all(record["family"] == "context" for record in records)
    first, second, lost = records
    data = redactor.text(target).encode("utf-8")

    assert first["source_type"] == "notice"
    span = data[first["byte_offset"] : first["byte_offset"] + first["byte_length"]]
    assert span == f"Notice with {SOFTMAX_TOKEN_MASK} inside.\n".encode()
    assert first["target_digest"] == hashlib.sha256(data).hexdigest()

    start = second["byte_offset"]
    assert data[start : start + second["byte_length"]] == words.encode()

    # A part that cannot be found says so, with no byte range.
    assert lost["source_type"] == "unattributed" and "target_digest" not in lost


def test_seeded_secrets_never_reach_the_stream(writer, stream_file):
    canary = [USER_TOKEN, PLAYER_TOKEN, HERMES_STYLE_SECRET, "opaque-canary-field-value"]
    text = " | ".join(canary[:3])
    request = {
        "model": "moonshot/kimi-k3.1-pro",
        "api_key": canary[3],
        "extra_body": {"note": text},
        "tools": [{"type": "function", "function": {"name": "t", "description": text}}],
        "messages": [
            {"role": "system", "content": f"system {text}"},
            {"role": "user", "content": f"user {text}"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": f"thinking {text}",
                "reasoning_details": [{"type": "reasoning.text", "text": text}],
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "t", "arguments": json.dumps({"a": text})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": f"result {text}"},
            {"role": "user", "content": [{"type": "text", "text": text, "cache_control": text}]},
            {"role": "user", "content": "x", "unknown_field": text},
        ],
    }
    writer.capture_request(
        request,
        **ATTEMPT,
        prompt_sections=[PromptSection("soul", f"system {text}")],
    )
    writer.capture_response(
        {"role": "assistant", "content": f"answer {text}"},
        session_id="ses_1",
        model_attempt_id="att_1",
    )
    writer.capture_operation("tool", "completed", content={"args": {"secret": text}, "t": text})
    writer.capture_context_parts(f"target {text}", [ContextPart("notice", text)])
    assert processed(writer, 5.0)

    raw = stream_file.read_bytes()
    for secret in canary:
        assert secret.encode() not in raw
    # The scan is real: other text from the same captures is there.
    assert b"answer" in raw and SOFTMAX_TOKEN_MASK.encode() in raw


def test_details_hold_no_strings_on_any_writer(writer, stream_file, caplog):
    other = TelemetryWriter(stream_file, Producer.for_process("research-cli", {}))
    with caplog.at_level(logging.WARNING):
        writer.capture_operation("tool", "completed", details={"api_key": USER_TOKEN})
        writer.capture_operation("tool", "completed", details={"nested": [PLAYER_TOKEN]})
        other.capture_operation("tool", "completed", details={"api_key": USER_TOKEN})
        writer.capture_operation("tool", "completed", details={"held": False, "message_bytes": 4})
        processed(writer, 5.0)
        processed(other, 5.0)

    raw = stream_file.read_bytes()
    assert USER_TOKEN.encode() not in raw and PLAYER_TOKEN.encode() not in raw
    [record] = stored_records(stream_file)
    assert record["details"] == {"held": False, "message_bytes": 4}
    assert sum("telemetry: dropped" in message for message in caplog.messages) == 3


def test_the_gateway_writer_redacts_id_fields_too(writer, stream_file):
    """A tool name and a call ID can come straight from the model."""
    writer.capture_operation(
        "tool", "completed", tool_name=USER_TOKEN, tool_call_id=f"call_{PLAYER_TOKEN}"
    )
    writer.capture_response(
        {"role": "assistant", "content": "x"},
        session_id=USER_TOKEN,
        model_attempt_id="att_1",
    )
    writer.capture_request(
        {"model": USER_TOKEN, "messages": []},
        session_id=USER_TOKEN,
        model_attempt_id="att_2",
        provider=PLAYER_TOKEN,
        tool_call_id=PLAYER_TOKEN,
    )
    processed(writer, 5.0)
    raw = stream_file.read_bytes()
    assert USER_TOKEN.encode() not in raw and PLAYER_TOKEN.encode() not in raw
    assert stored_records(stream_file)[0]["tool_name"] == SOFTMAX_TOKEN_MASK


def test_a_writer_without_a_scanner_still_masks_softmax_tokens(stream_file):
    """``research serve`` copies a client's ``actor`` and ``client.kind`` into its records."""
    writer = TelemetryWriter(stream_file, Producer.for_process("research-serve", {}))
    writer.capture_operation(
        "admission", "accepted", reported_actor=f"user:{USER_TOKEN}", initiator=PLAYER_TOKEN
    )
    processed(writer, 5.0)
    assert USER_TOKEN.encode() not in stream_file.read_bytes()
    [operation] = stored_records(stream_file)
    assert operation["reported_actor"] == f"user:{SOFTMAX_TOKEN_MASK}"
    assert operation["initiator"] == SOFTMAX_TOKEN_MASK
    assert operation["redaction_version"] == "none"


def test_a_player_id_is_not_treated_as_a_token(writer, stream_file):
    writer.capture_operation("tool", "completed", content={"text": f"player {PLAYER_ID}"})
    processed(writer, 5.0)
    assert PLAYER_ID in stored_records(stream_file)[0]["body"]


def test_metadata_too_large_for_a_line_is_dropped_with_a_warning(writer, stream_file, caplog):
    """Chunking keeps a body inside the limit. A record is kept whole, so this one is a bug."""
    with caplog.at_level(logging.WARNING):
        writer.capture_operation("tool", "completed", tool_name="x" * 5000)
        writer.capture_operation("tool", "completed", tool_name="fits")
        assert processed(writer, 5.0)
    [record] = stored_records(stream_file)
    assert record["tool_name"] == "fits"
    assert sum("dropped a operation line" in message for message in caplog.messages) == 1


def test_a_capture_owns_its_data(writer, stream_file):
    history = [{"role": "user", "content": "original", "extra": {"k": ["v"]}}]
    request = request_with(*history)
    writer.capture_request(request, **ATTEMPT)
    history[0]["content"] = "changed"
    history[0]["extra"]["k"].append("changed")
    request["messages"].append({"role": "user", "content": "changed"})
    processed(writer, 5.0)
    bodies = [r["body"] for r in stored_records(stream_file) if "body" in r]
    assert any("original" in body for body in bodies)
    assert not any("changed" in body for body in bodies)


def test_a_capture_never_raises_and_a_bad_record_does_not_stop_the_writer(
    writer, stream_file, caplog
):
    with caplog.at_level(logging.WARNING):
        writer.capture_operation("tool", "completed", details=object())
        writer.capture_operation("tool", "completed", duration_ms=float("nan"))
        writer.capture_request({"model": "m"}, session_id="s", model_attempt_id="a", provider="p")
        writer.capture_operation("tool", "completed", outcome="success")
    processed(writer, 5.0)
    [record] = stored_records(stream_file)
    assert record["outcome"] == "success"
    assert sum("telemetry: dropped" in message for message in caplog.messages) == 3


def test_a_writer_without_a_redactor_drops_free_text(stream_file, caplog):
    writer = TelemetryWriter(stream_file, Producer.for_process("research-serve", {}))
    with caplog.at_level(logging.WARNING):
        writer.capture_operation("x", "y", content={"text": f"has {USER_TOKEN}"})
        writer.capture_request(request_with(), **ATTEMPT)
        writer.capture_response({}, session_id="s", model_attempt_id="a")
        writer.capture_context_parts("text", [])
        writer.capture_operation("x", "y")
    processed(writer, 5.0)
    assert len(stored_records(stream_file)) == 1
    assert sum("telemetry: dropped" in message for message in caplog.messages) == 4


class StuckStream:
    """A pipe nobody reads, so the writer's next line waits like a stream that has stopped."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "stuck"
        os.mkfifo(self.path)
        self.reader = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        self.writer = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        while True:
            try:
                os.write(self.writer, b"x" * 4096)
            except BlockingIOError:
                break

    def release(self, writer: TelemetryWriter) -> list[dict]:
        """Read until ``writer`` has written everything, however long its thread took to open the
        stream."""
        closing = threading.Thread(target=processed, args=(writer, 30.0))
        closing.start()
        data = b""
        while closing.is_alive():
            data += self._read()
        data += self._read()
        os.close(self.writer)
        os.close(self.reader)
        return [json.loads(line) for line in data.lstrip(b"x").splitlines() if line]

    def _read(self) -> bytes:
        try:
            return os.read(self.reader, 65536)
        except BlockingIOError:
            time.sleep(0.001)
            return b""


def test_callers_do_not_wait_or_log_when_the_stream_is_stuck_and_a_full_queue_drops(
    tmp_path, producer, redactor, caplog, monkeypatch
):
    """Logging can block on a full pipe too, so the writer's thread reports what was dropped."""
    monkeypatch.setattr(writer_module, "MAX_QUEUED_OBSERVATIONS", 3)
    stuck = StuckStream(tmp_path)
    writer = TelemetryWriter(stuck.path, producer, redactor=redactor)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        for index in range(10):
            writer.capture_operation("tool", "completed", tool_call_id=f"call_{index}")
        assert time.monotonic() - started < 0.2
        writer.capture_operation("tool", "completed", details=lambda: "cannot be copied")
        kept = [r["tool_call_id"] for r in stuck.release(writer)]
    # The writer holds one more while it waits. When it takes that one races the loop above, so a
    # later capture can take the freed place.
    assert kept[:3] == ["call_0", "call_1", "call_2"]
    assert kept == sorted(kept)
    assert 3 <= len(kept) <= 4
    assert {record.threadName for record in caplog.records} == {"telemetry-writer"}
    # Once per change: the count rises from one message to the next, and its last value is whole.
    counts = [int(record.args[0]) for record in caplog.records]
    assert counts == sorted(set(counts))
    assert counts[-1] >= 6


def test_the_queue_is_also_bounded_by_size(tmp_path, producer, redactor, monkeypatch):
    """What the thread has taken no longer counts, so "x" leaves room for "y" and not for "z"."""
    monkeypatch.setattr(writer_module, "MAX_QUEUED_BYTES", 500)
    stuck = StuckStream(tmp_path)
    writer = TelemetryWriter(stuck.path, producer, redactor=redactor)
    writer.capture_operation("tool", "completed", content={"text": "x" * 80})
    time.sleep(0.05)
    writer.capture_operation("tool", "completed", content={"text": "y" * 80})
    writer.capture_operation("tool", "completed", content={"text": "z" * 300})
    bodies = [r.get("body") for r in stuck.release(writer)]
    assert "x" * 80 in bodies and "y" * 80 in bodies and "z" * 300 not in bodies


def test_only_one_capture_over_the_byte_limit_waits_at_a_time(
    tmp_path, producer, redactor, monkeypatch
):
    """It counts for nothing against the limit, so what bounds it is that the queue is empty."""
    monkeypatch.setattr(writer_module, "MAX_QUEUED_BYTES", 600)
    stuck = StuckStream(tmp_path)
    writer = TelemetryWriter(stuck.path, producer, redactor=redactor)
    writer.capture_operation("tool", "completed", content={"text": "x" * 80})
    time.sleep(0.05)
    writer.capture_operation("tool", "completed", content={"text": "y" * 3000})
    writer.capture_operation("tool", "completed", content={"text": "z" * 3000})
    bodies = "".join(r.get("body", "") for r in stuck.release(writer))
    assert "y" * 3000 in bodies and "z" not in bodies


def test_one_capture_over_the_byte_limit_still_goes_out_when_nothing_is_queued(
    stream_file, producer, redactor, monkeypatch
):
    """Refusing it would leave its request out of every rebuild, for no memory saved: the pickle
    already exists. The limit is for what piles up."""
    monkeypatch.setattr(writer_module, "MAX_QUEUED_BYTES", 600)
    writer = TelemetryWriter(stream_file, producer, redactor=redactor)
    writer.capture_operation("tool", "completed", content={"text": "z" * 3000})
    assert processed(writer, 5.0)
    assert "z" * 3000 in [r.get("body") for r in stored_records(stream_file)]


def test_a_request_over_the_byte_limit_does_not_cost_its_reply(
    stream_file, producer, redactor, monkeypatch
):
    """The reply comes microseconds after its request, often before the thread has taken it."""
    monkeypatch.setattr(writer_module, "MAX_QUEUED_BYTES", 600)
    writer = TelemetryWriter(stream_file, producer, redactor=redactor)
    for turn in range(20):
        attempt = {**ATTEMPT, "model_attempt_id": f"att_{turn}"}
        writer.capture_request(
            request_with({"role": "user", "content": f"{turn} " * 2000}), **attempt
        )
        writer.capture_response({"role": "assistant", "content": f"reply {turn}"}, **attempt)
        # True means both were processed already, so both are on the stream by now.
        assert processed(writer, 5.0)
        replies = [r for r in stored_records(stream_file) if r.get("stage") == "response"]
        assert len(replies) == turn + 1


def test_a_deferred_job_runs_after_what_was_captured_before_it(writer, stream_file):
    writer.capture_operation("a", "one")
    writer.capture_operation("a", "two")
    assert processed(writer, 5.0)
    assert [r["stage"] for r in stored_records(stream_file)] == ["one", "two"]
    writer.capture_operation("a", "three")
    assert processed(writer, 5.0)
    assert [r["sequence"] for r in stored_records(stream_file)] == [0, 1, 2]


def test_waiting_for_the_writer_is_bounded_and_says_when_it_could_not_finish(
    tmp_path, producer, redactor
):
    stuck = StuckStream(tmp_path)
    writer = TelemetryWriter(stuck.path, producer, redactor=redactor)
    writer.capture_operation("a", "one")
    started = time.monotonic()
    assert not processed(writer, 0.3)
    assert time.monotonic() - started < 1.0
    assert [r["stage"] for r in stuck.release(writer)] == ["one"]


def test_the_queue_empties_with_no_flush(writer, stream_file):
    """Why sleep needs no step of its own: the thread never waits for anything but the stream."""
    writer.capture_operation("a", "one")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not raw_lines(stream_file):
        time.sleep(0.001)
    assert raw_lines(stream_file)


def test_a_failing_stream_costs_only_that_record(tmp_path, producer, caplog):
    """A pipe with no reader fails each write. The next capture still tries."""
    path = tmp_path / "gone"
    os.mkfifo(path)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    writer = TelemetryWriter(path, producer)
    writer.capture_operation("a", "before")
    assert processed(writer, 5.0)
    os.close(reader)
    with caplog.at_level(logging.WARNING):
        writer.capture_operation("a", "lost")
        assert processed(writer, 5.0)
    assert sum("dropped a operation record (BrokenPipeError)" in m for m in caplog.messages) == 1


def test_a_path_that_does_not_exist_is_quiet_and_writes_nothing(tmp_path, caplog):
    stream = LogStream(str(tmp_path / "no" / "such" / "folder" / "stream"))
    with caplog.at_level(logging.WARNING):
        stream.write(b"line\n")
        stream.write(b"line\n")
    assert caplog.messages == []


def test_a_stream_that_cannot_be_opened_warns_once(tmp_path, caplog):
    folder = tmp_path / "a-folder"
    folder.mkdir()
    stream = LogStream(str(folder))
    with caplog.at_level(logging.WARNING):
        stream.write(b"line\n")
        stream.write(b"line\n")
    assert sum("cannot open the stream" in message for message in caplog.messages) == 1


_ESCAPED_TEXT = '日本語 quoted " and\n a newline '

_COMMAND_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
from research_core.agent_telemetry import Producer
from research_core.agent_telemetry.writer import InlineWriter
writer = InlineWriter(sys.argv[2], Producer.for_process("research-command", {}))
for index in range(int(sys.argv[3])):
    writer.capture_operation("command", "completed", size_bytes=index)
"""

_BACKGROUND_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
from research_core.agent_telemetry import Producer, TelemetryWriter
from research_core.agent_telemetry.redaction import Redactor
import threading
def processed(timeout_s):
    done = threading.Event()
    writer.defer(done.set)
    return done.wait(timeout_s)
name, redactor = sys.argv[4], None
if name == "gateway":
    redactor = Redactor(lambda text: text, "stand-in")
writer = TelemetryWriter(sys.argv[2], Producer.for_process(name, {}), redactor=redactor)
for index in range(int(sys.argv[3])):
    if redactor:
        text = f"{index} {sys.argv[5]}" * 700
        writer.capture_operation("tool", "completed", content={"result": text}, size_bytes=index)
    else:
        writer.capture_operation("admission", "accepted", size_bytes=index)
    if index % 16 == 0:
        processed(60.0)
assert processed(120.0)
"""


@pytest.mark.skipif(select.PIPE_BUF < rec.MAX_LINE_BYTES, reason="a pipe here tears smaller lines")
def test_every_kind_of_writer_shares_one_pipe_and_every_line_parses_and_rejoins(tmp_path):
    """Real processes, at once, on a pipe as in a container: short commands, the gateway, serve."""
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    assert stat.S_ISFIFO(pipe.stat().st_mode)
    # Kept open for writing too, so the reader sees no end of file until the children are done.
    reader = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
    holder = os.open(pipe, os.O_WRONLY)
    os.set_blocking(reader, True)
    count = 200
    children = [
        [sys.executable, "-c", _COMMAND_CHILD, str(CORE_SRC), str(pipe), str(count)]
        for _ in range(6)
    ]
    for name in ("gateway", "research-serve"):
        args = [str(CORE_SRC), str(pipe), str(count), name, _ESCAPED_TEXT]
        children.append([sys.executable, "-c", _BACKGROUND_CHILD, *args])
    processes = [
        subprocess.Popen(child, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for child in children
    ]
    data = bytearray()

    def read_all() -> None:
        while chunk := os.read(reader, 65536):
            data.extend(chunk)

    reading = threading.Thread(target=read_all)
    reading.start()
    for process in processes:
        out, errors = process.communicate(timeout=300)
        assert process.returncode == 0, errors
        assert out == "" and "dropped" not in errors
    os.close(holder)
    reading.join(30)
    os.close(reader)

    lines = [line for line in bytes(data).splitlines(keepends=True) if line != b"\n"]
    assert all(len(b"\n" + line) <= rec.MAX_LINE_BYTES for line in lines)
    records = [json.loads(line) for line in lines]
    assert {r["service"] for r in records} == {"ide-agent-telemetry"}
    operations = [r for r in records if r["family"] == "operation"]
    assert len(operations) == len(children) * count

    # Every chunk rejoins under its digest, and sequence is capture order inside each process.
    chunks: dict[str, dict[int, dict]] = {}
    sources: dict[str, list[int]] = {}
    for record in records:
        if record["family"] == "content":
            chunks.setdefault(record["content_digest"], {})[record["chunk_index"]] = record
    for record in operations:
        sources.setdefault(record["source_instance"], []).append(record["size_bytes"])
    assert len(chunks) == count
    for digest, by_index in chunks.items():
        joined = "".join(by_index[i]["body"] for i in range(by_index[0]["chunk_count"]))
        assert hashlib.sha256(joined.encode()).hexdigest() == digest
    assert {len(values) for values in sources.values()} == {count}
    for values in sources.values():
        assert values == list(range(count))


@pytest.mark.parametrize("partial", [b"progress", b"y" * 5000])
def test_a_partial_line_of_other_output_cannot_join_a_telemetry_line(writer, stream_file, partial):
    """Whatever else writes to the stream may have left a line open: an unbuffered ``print`` between
    its text and its newline, or a long line the pipe has split."""
    with stream_file.open("ab", buffering=0) as other:
        other.write(partial)
        writer.capture_operation("turn", "ended", session_id="ses_1")
        processed(writer, 5.0)
        other.write(b" rest\n")
    lines = stream_file.read_bytes().split(b"\n")
    assert lines[0] == partial
    assert json.loads(lines[1])["action"] == "turn"
    assert lines[2] == b" rest"


_PRINTER = """
import sys
sys.stdout.reconfigure(line_buffering=sys.argv[1] == "line")
for number in range(int(sys.argv[2])):
    print("progress text")
    print("step", end="", flush=True)
    print(" ok")
"""


@pytest.mark.skipif(select.PIPE_BUF < rec.MAX_LINE_BYTES, reason="a pipe here tears smaller lines")
@pytest.mark.parametrize(
    ("buffering", "environment"),
    [("raw", {"PYTHONUNBUFFERED": "1"}), ("line", {})],
    ids=["unbuffered as in the image", "line buffered"],
)
def test_real_prints_beside_telemetry_lines_on_one_pipe_lose_no_line(
    tmp_path, producer, redactor, buffering, environment
):
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    reader = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
    holder = os.open(pipe, os.O_WRONLY)
    os.set_blocking(reader, True)
    data = bytearray()

    def read_all() -> None:
        while chunk := os.read(reader, 65536):
            data.extend(chunk)

    reading = threading.Thread(target=read_all)
    reading.start()
    prints = 20000
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    printer = subprocess.Popen(
        [sys.executable, "-c", _PRINTER, buffering, str(prints)],
        stdout=holder,
        env={**env, **environment},
    )
    # A real writer, with every line through its one thread, for as long as the prints last.
    writer = TelemetryWriter(str(pipe), producer, redactor=redactor)
    written = 0
    while printer.poll() is None:
        writer.capture_operation("turn", "ended", session_id="ses_1")
        written += 1
        if written % 500 == 0:
            processed(writer, 30.0)
    assert processed(writer, 60.0)
    assert printer.returncode == 0
    os.close(holder)
    reading.join(30)
    os.close(reader)

    lines = bytes(data).split(b"\n")
    telemetry = [line for line in lines if b"ide-agent-telemetry" in line]
    assert written > 0 and len(telemetry) == written
    assert {json.loads(line)["service"] for line in telemetry} == {"ide-agent-telemetry"}
    assert bytes(data).count(b"progress text") == prints
