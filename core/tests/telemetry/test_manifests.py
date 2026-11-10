"""Incremental manifests over a growing conversation, and what forces a full one."""

import json

import pytest

from research_core.agent_telemetry import PromptSection, TelemetryWriter
from research_core.agent_telemetry.encoder import locate_parts
from research_core.agent_telemetry.redaction import Redactor

from .conftest import (
    HERMES_STYLE_SECRET,
    manifests_of,
    objects_of,
    rebuild_request,
    scrub,
    stored_records,
)
from .test_redaction import USER_TOKEN

MODEL = "moonshot/kimi-k3.1-pro"


def attempt(number: int, session: str = "ses_1", **fields) -> dict:
    return {
        "session_id": session,
        "model_attempt_id": f"att_{number}",
        "provider": "moonshot",
        **fields,
    }


def grow(writer, turns: int, session: str = "ses_1") -> list[dict]:
    """Send one request per turn, each holding the whole history so far."""
    system = {"role": "system", "content": "You are a researcher."}
    history: list[dict] = []
    for number in range(turns):
        history = [*history, {"role": "user", "content": f"question {number}"}]
        writer.capture_request(
            {"model": MODEL, "messages": [system, *history], "temperature": 0.5},
            **attempt(number, session),
        )
        history = [*history, {"role": "assistant", "content": f"answer {number}"}]
    return history


def marked(message: dict) -> dict:
    """The message as Hermes sends it with a cache marker: its string in one marked part."""
    part = {"type": "text", "text": message["content"], "cache_control": {"type": "ephemeral"}}
    return {**message, "content": [part]}


def test_moving_cache_markers_keep_appends_and_rebuild_exactly(writer, stream_file):
    system_parts = [
        {"type": "text", "text": "You are ", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "a researcher.", "cache_control": {"type": "ephemeral"}},
    ]
    system = {"role": "system", "content": system_parts}
    history: list[dict] = []
    requests = []
    for number in range(8):
        history = [*history, {"role": "user", "content": f"question {number}"}]
        # The markers sit on the last three messages only, wherever those are by now.
        messages = [*history[:-3], *[marked(m) for m in history[-3:]]]
        requests.append({"model": MODEL, "messages": [system, *messages]})
        writer.capture_request(requests[-1], **attempt(number))
        history = [*history, {"role": "assistant", "content": f"answer {number}"}]
    writer.flush(5.0)
    records = stored_records(stream_file)

    manifests = manifests_of(records)
    assert [m["operation"] for m in manifests] == ["full"] + ["append"] * 7
    assert all(len(m["messages"]) == 2 for m in manifests[1:])
    assert manifests[-1]["cache_markers"][-1] == {
        "position": 15,
        "at": "text",
        "cache_control": {"type": "ephemeral"},
    }
    # Every message is stored once, in the form without a marker.
    messages = [json.loads(r["body"]) for r in records if '"role"' in r.get("body", "")]
    messages = [m for m in messages if m.get("role") == "user"]
    assert all(isinstance(m["content"], str) for m in messages)
    objects = objects_of(records)
    digests = [r["content_digest"] for r in records if r.get("content_kind") == "request_manifest"]
    for digest, request in zip(digests, requests, strict=True):
        assert rebuild_request(digest, objects) == request


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(
            {"role": "user", "content": "", "cache_control": {"type": "ephemeral"}},
            id="on the message",
        ),
        pytest.param(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "scaffold", "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": "tail"},
                ],
            },
            id="on one of several parts",
        ),
        pytest.param(
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "u"}},
                    {"type": "text", "text": "t", "cache_control": {"ttl": "1h"}},
                ],
            },
            id="on the last part, beside an image",
        ),
        pytest.param(
            {"role": "user", "content": [{"type": "text", "text": "t", "extra": 1}]},
            id="a part with no marker stays as it came",
        ),
        pytest.param(
            {"role": "user", "content": [{"type": "text", "text": "t", "cache_control": "odd"}]},
            id="a marker of another form stays as it came",
        ),
    ],
)
def test_each_form_of_cache_marker_rebuilds_exactly(writer, stream_file, message):
    request = {"model": MODEL, "messages": [message]}
    writer.capture_request(request, **attempt(1))
    writer.flush(5.0)
    records = stored_records(stream_file)
    [manifest] = [r for r in records if r.get("content_kind") == "request_manifest"]
    assert rebuild_request(manifest["content_digest"], objects_of(records)) == request


