"""Turn one capture into lines on the stream: redaction, hashing, manifests, chunks, the write.

The writer's thread calls this. It is the only place that redacts, so raw text never reaches the
stream.

A request becomes a manifest that lists it by reference. Bodies are sent once by digest, and each
manifest after the first names only the messages that are new. Cache markers sit in the manifest and
not in the messages, because they move at every request. ``CONTRACT.md`` beside this file says what
each line and body holds.

What has been sent is known only in memory, per process. A new process sends a full manifest and
everything it lists.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from research_core.agent_telemetry import records as rec
from research_core.agent_telemetry.records import ContentObject, Producer
from research_core.agent_telemetry.redaction import Redactor, mask_softmax_tokens
from research_core.agent_telemetry.stream import LogStream

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"


@dataclass(frozen=True)
class PromptSection:
    """One stretch of the system prompt, with the source that produced it."""

    name: str
    text: str


@dataclass(frozen=True)
class PromptFile:
    """A section whose text is a file's, read when the request is written, on the writer's thread.
    A file that is missing, or whose text is not in the prompt, names no section."""

    name: str
    path: str

    def section(self) -> PromptSection:
        file = Path(self.path)
        return PromptSection(self.name, file.read_text("utf-8").strip() if file.exists() else "")


@dataclass(frozen=True)
class ContextPart:
    """One piece of a message's text, with the source that added it."""

    source_type: str
    text: str


@dataclass
class _Baseline:
    """What the next manifest of one history line compares with. It advances only after a write."""

    message_digests: list[str]
    manifest_digest: str
    row_ids: dict[int, str]


class _Objects:
    """The objects one observation depends on, by digest, without the empty object.

    A value this process has already redacted, in any Session, is not redacted again. Each request
    then costs a hash of its history, far less than scanning it, and a scan of what is new. Such a
    value has been sent, so the encoder keeps no text between requests. Hermes's prompt sections do
    stay in memory, per Session, in ``plugin/capture.py``.
    """

    def __init__(self, known: Mapping[bytes, str] | None = None) -> None:
        self.made: dict[str, ContentObject] = {}
        self.seen: dict[bytes, str] = {}
        self._known = known or {}

    def add(self, obj: ContentObject) -> str:
        if obj.size_bytes > 0:
            self.made.setdefault(obj.digest, obj)
        return obj.digest

    def add_value(self, raw: Any, make: Callable[[], ContentObject]) -> str:
        """``make`` redacts and encodes ``raw``. ``raw`` may hold text that UTF-8 cannot encode."""
        key = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).digest()
        # Text seen earlier in this request, or sent before, is not redacted again.
        digest = self.seen.get(key) or self._known.get(key) or self.add(make())
        self.seen[key] = digest
        return digest


def locate_parts(target: bytes, parts: Sequence[bytes]) -> list[tuple[int, int] | None]:
    """The ``(byte_offset, byte_length)`` of each part in ``target``, searching forward in order.

    Both sides are redacted text, so these are the offsets a reader of the object sees. ``None``
    means the part cannot be attributed, for example when a mask was cut differently in the whole.
    """
    cursor = 0
    located: list[tuple[int, int] | None] = []
    for part in parts:
        offset = target.find(part, cursor) if part else -1
        if offset < 0:
            located.append(None)
        else:
            located.append((offset, len(part)))
            cursor = offset + len(part)
    return located


def message_text(message: Any) -> str | None:
    """The text of a message: its content string, or its text parts joined with nothing between.

    On the hosted route Hermes cuts one string into parts to place cache markers, so joining them
    gives the string back. ``None`` when there is no text.
    """
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = [
        part["text"]
        for part in content
        if isinstance(part, Mapping)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]
    return "".join(texts) if texts else None


def split_cache_markers(message: Mapping[str, Any], position: int) -> tuple[dict, list[dict]]:
    """The message without its cache markers, and an entry saying where each one goes back.

    Hermes moves its markers to the last messages at each request, and wraps a marked string in a
    list of one part. Without the markers, a message is the same object at every request, so the
    history stays a plain extension of the last one. ``CONTRACT.md`` says how to restore them.
    """
    stripped = dict(message)
    markers = []
    if isinstance(stripped.get("cache_control"), Mapping):
        marker = stripped.pop("cache_control")
        markers.append({"position": position, "at": "message", "cache_control": marker})
    content = stripped.get("content")
    if not isinstance(content, list):
        return stripped, markers
    if len(content) == 1 and _is_wrapped_text(content[0]):
        stripped["content"] = content[0]["text"]
        marker = content[0]["cache_control"]
        markers.append({"position": position, "at": "text", "cache_control": marker})
        return stripped, markers
    parts = list(content)
    for index, part in enumerate(content):
        if isinstance(part, Mapping) and isinstance(part.get("cache_control"), Mapping):
            parts[index] = {name: value for name, value in part.items() if name != "cache_control"}
            marker = part["cache_control"]
            markers.append(
                {"position": position, "at": "part", "part": index, "cache_control": marker}
            )
    stripped["content"] = parts
    return stripped, markers


