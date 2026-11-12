"""Shared helpers for the telemetry tests. No Hermes here: ``scrub`` stands in for its scanner."""

import json
import re
import sqlite3
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from research_core.agent_telemetry import Producer, TelemetryWriter
from research_core.agent_telemetry.rebuild import _Finder, _join_object, _rebuild_request
from research_core.agent_telemetry.redaction import Redactor

HERMES_STYLE_SECRET = "sk-abcdefghijklmnopqrstuvwxyz012345"


def processed(writer: TelemetryWriter, timeout_s: float = 5.0) -> bool:
    """Wait at most ``timeout_s`` for the writer's thread to process what was captured before this
    call. True proves the queue was processed up to here, not that anything arrived: a stream that
    could not be opened, a failed write and a dropped record all still count as processed."""
    done = threading.Event()
    writer.defer(done.set)
    return done.wait(timeout_s)


def scrub(text: str) -> str:
    """A small stand-in for Hermes's ``redact_for_egress``, which changes the length of the text."""
    return re.sub(r"sk-[A-Za-z0-9]{10,}", "sk-abc...2345", text)


def raw_lines(path: Path) -> list[bytes]:
    """Each line as it reached the stream, with its newline. A line's leading newline leaves a blank
    line before it, which is no line of ours."""
    if not path.exists():
        return []
    return [line for line in path.read_bytes().splitlines(keepends=True) if line != b"\n"]


def stream_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in raw_lines(path)]


def stored_records(path: Path) -> list[dict[str, Any]]:
    """What reached the stream, in order, with a body's chunks joined at its first chunk."""
    joined: list[dict[str, Any]] = []
    open_bodies: dict[str, dict[str, Any]] = {}
    for record in stream_lines(path):
        if record["family"] == "content" and record["chunk_index"] > 0:
            open_bodies[record["content_digest"]]["body"] += record["body"]
            continue
        if record["family"] == "content":
            open_bodies[record["content_digest"]] = record
        joined.append(record)
    for record in joined:
        if record["family"] == "content":
            record["chunk_count"] = 1
    return joined


def manifests_of(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        json.loads(record["body"])
        for record in records
        if record.get("content_kind") == "request_manifest"
    ]


def objects_of(records: list[dict[str, Any]]) -> dict[str, bytes]:
    """Every content object's bytes by digest, joined as the production reader joins them."""
    chunks: dict[str, dict[int, dict[str, Any]]] = {}
    for record in records:
        if record["family"] == "content":
            chunks.setdefault(record["content_digest"], {})[record["chunk_index"]] = record
    return {digest: _join_object(digest, found)[0] for digest, found in chunks.items()}


def rebuild_request(manifest_digest: str, objects: dict[str, bytes]) -> dict[str, Any]:
    """One request, by the production reader, from objects already in hand."""
    finder = _Finder(None, "", None)
    finder.objects.update(objects)
    rebuilt = _rebuild_request(finder, manifest_digest)
    assert rebuilt["request"] is not None, rebuilt
    return rebuilt["request"]


@pytest.fixture
def producer() -> Producer:
    return Producer.for_process(
        "gateway",
        {"IDE_IMAGE": "ide@sha256:0f3a", "IDE_RUNTIME_ID": "rt-1", "IDE_WORKSPACE_ID": "wrk-proof"},
    )


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(scrub, "stand-in+softmax-1", lambda key: key == "api_key")


@pytest.fixture
def writer(stream_file, producer, redactor) -> Iterator[TelemetryWriter]:
    yield TelemetryWriter(stream_file, producer, redactor=redactor)


MESSAGES_TABLE = (
    "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
    "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, "
    "finish_reason TEXT, reasoning_content TEXT, compacted INTEGER NOT NULL DEFAULT 0)"
)
ADMISSIONS_TABLE = (
    "CREATE TABLE session_admissions (seq INTEGER PRIMARY KEY, request_id TEXT, "
    "target_session_id TEXT, payload_json TEXT, status TEXT)"
)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A profile home with Hermes's ``state.db`` tables this plugin reads, as Hermes's
    ``hermes_constants`` names it."""
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute(MESSAGES_TABLE)
        db.execute(ADMISSIONS_TABLE)
    (tmp_path / "SOUL.md").write_text("SOUL TEXT\n")
    module = ModuleType("hermes_constants")
    module.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", module)
    return tmp_path


def insert_rows(home: Path, rows: list[dict[str, Any]]) -> None:
    """Rows as Hermes commits them to ``state.db``."""
    with sqlite3.connect(home / "state.db") as db:
        for row in rows:
            names = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            db.execute(f"INSERT INTO messages ({names}) VALUES ({marks})", tuple(row.values()))