def test_a_real_rewrite_under_moving_markers_stays_full(writer, stream_file):
    first = {"role": "tool", "tool_call_id": "c", "content": "a long tool result"}
    pruned = {**first, "content": "[pruned]"}
    question = {"role": "user", "content": "question"}
    writer.capture_request({"model": MODEL, "messages": [first, marked(question)]}, **attempt(1))
    writer.capture_request({"model": MODEL, "messages": [pruned, marked(question)]}, **attempt(2))
    writer.flush(5.0)
    records = stored_records(stream_file)
    assert [m["operation"] for m in manifests_of(records)] == ["full", "full"]


def test_a_growing_conversation_stores_only_new_messages(writer, stream_file):
    turns = 60
    history = grow(writer, turns)
    writer.flush(10.0)
    records = stored_records(stream_file)

    parts = [r for r in records if r.get("content_kind") == "part"]
    bodies = [r["body"] for r in parts]
    assert len(bodies) == len(set(bodies))
    # The system prompt, the options, then each turn's question, and each earlier answer.
    assert len(parts) == 2 + turns + (turns - 1)

    manifests = manifests_of(records)
    assert [m["operation"] for m in manifests] == ["full"] + ["append"] * (turns - 1)
    # An append names only what is new; a turn adds an answer and a question.
    assert len(manifests[0]["messages"]) == 2
    assert all(len(m["messages"]) == 2 for m in manifests[1:])
    assert [m["message_count"] for m in manifests] == [2 + 2 * n for n in range(turns)]

    last = [r for r in records if r.get("content_kind") == "request_manifest"][-1]
    assert rebuild_request(last["content_digest"], objects_of(records)) == {
        "model": MODEL,
        "temperature": 0.5,
        "messages": [{"role": "system", "content": "You are a researcher."}, *history[:-1]],
    }


class CountingRedactor(Redactor):
    """Remembers how much text it was asked to scan."""

    def __init__(self) -> None:
        self.scanned: list[int] = []
        super().__init__(self._count, "counting")

    def _count(self, text: str) -> str:
        self.scanned.append(len(text))
        return text


def test_each_request_redacts_only_what_is_new(stream_file, producer):
    redactor = CountingRedactor()
    writer = TelemetryWriter(stream_file, producer, redactor=redactor)
    long_text = "x" * 50_000
    tool = {"type": "function", "function": {"name": "read_file", "description": long_text}}
    system = {"role": "system", "content": long_text + "s"}
    history = [{"role": "user", "content": long_text + "u"}]
    scanned_per_call = []
    for number in range(5):
        history = [*history, {"role": "assistant", "content": f"answer {number}"}]
        request = {"model": MODEL, "messages": [system, *history], "tools": [tool]}
        writer.capture_request(request, **attempt(number))
        assert writer.flush(5.0)
        scanned_per_call.append(sum(redactor.scanned))
        redactor.scanned.clear()
    writer.flush(5.0)
    assert scanned_per_call[0] > 150_000
    assert all(scanned < 1_000 for scanned in scanned_per_call[1:])

    records = stored_records(stream_file)
    last = [r for r in records if r.get("content_kind") == "request_manifest"][-1]
    assert rebuild_request(last["content_digest"], objects_of(records)) == request


def test_every_manifest_describes_its_own_request(writer, stream_file):
    system = {"role": "system", "content": "s"}
    user = {"role": "user", "content": "u"}
    tool = {"type": "function", "function": {"name": "read_file", "parameters": {}}}
    request = {"model": MODEL, "messages": [system, user], "tools": [tool]}
    writer.capture_request(request, **attempt(1))
    writer.capture_request({"model": "other", "messages": [system, user]}, **attempt(2))
    writer.flush(5.0)
    first, second = manifests_of(stored_records(stream_file))
    assert "tool_list_digest" in first and "tool_list_digest" not in second
    assert second["options_digest"] != first["options_digest"]
    # A retry with the same history is a new attempt with no new messages.
    assert second["operation"] == "append" and second["messages"] == []
    assert second["parent_manifest_digest"]