def _is_wrapped_text(part: Any) -> bool:
    return (
        isinstance(part, Mapping)
        and set(part) == {"type", "text", "cache_control"}
        and part["type"] == "text"
        and isinstance(part["text"], str)
        and isinstance(part["cache_control"], Mapping)
    )


class RecordEncoder:
    """Owns the sequence numbers and manifest baselines of one process. One thread at a time."""

    def __init__(
        self,
        stream: LogStream,
        producer: Producer,
        redactor: Redactor | None,
    ) -> None:
        self._stream = stream
        self._producer = producer
        self._redactor = redactor
        self._redaction_version = redactor.version if redactor else rec.NO_REDACTION
        self._next_sequence = 0
        self._baselines: dict[tuple[str, ...], _Baseline] = {}
        # What this process has sent, and the redacted digest of each value it sent in a
        # request, by a hash of the value's raw form.
        self._sent: set[str] = set()
        self._redacted: dict[bytes, str] = {}

    def write(self, kind: str, occurred_at: float, payload: Mapping[str, Any]) -> None:
        """``payload`` is the writer's own copy of what a callback saw."""
        getattr(self, f"_write_{kind}")(payload, rec.format_time(occurred_at))

    def _sequence(self) -> int:
        self._next_sequence += 1
        return self._next_sequence - 1

    def _write_content(self, objects: _Objects, occurred_at: str) -> None:
        """Write each object not sent yet, a line at a time as it is made. A caller writes content
        before the line that names it, so that a line never names what is yet to come, and marks
        the objects sent after that line."""
        for digest, obj in objects.made.items():
            if digest not in self._sent:
                self._emit(rec.content_records(obj, self._base(occurred_at)))

    def _emit(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            line = rec.line_bytes(row)
            if len(line) <= rec.MAX_LINE_BYTES:
                self._stream.write(line)
            else:
                # Chunking keeps a body inside the limit, so this is metadata too large: a bug.
                logger.warning("telemetry: dropped a %s line of %d bytes", row["family"], len(line))

    def _base(self, occurred_at: str) -> dict[str, Any]:
        return rec.envelope(self._producer, self._redaction_version, occurred_at, self._sequence())

    def _metadata(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """IDs and names can come straight from the model or a client, so they are scanned too. A
        writer with no scanner still knows the Softmax token patterns."""
        scan = self._redactor.text if self._redactor else mask_softmax_tokens
        return {k: scan(v) if isinstance(v, str) else v for k, v in fields.items()}

    # Operations and context records.

    def _write_operation(self, payload: Mapping[str, Any], occurred_at: str) -> None:
        objects = _Objects(self._redacted)
        refs = {
            name: objects.add_value(
                value,
                partial(self._text_object if isinstance(value, str) else self._json_object, value),
            )
            for name, value in payload["content"].items()
        }
        self._write_content(objects, occurred_at)
        fields = self._metadata(payload["fields"])
        event_id, action, stage = str(uuid.uuid4()), payload["action"], payload["stage"]
        base = self._base(occurred_at)
        self._emit([rec.operation_record(base, event_id, action, stage, fields, refs)])
        self._sent.update(objects.made)
        self._redacted.update(objects.seen)

    def _write_context_parts(self, payload: Mapping[str, Any], occurred_at: str) -> None:
        """Say where each part sits in the redacted target. A reader counts what no part covers.

        The target itself is not sent: a reader finds it by hashing each message of a request.
        """
        target = self._text_object(payload["target"])
        parts: Sequence[ContextPart] = payload["parts"]
        redacted_parts = [self._redact().text(part.text).encode("utf-8") for part in parts]
        located = locate_parts(target.body.encode("utf-8"), redacted_parts)
        rows = []
        for part, data, span in zip(parts, redacted_parts, located, strict=True):
            fields = {**payload["fields"], "size_bytes": len(data)}
            if span:
                fields.update(target_digest=target.digest, byte_offset=span[0], byte_length=span[1])
            source_type = part.source_type if span else "unattributed"
            event_id = str(uuid.uuid4())
            base = self._base(occurred_at)
            rows.append(rec.context_record(base, event_id, source_type, self._metadata(fields)))
        self._emit(rows)

    # Redacting content into objects.

    def _redact(self) -> Redactor:
        if self._redactor is None:
            raise ValueError("this writer has no redactor, so it takes no free text")
        return self._redactor

    def _text_object(self, text: str) -> ContentObject:
        return rec.text_object(self._redact().text(text))

    def _json_object(self, value: Any) -> ContentObject:
        return rec.json_object(self._redact().value(value))

    # Model requests.

    def _tool_list_object(self, tools: Sequence[Any], objects: _Objects) -> ContentObject:
        digests = [objects.add_value(tool, partial(self._json_object, tool)) for tool in tools]
        return rec.json_object({"tools": digests}, "tool_list")

    def _prompt_sections_object(
        self, system_text: str, sections: Sequence[PromptSection]
    ) -> ContentObject:
        """A section that cannot be found is left out; a reader sees its bytes as unattributed."""
        system = self._text_object(system_text)
        located = locate_parts(
            system.body.encode("utf-8"),
            [self._redact().text(section.text).encode("utf-8") for section in sections],
        )
        listed = [
            {"name": section.name, "byte_offset": span[0], "byte_length": span[1]}
            for section, span in zip(sections, located, strict=True)
            if span is not None
        ]
        return rec.json_object({"sections": listed}, "prompt_sections")

    def _request_head(self, payload: Mapping[str, Any], objects: _Objects) -> dict[str, Any]:
        """The manifest fields that describe this request alone."""
        request: Mapping[str, Any] = payload["request"]
        messages = request["messages"]
        head: dict[str, Any] = {}
        # A system message is listed like any other. Its sections point into its text.
        system = messages[0] if messages and messages[0].get("role") == "system" else None
        system_text = message_text(system)
        sections = [
            section
            for listed in payload["prompt_sections"]
            if (section := listed.section() if isinstance(listed, PromptFile) else listed).text
        ]
        if system_text is not None and sections:
            raw = [system_text, [[s.name, s.text] for s in sections]]
            head["prompt_sections_digest"] = objects.add_value(
                raw, partial(self._prompt_sections_object, system_text, sections)
            )
        # Only a list with something in it moves out. Omitted, empty and null stay as they came.
        options = {name: value for name, value in request.items() if name != "messages"}
        tools = request.get("tools")
        if tools and isinstance(tools, list):
            del options["tools"]
            head["tool_list_digest"] = objects.add(self._tool_list_object(tools, objects))
        if options:
            head["options_digest"] = objects.add_value(options, partial(self._json_object, options))
        return head

    def _write_request(self, payload: Mapping[str, Any], occurred_at: str) -> None:
        self._redact()
        fields = payload["fields"]
        baseline_key = (fields.get("profile") or "", payload["session_id"], payload["history_line"])
        baseline = self._baselines.get(baseline_key)
        objects = _Objects(self._redacted)
        model = payload["request"].get("model")
        ids = self._metadata(
            {
                "session_id": payload["session_id"],
                "model_attempt_id": payload["model_attempt_id"],
                "provider": payload["provider"],
                "model": model if isinstance(model, str) else UNKNOWN,
            }
        )
        body = self._request_head(payload, objects)

        digests: list[str] = []
        row_ids: dict[int, str] = {}
        cache_markers: list[dict[str, Any]] = []
        for position, message in enumerate(payload["request"]["messages"]):
            # The row ID is ours, not part of the wire message, and it can appear later.
            wire = {name: value for name, value in message.items() if name != "_row_id"}
            wire, markers = split_cache_markers(wire, position)
            cache_markers.extend(markers)
            digests.append(objects.add_value(wire, partial(self._json_object, wire)))
            if message.get("_row_id") is not None:
                row_ids[position] = str(message["_row_id"])

        if baseline and digests[: len(baseline.message_digests)] == baseline.message_digests:
            first_listed = len(baseline.message_digests)
            known_rows = baseline.row_ids
            body.update(operation="append", parent_manifest_digest=baseline.manifest_digest)
        else:
            first_listed = 0
            known_rows = {}
            body["operation"] = "full"
        body.update(message_count=len(digests), messages=digests[first_listed:])
        # Hermes can name a row only after it first went out, so an append can name an older one.
        listed_row_ids = {str(p): row for p, row in row_ids.items() if known_rows.get(p) != row}
        if listed_row_ids:
            body["hermes_message_ids"] = listed_row_ids
        if cache_markers:
            body["cache_markers"] = cache_markers
        manifest = rec.json_object(body, "request_manifest")
        objects.add(manifest)

        operation_fields = {**self._metadata(fields), **ids}
        self._write_content(objects, occurred_at)
        event_id = str(uuid.uuid4())
        refs = {"request_manifest": manifest.digest}
        base = self._base(occurred_at)
        operation = "model_attempt", "request", operation_fields, refs
        self._emit([rec.operation_record(base, event_id, *operation)])
        self._sent.update(objects.made)
        self._redacted.update(objects.seen)
        self._baselines[baseline_key] = _Baseline(digests, manifest.digest, row_ids)
