"""Build lines in the agent telemetry format.

This module defines the format: digests, chunks, and the three record families. ``CONTRACT.md``
beside it describes them. Nothing here touches disk or takes secrets out; callers pass redacted
text.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SERVICE = "ide-agent-telemetry"
# The redaction version of a producer that sends no free text.
NO_REDACTION = "none"
# The digest of an object with no bytes. Such an object has no record; readers treat it as empty.
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
# A line includes both its newlines. Above this, concurrent lines tear on the way to Datadog.
MAX_LINE_BYTES = 4096
# A body's chunks depend on the body alone, so a chunk sent again by another process is the
# same text at the same index. This leaves 1,094 bytes for the rest of a line.
CHUNK_MAX_BODY_BYTES = 3000


def canonical_bytes(value: Any) -> bytes:
    """The only serialization digests rely on. Readers hash what they receive."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def line_bytes(record: Mapping[str, Any]) -> bytes:
    """One record as written, between two newlines. JSON escapes every newline in it.

    The first ends whatever line other output left open on the stream, which can happen at any
    moment and which no buffering of ours can prevent: ``stream.py`` says why.
    """
    text = json.dumps(record, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return b"\n" + text.encode("utf-8") + b"\n"


def clean_text(text: str) -> str:
    """Replace lone surrogates with U+FFFD. They cannot be UTF-8."""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def format_time(epoch_s: float) -> str:
    """UTC with exactly three decimals: ``2026-11-04T18:31:14.123Z``."""
    moment = datetime.fromtimestamp(epoch_s, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _escaped_size(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def split_body(body: str, max_bytes: int = CHUNK_MAX_BODY_BYTES) -> list[str]:
    """Pieces of at most ``max_bytes``, measured as JSON-escaped UTF-8, never inside a code point.

    Each one is the longest slice that fits, found by halving. Measuring one character at a time
    gives the same pieces and takes seconds on a large body.
    """
    pieces = []
    start = 0
    while True:
        low = min(start + 1, len(body))
        # Every character takes at least one byte, so nothing past this can fit.
        high = min(start + max_bytes, len(body))
        if _escaped_size(body[start:high]) <= max_bytes:
            low = high  # The longest slice fits, as it does for most text.
        while low < high:
            middle = (low + high + 1) // 2
            if _escaped_size(body[start:middle]) <= max_bytes:
                low = middle
            else:
                high = middle - 1
        pieces.append(body[start:low])
        if low == len(body):
            return pieces
        start = low


@dataclass(frozen=True)
class Producer:
    """What every record says about the process that made it."""

    producer: str
    image: str
    source_instance: str
    runtime_id: str
    workspace_id: str

    @classmethod
    def for_process(cls, producer: str, env: Mapping[str, str] = os.environ) -> Producer:
        """A new ``source_instance``. ``IDE_RUNTIME_ID`` is new at each image start.

        Pod tags do not identify a workspace's owner, so each line names its workspace.
        """
        return cls(
            producer=producer,
            image=env.get("IDE_IMAGE") or "unknown",
            source_instance=f"{producer}-{uuid.uuid4().hex[:12]}",
            runtime_id=env.get("IDE_RUNTIME_ID") or "unknown",
            workspace_id=env.get("IDE_WORKSPACE_ID") or "unknown",
        )


def envelope(
    producer: Producer, redaction_version: str, occurred_at: str, sequence: int
) -> dict[str, Any]:
    return {
        "service": SERVICE,
        "workspace_id": producer.workspace_id,
        "producer": producer.producer,
        "image": producer.image,
        "source_instance": producer.source_instance,
        "runtime_id": producer.runtime_id,
        "redaction_version": redaction_version,
        "occurred_at": occurred_at,
        "sequence": sequence,
    }


def _fields_that_apply(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in fields.items() if value is not None}


def operation_record(
    base: Mapping[str, Any],
    event_id: str,
    action: str,
    stage: str,
    fields: Mapping[str, Any],
    content_refs: Mapping[str, str],
) -> dict[str, Any]:
    """``base`` is the envelope. ``details`` holds no text, because no writer scans inside it."""
    details = fields.get("details") or {}
    if not all(isinstance(value, bool | int | float) for value in details.values()):
        raise ValueError("details hold only numbers and booleans")
    record = {**base, "family": "operation", "event_id": event_id, "action": action, "stage": stage}
    record.update(_fields_that_apply(fields))
    if content_refs:
        record["content_refs"] = [
            {"name": name, "digest": digest} for name, digest in content_refs.items()
        ]
    return record


def context_record(
    base: Mapping[str, Any], event_id: str, source_type: str, fields: Mapping[str, Any]
) -> dict[str, Any]:
    """``base`` is the envelope."""
    record = {**base, "family": "context", "event_id": event_id, "source_type": source_type}
    record.update(_fields_that_apply(fields))
    return record


@dataclass(frozen=True)
class ContentObject:
    """Redacted text, as a record body carries it. ``digest`` covers its UTF-8 bytes alone."""

    kind: str
    body: str
    digest: str
    size_bytes: int


def _utf8_object(kind: str, data: bytes) -> ContentObject:
    digest = hashlib.sha256(data).hexdigest()
    return ContentObject(kind, data.decode("utf-8"), digest, len(data))


def text_object(text: str) -> ContentObject:
    return _utf8_object("part", text.encode("utf-8"))


def json_object(value: Any, kind: str = "part") -> ContentObject:
    return _utf8_object(kind, canonical_bytes(value))


def content_records(obj: ContentObject, base: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """One line per chunk, each with the envelope ``base``. ``content_digest`` and ``chunk_index``
    say which piece of what it is.

    An object can be shared across Sessions, so a chunk carries no IDs of a conversation.
    """
    pieces = split_body(obj.body)
    for index, piece in enumerate(pieces):
        yield {
            **base,
            "family": "content",
            "content_kind": obj.kind,
            "content_digest": obj.digest,
            "size_bytes": obj.size_bytes,
            "chunk_index": index,
            "chunk_count": len(pieces),
            "body": piece,
        }