def test_a_changed_prompt_costs_a_rewrite_manifest_and_only_its_own_text(writer, stream_file):
    """A plain system message is listed like any other, so history is not sent again."""
    history = [{"role": "user", "content": "the question"}, {"role": "assistant", "content": "no"}]
    for number, prompt in enumerate(("You are a researcher.", "You are an orchestrator.")):
        system = {"role": "system", "content": prompt}
        writer.capture_request({"model": MODEL, "messages": [system, *history]}, **attempt(number))
    writer.flush(5.0)
    records = stored_records(stream_file)
    first, second = manifests_of(records)
    assert second["operation"] == "full"
    assert second["messages"][1:] == first["messages"][1:]
    bodies = [r["body"] for r in records if r.get("content_kind") == "part" and "role" in r["body"]]
    assert sum("the question" in body for body in bodies) == 1
    assert sum("orchestrator" in body for body in bodies) == 1


def test_a_changed_message_forces_a_full_manifest(writer, stream_file):
    first = {"role": "user", "content": "one"}
    answer = {"role": "assistant", "content": "two"}
    next_user = {"role": "user", "content": "three"}
    pruned = {"role": "assistant", "content": "[pruned]"}
    writer.capture_request({"model": MODEL, "messages": [first, answer]}, **attempt(1))
    writer.capture_request({"model": MODEL, "messages": [first, answer, next_user]}, **attempt(2))
    # Same length as the baseline's prefix, but an old message changed.
    writer.capture_request({"model": MODEL, "messages": [first, pruned, next_user]}, **attempt(3))
    # Shorter than what came before.
    writer.capture_request({"model": MODEL, "messages": [first]}, **attempt(4))
    writer.flush(5.0)
    manifests = manifests_of(stored_records(stream_file))
    assert [m["operation"] for m in manifests] == ["full", "append", "full", "full"]


def test_a_row_id_that_appears_later_does_not_force_a_full_manifest(writer, stream_file):
    message = {"role": "user", "content": "one"}
    known = {**message, "_row_id": 1188}
    answer = {"role": "assistant", "content": "two"}
    writer.capture_request({"model": MODEL, "messages": [message]}, **attempt(1))
    writer.capture_request({"model": MODEL, "messages": [known, answer]}, **attempt(2))
    writer.capture_request({"model": MODEL, "messages": [known]}, **attempt(3, "ses_2"))
    writer.flush(5.0)
    first, second, other = manifests_of(stored_records(stream_file))
    assert second["operation"] == "append"
    assert other["hermes_message_ids"] == {"0": "1188"}
    assert "hermes_message_ids" not in first
    # A later manifest says what this history learned, though it lists only the new message.
    assert second["hermes_message_ids"] == {"0": "1188"}
    # The ID is ours and not the provider's, so the stored message is the same for both.
    assert other["messages"][0] == first["messages"][0]


def test_a_row_id_is_named_once(writer, stream_file):
    first_row = {"role": "user", "content": "one", "_row_id": 1}
    second_row = {"role": "assistant", "content": "two", "_row_id": 2}
    writer.capture_request({"model": MODEL, "messages": [first_row]}, **attempt(1))
    writer.capture_request({"model": MODEL, "messages": [first_row, second_row]}, **attempt(2))
    writer.flush(5.0)
    _, second = manifests_of(stored_records(stream_file))
    assert second["hermes_message_ids"] == {"1": "2"}


def test_histories_are_kept_apart(writer, stream_file):
    message = {"role": "user", "content": "same"}
    request = {"model": MODEL, "messages": [message]}
    writer.capture_request(request, **attempt(1, profile="orchestrator"))
    writer.capture_request(request, **attempt(2, profile="researcher-1"))
    writer.capture_request(request, **attempt(3, profile="researcher-1"), history_line="auxiliary")
    writer.capture_request(request, **attempt(4, "ses_2", profile="researcher-1"))
    writer.flush(5.0)
    records = stored_records(stream_file)
    # None of them extends another, so all four name one manifest with nothing to walk back to.
    named = {
        ref["digest"]
        for record in records
        if record.get("stage") == "request"
        for ref in record["content_refs"]
    }
    [manifest] = manifests_of(records)
    assert len(named) == 1 and manifest["operation"] == "full"
    # The message itself is shared across all of them, beside the options.
    parts = [r for r in records if r.get("content_kind") == "part"]
    assert len(parts) == 2


