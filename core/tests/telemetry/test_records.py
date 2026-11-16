"""Which fields a record may hold, the line format, and body splitting."""

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from research_core.agent_telemetry import Producer
from research_core.agent_telemetry import records as rec

CORE_SRC = Path(__file__).resolve().parents[2] / "src"
OCCURRED_AT = "2026-11-04T18:31:14.123Z"
EVENT = "0199014d-55f8-4b70-a8eb-000000000002"
BASE_PRODUCER = Producer.for_process("research-serve", {})
BASE = rec.envelope(BASE_PRODUCER, "none", OCCURRED_AT, 0)


def slow_split(body: str, max_bytes: int) -> list[str]:
    """One character at a time, kept to check that the fast version gives the same pieces."""
    pieces, start, size = [], 0, 0
    for index, char in enumerate(body):
        needed = len(json.dumps(char, ensure_ascii=False).encode("utf-8")) - 2
        if size + needed > max_bytes:
            pieces.append(body[start:index])
            start, size = index, 0
        size += needed
    pieces.append(body[start:])
    return pieces


@pytest.mark.parametrize("max_bytes", [8, 16, 100, 1000])
def test_split_gives_the_longest_pieces_that_fit(max_bytes):
    generator = random.Random(7)
    alphabet = 'ab"\\\n\t\x01é日本😀\x7f '
    for length in (0, 1, 7, 8, 9, 300, 5000):
        body = "".join(generator.choice(alphabet) for _ in range(length))
        assert rec.split_body(body, max_bytes) == slow_split(body, max_bytes)


def test_an_objects_digest_and_size_are_of_its_utf8_bytes():
    """Pinned: a reader hashes the joined body's UTF-8 bytes, so these may never change."""
    text = rec.text_object("café 日本語 😀")
    assert (text.kind, text.body, text.size_bytes) == ("part", "café 日本語 😀", 20)
    assert text.digest == "dbcef621b3450a8fe17d56ecba2fa0b685456bccc87f5e51b9b431c6fe9162c7"
    value = rec.json_object({"b": 1, "a": "é 😀"}, "tool_list")
    assert (value.kind, value.body, value.size_bytes) == ("tool_list", '{"a":"é 😀","b":1}', 21)
    assert value.digest == "79c41287d341cf0d798c932ea4a45f67a8fb85a05b9ee7126a25fa20bf74f5e0"


def _content_lines(body: str) -> list[bytes]:
    obj = rec.text_object(body)
    producer = Producer.for_process("gateway", {"IDE_WORKSPACE_ID": "wrk-1"})
    return [
        rec.line_bytes(r)
        for r in rec.content_records(obj, rec.envelope(producer, "none", OCCURRED_AT, 7))
    ]


# Each lands a character of some escaped size on a chunk boundary.
LIMIT = rec.CHUNK_MAX_BODY_BYTES
BODIES = {
    "short": "hello",
    "exact_limit_ascii": "a" * LIMIT,
    "one_over_ascii": "a" * (LIMIT + 1),
    "quote_on_boundary": "a" * (LIMIT - 1) + '"' + "b" * 10,
    "backslash_on_boundary": "a" * (LIMIT - 1) + "\\" + "b" * 10,
    "control_on_boundary": "a" * (LIMIT - 3) + "\x01" + "b" * 10,
    "named_escapes": "\b\t\n\f\r" * 4000,
    "del_and_c1": "\x7f\x80\x9f" * 9000,
    "two_byte_on_boundary": "a" * (LIMIT - 1) + "é" + "b",
    "three_byte_on_boundary": "a" * (LIMIT - 2) + "日" + "b",
    "emoji_on_boundary": "a" * (LIMIT - 3) + "😀" + "b",
    "emoji_only": "😀" * 12000,
    "line_separators": "\u2028\u2029" * 8000,
    "mixed": 'read_file("notes.md") -> "café 日本語 😀"\tapi_key=***\n' * 2000,
    "json_body": json.dumps({"service": "x", "text": "\n".join(["line"] * 9000)}),
    "surrounding_space": "  \n padded \n  ",
}


@pytest.mark.parametrize("name", BODIES)
def test_every_line_is_one_json_object_within_the_limit_and_joins_back(name):
    """Measured as the UTF-8 of the whole line, JSON escapes and both newlines included."""
    body = BODIES[name]
    lines = _content_lines(body)
    for line in lines:
        assert len(line) <= rec.MAX_LINE_BYTES
        assert line.startswith(b"\n") and line.endswith(b"\n") and line.count(b"\n") == 2
    records = [json.loads(line) for line in lines]
    assert "".join(record["body"] for record in records) == body
    assert [record["chunk_index"] for record in records] == list(range(len(records)))
    assert {record["chunk_count"] for record in records} == {len(records)}
    assert {record["content_digest"] for record in records} == {rec.text_object(body).digest}
    assert {record["size_bytes"] for record in records} == {len(body.encode())}
    assert {record["service"] for record in records} == {"ide-agent-telemetry"}
    assert {record["workspace_id"] for record in records} == {"wrk-1"}
    assert "message" not in records[0]


def test_a_chunk_is_the_same_text_at_the_same_index_whatever_else_its_line_holds():
    """So that digest and index are enough to name a chunk, whoever sent it and whenever."""
    obj = rec.text_object("日本語 " * 3000)
    plain = [r["body"] for r in rec.content_records(obj, BASE)]
    other = Producer.for_process("another-producer-with-a-longer-name", {"IDE_IMAGE": "i" * 64})
    assert plain == [
        r["body"]
        for r in rec.content_records(obj, rec.envelope(other, "none", OCCURRED_AT, 123456))
    ]


def test_a_full_chunk_of_a_manifest_with_real_id_lengths_leaves_room_on_its_line():
    """Lengths from a real workspace."""
    producer = rec.Producer(
        "gateway",
        "distribution-8e66497bd5a8b175",
        "gateway-08838793f3eb",
        "6f0e1fd2b7a94c52a1b3c4d5e6f70819",
        "58bda2da-38b3-4e89-9d61-5df046c11134",
    )
    obj = rec.json_object(["x" * 64] * 400, "request_manifest")
    base = rec.envelope(producer, "hermes-0.22.6+softmax-1", "2026-11-05T20:31:02.288Z", 123456)
    first = next(rec.content_records(obj, base))
    assert len(first["body"]) == rec.CHUNK_MAX_BODY_BYTES - first["body"].count('"')
    room = rec.MAX_LINE_BYTES - len(rec.line_bytes(first))
    assert room > 150, room


def test_lone_surrogates_become_replacement_characters():
    assert rec.clean_text("a\ud83db") == "a�b"
    assert rec.clean_text("😀") == "😀"
    assert rec.clean_text("﻿x") == "﻿x"


def test_a_field_that_does_not_apply_is_left_out():
    fields = {"session_id": "ses_1", "run_id": None}
    record = rec.operation_record(BASE, EVENT, "tool", "completed", fields, {})
    assert record["session_id"] == "ses_1" and "run_id" not in record
    assert "content_refs" not in record


def test_plugin_side_modules_import_with_the_standard_library_alone():
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(CORE_SRC)!r})\n"
        "import research_core.agent_telemetry\n"
        "import research_core.agent_telemetry.rebuild, research_core.plugin.capture\n"
        "stdlib = sys.stdlib_module_names\n"
        "other = sorted({m.split('.')[0] for m in sys.modules} - stdlib - {'research_core'})\n"
        "assert not [m for m in other if not m.startswith('_')], other\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