def test_a_whole_conversation_rebuilds_as_the_provider_got_it(writer, stream_file):
    """Every form a message can take, compared with the same request after redaction."""
    image = "data:image/png;base64,AAAAeyJhbGciOiJCCCC"
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "not plain", "cache_control": 1}]},
        {"role": "user", "content": ""},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"look {USER_TOKEN}"},
                {"type": "image_url", "image_url": {"url": image}},
                {"type": "image_url", "image_url": {"url": image, "detail": "low"}},
                {"type": "input_audio", "input_audio": {"data": "abcd"}},
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "reasoning_details": [{"type": "reasoning.text", "text": "because"}],
            "reasoning_content": "because",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "index": 0,
                    "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                },
                {"id": "has space", "type": "function", "function": {"name": "x", "arguments": ""}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "lone\ud83d"},
        {"role": "tool", "tool_call_id": "call_2", "content": [{"type": "text", "text": "x"}]},
        {"role": "assistant", "finish_reason": "stop", "tool_calls": []},
        {"role": "assistant", "content": f"key {HERMES_STYLE_SECRET}", "tool_calls": None},
    ]
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    request = {"model": MODEL, "messages": messages, "tools": tools, "api_key": "opaque", "n": None}
    writer.capture_request(request, **attempt(1))
    writer.flush(5.0)
    records = stored_records(stream_file)
    [manifest] = [r for r in records if r.get("content_kind") == "request_manifest"]

    redactor = Redactor(scrub, "stand-in+softmax-1", lambda key: key == "api_key")
    rebuilt = rebuild_request(manifest["content_digest"], objects_of(records))
    assert rebuilt == redactor.value(request)
    assert rebuilt["api_key"] == "[redacted-secret-field]"
    assert USER_TOKEN not in json.dumps(rebuilt)
    assert rebuilt["messages"][2]["content"][1]["image_url"]["url"] == image


def test_an_empty_system_prompt_has_no_object_and_still_comes_back(writer, stream_file):
    request = {"model": MODEL, "messages": [{"role": "system", "content": ""}]}
    writer.capture_request(request, **attempt(1))
    writer.flush(5.0)
    records = stored_records(stream_file)
    [manifest] = [r for r in records if r.get("content_kind") == "request_manifest"]
    assert rebuild_request(manifest["content_digest"], objects_of(records)) == request


def test_prompt_sections_and_tool_lists_are_recorded(writer, stream_file):
    system = f"You are a researcher.\n\nProject rules: never print {USER_TOKEN} anywhere.\n"
    soul = "You are a researcher.\n"
    rules = f"Project rules: never print {USER_TOKEN} anywhere.\n"
    tool = {"type": "function", "function": {"name": "read_file", "parameters": {}}}
    writer.capture_request(
        {"model": MODEL, "messages": [{"role": "system", "content": system}], "tools": [tool]},
        **attempt(1),
        prompt_sections=[
            PromptSection("soul", soul),
            PromptSection("project_rules", rules),
            PromptSection("nowhere", "not in the prompt"),
        ],
    )
    writer.flush(5.0)
    records = stored_records(stream_file)
    objects = objects_of(records)
    [manifest] = manifests_of(records)
    system_data = json.loads(objects[manifest["messages"][0]])["content"].encode()
    assert USER_TOKEN.encode() not in system_data

    sections = json.loads(objects[manifest["prompt_sections_digest"]])
    assert [s["name"] for s in sections["sections"]] == ["soul", "project_rules"]
    slices = [
        system_data[s["byte_offset"] : s["byte_offset"] + s["byte_length"]]
        for s in sections["sections"]
    ]
    assert slices[0] == soul.encode() and slices[1].startswith(b"Project rules: never print [")

    [listed] = json.loads(objects[manifest["tool_list_digest"]])["tools"]
    assert json.loads(objects[listed]) == tool


@pytest.mark.parametrize(
    ("target", "parts", "expected"),
    [
        (b"abcabc", [b"abc", b"abc"], [(0, 3), (3, 3)]),
        (b"abc", [b"x", b"c"], [None, (2, 1)]),
        (b"abc", [b"c", b"a"], [(2, 1), None]),
        (b"abc", [b""], [None]),
    ],
)
def test_locate_parts_searches_forward(target, parts, expected):
    assert locate_parts(target, parts) == expected
