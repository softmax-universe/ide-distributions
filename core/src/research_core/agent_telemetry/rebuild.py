"""``research telemetry rebuild``: one Session rebuilt from Datadog's Logs search API alone.

It uses no state.db, volume or workspace API. ``CONTRACT.md`` beside this file defines the lines,
the bodies and the joins. ``docs/agent-telemetry.md`` says how to run it and what it cannot show.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from research_core.agent_telemetry.encoder import message_text
from research_core.agent_telemetry.records import EMPTY_DIGEST

SEARCH_PATH = "/api/v2/logs/events/search"
PAGE_LIMIT = 1000  # Datadog's largest page.
# 200 digests make a 14,000-character query. Datadog answered one of 140,000 on 2026-11-04.
VALUES_PER_SEARCH = 200
# Agent content stays in the ``main`` index for 15 days, so nothing older can be there.
SEARCH_WINDOW = "now-15d"
# What a structured body refers to, besides its lists.
_SINGLE_REFERENCES = (
    "parent_manifest_digest",
    "prompt_sections_digest",
    "tool_list_digest",
    "options_digest",
)


class AmbiguousSession(Exception):
    """More than one workspace has records under this Session ID."""


class IncompleteSearch(Exception):
    """Datadog answered a search with a status other than ``done``, such as ``timeout``, so what
    it returned may be partial."""


class LogsSearch:
    """Logs search over the Standard indexes. It waits out a 429 for as long as Datadog says."""

    def __init__(
        self, url: str, api_key: str, app_key: str, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._url = url.rstrip("/") + SEARCH_PATH
        self._keys = {"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key}
        self._sleep = sleep

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", **self._keys}
        while True:
            request = urllib.request.Request(self._url, json.dumps(body).encode(), headers)
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code != 429:
                    raise
                self._sleep(max(1.0, float(error.headers.get("X-RateLimit-Reset") or 1)))

    def entries(self, query: str) -> Iterator[dict[str, Any]]:
        """Each log entry's own fields, which Datadog nests under ``attributes.attributes``."""
        window = {"from": SEARCH_WINDOW, "to": "now", "storage_tier": "indexes"}
        page: dict[str, Any] = {"limit": PAGE_LIMIT}
        while True:
            filters = {"query": query, **window}
            answer = self._post({"filter": filters, "page": page, "sort": "timestamp"})
            meta = answer.get("meta") or {}
            if meta.get("status") not in (None, "done"):
                raise IncompleteSearch(f"the search answered status {meta['status']!r}")
            for row in answer.get("data") or ():
                yield row["attributes"]["attributes"]
            page["cursor"] = (meta.get("page") or {}).get("after")
            if not page["cursor"]:
                return


def _any_of(field: str, values: Iterable[str]) -> str:
    return f"@{field}:(" + " OR ".join(f'"{value}"' for value in sorted(values)) + ")"


def _batches(values: Iterable[str]) -> Iterator[list[str]]:
    ordered = sorted(values)
    for start in range(0, len(ordered), VALUES_PER_SEARCH):
        yield ordered[start : start + VALUES_PER_SEARCH]


def _join_object(digest: str, chunks: dict[int, dict]) -> tuple[bytes | None, list]:
    """The object's bytes, or the reasons there are none."""
    first = next(iter(chunks.values()))
    absent = [index for index in range(first["chunk_count"]) if index not in chunks]
    if absent:
        return None, [{"kind": "chunk_gap", "digest": digest, "missing_chunks": absent}]
    body = "".join(chunks[index]["body"] for index in range(first["chunk_count"]))
    data = body.encode("utf-8", "replace")
    if hashlib.sha256(data).hexdigest() == digest and len(data) == first["size_bytes"]:
        return data, []
    return None, [{"kind": "digest_failure", "digest": digest, "size_bytes": len(data)}]


class _Finder:
    """What has been found so far: records by ``event_id``, chunks by digest and index. These keys
    are what drops duplicates. ``missing`` holds what could not be read, by digest."""

    def __init__(self, search: LogsSearch, session_id: str, workspace: str | None) -> None:
        self.search = search
        self.session_ids = {session_id}
        self.workspaces = {workspace} if workspace else set()
        self.records: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[int, dict]] = defaultdict(dict)
        self.objects: dict[str, bytes | None] = {EMPTY_DIGEST: b""}
        self.searched: set[str] = set()
        self.parsed: dict[str, Any] = {}
        self.missing: dict[str, list[dict[str, Any]]] = {}

    def _find(self, condition: str) -> None:
        scope = _any_of("workspace_id", self.workspaces) if self.workspaces else ""
        for entry in self.search.entries(f"service:ide-agent-telemetry {scope} {condition}"):
            self.workspaces.add(entry["workspace_id"])
            if entry["family"] == "content":
                self.chunks[entry["content_digest"]][entry["chunk_index"]] = entry
            else:
                self.records[entry["event_id"]] = entry
        if len(self.workspaces) > 1:
            raise AmbiguousSession(", ".join(sorted(self.workspaces)))

    def find_session(self) -> None:
        """The Session, what compaction moved it to, and what its admissions and traces recorded."""
        names = ("session_id", "hermes_session_id", "parent_session_id")
        searched: set[str] = set()
        while self.session_ids - searched:
            new = self.session_ids - searched
            searched |= new
            for batch in _batches(new):
                self._find("(" + " OR ".join(_any_of(name, batch) for name in names) + ")")
            for record in self.records.values():
                linked = {record.get(name) for name in names} - {None}
                if record.get("action") == "compaction" and linked & self.session_ids:
                    self.session_ids |= linked
        # A retried request names only its admission, and a store event or a workspace tool's
        # record only the trace of the message it served. A record written before records named
        # their trace (retained for 15 days) names its run instead.
        for field in ("admission_id", "otel_trace_id", "run_id"):
            values = {r[field] for r in self.records.values() if self.owned(r) and field in r}
            for batch in _batches(values):
                self._find(_any_of(field, batch))

    def owned(self, record: dict[str, Any]) -> bool:
        """A record with another Session's ID came here by a link, and is not this Session's."""
        session = record.get("session_id") or record.get("hermes_session_id")
        return session is None or session in self.session_ids

    def load(self, digests: Iterable[str]) -> None:
        """Find each object named, wherever it was first stored, and all that those name in turn."""
        frontier = set(digests)
        seen: set[str] = set()
        while frontier:
            seen |= frontier
            wanted = {d for d in frontier if d not in self.searched and self.read(d) is None}
            self.searched |= wanted
            for batch in _batches(wanted):
                self._find(_any_of("content_digest", batch))
            for digest in wanted:
                del self.objects[digest]
                self.read(digest)
            frontier = {name for digest in frontier for name in self._names(digest)} - seen

    def read(self, digest: str) -> bytes | None:
        if digest not in self.objects:
            data, problems = None, [{"kind": "no_content", "digest": digest}]
            if self.chunks.get(digest):
                data, problems = _join_object(digest, self.chunks[digest])
            self.objects[digest] = data
            self.missing.pop(digest, None)
            if data is None:
                self.missing[digest] = problems
        return self.objects[digest]

    def text(self, digest: str | None) -> str | None:
        data = self.read(digest or EMPTY_DIGEST)
        return None if data is None else data.decode("utf-8")

    def json(self, digest: str | None) -> Any:
        digest = digest or EMPTY_DIGEST
        if digest not in self.parsed:
            data = self.read(digest)
            self.parsed[digest] = json.loads(data) if data else None
        return self.parsed[digest]

    def _names(self, digest: str) -> list[str]:
        """What a structured object refers to. A native message or text refers to nothing."""
        entries = self.chunks.get(digest)
        kind = next(iter(entries.values()))["content_kind"] if entries else "part"
        body = None if kind == "part" else self.json(digest)
        if body is None:
            return []
        one = [body[name] for name in _SINGLE_REFERENCES if name in body]
        return [*one, *body.get("messages", ()), *body.get("tools", ())]


def _with_marker(message: dict[str, Any], marker: dict[str, Any]) -> dict[str, Any]:
    """A copy, because stored messages are shared between requests."""
    control = marker["cache_control"]
    if marker["at"] == "message":
        return {**message, "cache_control": control}
    if marker["at"] == "text":
        part = {"type": "text", "text": message["content"], "cache_control": control}
        return {**message, "content": [part]}
    content = list(message["content"])
    content[marker["part"]] = {**content[marker["part"]], "cache_control": control}
    return {**message, "content": content}


def _rebuild_request(finder: _Finder, manifest_digest: str) -> dict[str, Any]:
    """``CONTRACT.md``, "Rebuilding a request". A missing object blocks the request."""
    chain = []
    digest = manifest_digest
    while (link := finder.json(digest)) is not None:
        chain.insert(0, link)
        if link["operation"] != "append":
            break
        digest = link["parent_manifest_digest"]
    if link is None:
        return {"request": None, "blocked_by": [digest]}
    manifest = chain[-1]
    digests = [listed for part in chain for listed in part["messages"]]
    tool_list = finder.json(manifest.get("tool_list_digest"))
    tools = (tool_list or {}).get("tools", [])
    others = [manifest.get("options_digest"), manifest.get("tool_list_digest"), *tools]
    blocked = {d for d in [*digests, *others] if d and finder.read(d) is None}
    if blocked:
        return {"request": None, "blocked_by": sorted(blocked)}
    if len(digests) != manifest["message_count"]:
        return {"request": None, "blocked_by": [manifest_digest]}
    messages = [finder.json(listed) for listed in digests]
    for marker in manifest.get("cache_markers", ()):
        messages[marker["position"]] = _with_marker(messages[marker["position"]], marker)
    request = dict(finder.json(manifest.get("options_digest")) or {})
    request["messages"] = messages
    if tools:
        request["tools"] = [finder.json(tool) for tool in tools]
    rows = {
        position: row
        for part in chain
        for position, row in part.get("hermes_message_ids", {}).items()
    }
    return {
        "request": request,
        "message_digests": digests,
        "hermes_message_ids": rows,
        "operation": manifest["operation"],
    }


def _source_chars(finder: _Finder, manifest: dict, request: dict, places: dict) -> dict[str, int]:
    """Characters by source for one request. ``places`` holds context parts by target digest."""
    chars: dict[str, int] = defaultdict(int)

    def add_spans(text: str, spans: Iterable[tuple[str, int, int]], rest: str) -> None:
        """Text seen twice has its parts recorded twice. The first to claim a byte has it."""
        data = text.encode("utf-8")
        taken = 0
        claimed = 0
        for source, start, length in sorted(spans, key=lambda span: span[1]):
            if start < claimed:
                continue
            count = len(data[start : start + length].decode("utf-8", "replace"))
            chars[source] += count
            taken += count
            claimed = start + length
        chars[rest] += len(text) - taken

    sections = finder.json(manifest.get("prompt_sections_digest")) or {}
    for message in request["messages"]:
        text = message_text(message) or ""
        role = message.get("role")
        if role == "system":
            spans = [
                (section["name"], section["byte_offset"], section["byte_length"])
                for section in sections.get("sections", ())
            ]
            add_spans(text, spans, "system_unattributed")
        elif role == "user":
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            add_spans(text, places.get(digest, ()), "user_unattributed")
        elif role == "tool":
            chars["tool_results"] += len(text)
        else:
            chars["assistant_text"] += len(text)
            chars["reasoning"] += len(message.get("reasoning_content") or "")
            chars["reasoning"] += len(message.get("reasoning") or "")
            for call in message.get("tool_calls") or ():
                arguments = (call.get("function") or {}).get("arguments")
                chars["tool_call_arguments"] += len(arguments or "")
    for tool in (finder.json(manifest.get("tool_list_digest")) or {}).get("tools", ()):
        chars["tool_schemas"] += len(finder.text(tool) or "")
    return {source: count for source, count in chars.items() if count}


def _token_rows(chars: dict[str, int], usage: dict[str, Any] | None) -> list[dict[str, Any]]:
    prompt_tokens = (usage or {}).get("input_tokens")
    rows = []
    for source, count in sorted(chars.items(), key=lambda item: -item[1]):
        rows.append({"source": source, "chars": count})
        if prompt_tokens is not None:
            rows[-1]["estimated_tokens"] = round(prompt_tokens * count / sum(chars.values()))
    return rows


def _order(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest process first, then by time inside a process, where there is one clock.

    A restart makes a new ``source_instance`` and restarts ``sequence``. Within a process
    ``sequence`` alone is not wire order: it is the order the hooks ran in, and an auxiliary call,
    such as a title, runs beside the turn's calls.
    """
    items = list(records)
    starts: dict[str, str] = {}
    for record in items:
        instance = record["source_instance"]
        starts[instance] = min(starts.get(instance, record["occurred_at"]), record["occurred_at"])
    return sorted(
        items,
        key=lambda r: (
            starts[r["source_instance"]],
            r["source_instance"],
            r["occurred_at"],
            r["sequence"],
        ),
    )


def _ref(record: dict[str, Any], name: str) -> str | None:
    return next((r["digest"] for r in record.get("content_refs", ()) if r["name"] == name), None)


def _with_text(finder: _Finder, record: dict[str, Any]) -> dict[str, Any]:
    """The record as written, with the text of what it refers to under each reference's name."""
    refs = record.get("content_refs", ())
    return {**record, **{ref["name"]: finder.text(ref["digest"]) for ref in refs}}


def _of_attempt(record: dict[str, Any]) -> bool:
    """A record of one model attempt. A call that failed before any attempt of it was recorded has
    a ``model_attempt`` / ``failed`` record with no attempt, which stays among the events."""
    return record["action"] == "model_attempt" and "model_attempt_id" in record


def _build_requests(finder: _Finder, operations: list[dict], places: dict) -> list[dict]:
    replies: dict[str, dict] = defaultdict(dict)
    # What a broken stream had shown, which Hermes can retry inside the same attempt.
    partials: dict[str, list] = defaultdict(list)
    for record in filter(_of_attempt, operations):
        if record.get("partial"):
            partials[record["model_attempt_id"]].append(record)
        else:
            replies[record["model_attempt_id"]][record["stage"]] = record
    requests = []
    attempts = [r for r in operations if r["action"] == "model_attempt" and r["stage"] == "request"]
    for position, attempt in enumerate(_order(attempts), start=1):
        entry = {"position": position, **attempt, "tools": []}
        manifest_digest = _ref(attempt, "request_manifest")
        entry["manifest_digest"] = manifest_digest
        entry.update(_rebuild_request(finder, manifest_digest or EMPTY_DIGEST))
        stages = replies[attempt["model_attempt_id"]]
        reply = stages.get("response", {})
        if reply:
            entry["response"] = {**reply, "message": finder.json(_ref(reply, "message"))}
        if partials[attempt["model_attempt_id"]]:
            entry["partial_responses"] = [
                {**partial, "message": finder.json(_ref(partial, "message"))}
                for partial in partials[attempt["model_attempt_id"]]
            ]
        for stage in ("failed", "unread"):
            if stage in stages:
                entry[stage] = _with_text(finder, stages[stage])
        if entry["request"] is not None:
            manifest = finder.json(manifest_digest)
            chars = _source_chars(finder, manifest, entry["request"], places)
            entry["tokens_by_source"] = _token_rows(chars, reply.get("usage"))
        requests.append(entry)
    # A reply, failure or partial reply whose request record was lost.
    for attempt_id in sorted(replies.keys() | partials.keys()):
        if "request" not in replies[attempt_id]:
            problem = {"kind": "reply_with_no_request", "model_attempt_id": attempt_id}
            finder.missing[attempt_id] = [problem]
    return requests


def _scope(record: dict[str, Any]) -> str | None:
    """What a tool call ID is unique within: the record's trace, or its run on a record written
    before records named their trace."""
    return record.get("otel_trace_id") or record.get("run_id")


def _attach_tools(finder: _Finder, operations: list[dict], requests: list[dict]) -> list[dict]:
    """Tool activity on the request that asked for it. Returns what names no such request."""
    by_attempt = {request["model_attempt_id"]: request for request in requests}
    by_call = {r["api_request_id"]: r for r in requests if "api_request_id" in r}  # Last retry.
    unattached: list[dict] = []
    # A result names only its tool call, so it goes where that call's completion went.
    of_tool_call: dict[tuple, dict | None] = {}
    for record in operations:
        if record["action"] != "tool":
            continue
        key = (_scope(record), record.get("tool_call_id"))
        request = (
            by_attempt.get(record.get("model_attempt_id"))
            or by_call.get(record.get("api_request_id"))
            or of_tool_call.get(key)
        )
        if record["stage"] in ("completed", "refused"):
            of_tool_call[key] = request
        (request["tools"] if request else unattached).append(_with_text(finder, record))
    return unattached


def _results_of_rows(operations: list[dict]) -> dict:
    """Each tool result by its row ID. One with none goes by ``(_scope, tool_call_id)``, and when
    several share that nothing says which is whose, so it holds ``None``."""
    results: dict = {}
    for record in operations:
        if (record["action"], record["stage"]) == ("tool", "result"):
            call = (_scope(record), record.get("tool_call_id"))
            if row := record.get("hermes_message_id"):
                results[row] = record
            else:
                results[call] = None if call in results else record
    return results


def _unlisted_message(finder: _Finder, row: dict, result: dict | None) -> dict | None:
    """A row no request lists: its commit holds its own text, and a tool row takes the result
    recorded for it. ``None`` when that text is lost or was never recorded."""
    own = _ref(row, "message")
    if own:
        return finder.json(own)
    text = finder.text(_ref(result, "result")) if result else None
    if text is None:
        return None
    return {"role": "tool", "tool_call_id": row["tool_call_id"], "content": text}


def _rows(finder: _Finder, operations: list[dict], requests: list[dict]) -> list[dict]:
    """Each committed native row: ``message`` as first sent, ``rewritten_to`` if a prune changed it.

    A row joins by its row ID first. Hermes gives a row ID only to rows committed since its last
    prune or restart, so a listing with none joins through its tool call ID: to the row that last
    held that ID, or by the ID alone, unless the row's own result says otherwise, because a call ID
    can come back on a new row. A row no request lists never reached a model, such as a turn's last
    reply or the text Hermes's tool guard ends a turn with: see ``_unlisted_message``.
    """
    first: dict[tuple[str, str], tuple[str, dict]] = {}
    last: dict[tuple[str, str], tuple[str, dict]] = {}
    row_of_call: dict[str, str] = {}
    for request in requests:
        body = request["request"] or {"messages": ()}
        for position, message in enumerate(body["messages"]):
            listed = (request["message_digests"][position], message)
            row = request["hermes_message_ids"].get(str(position))
            call = message.get("tool_call_id")
            if call and row:
                row_of_call[call] = row
            row = row or row_of_call.get(call)
            key = ("row", row) if row else ("call", call)
            if key[1]:
                first.setdefault(key, listed)
                last[key] = listed
    results = _results_of_rows(operations)
    rows = []
    for record in operations:
        if record["action"] != "message":
            continue
        row_id = record.get("hermes_message_id")
        entry = {**record, "hermes_message_id": row_id}
        call = record.get("tool_call_id")
        result = results.get(row_id) or results.get((_scope(record), call))
        key = ("row", row_id) if ("row", row_id) in first else ("call", call)
        if key[0] == "call" and key in first and result:
            if message_text(first[key][1]) != finder.text(_ref(result, "result")):
                key = None
        if key in first:
            entry["message"] = first[key][1]
            if last[key][0] != first[key][0]:
                entry["rewritten_to"] = last[key][1]
        else:
            entry["message"] = _unlisted_message(finder, record, result)
            if entry["message"] is None:
                finder.missing[f"row-{row_id}"] = [{"kind": "row_without_text", "row": row_id}]
        rows.append(entry)
    return rows


def rebuild_session(search: LogsSearch, session_id: str, workspace: str | None = None) -> dict:
    """The whole report. Raises ``AmbiguousSession`` when no one workspace can be chosen."""
    finder = _Finder(search, session_id, workspace)
    finder.find_session()
    owned = _order(record for record in finder.records.values() if finder.owned(record))
    operations = [record for record in owned if record["family"] == "operation"]
    contexts = [record for record in owned if record["family"] == "context"]
    finder.load(r["digest"] for record in operations for r in record.get("content_refs", ()))
    if not owned:
        finder.missing[session_id] = [{"kind": "no_records", "session_id": session_id}]

    places: dict[str, list] = defaultdict(list)
    for record in contexts:
        if "target_digest" in record:
            place = (record["source_type"], record["byte_offset"], record["byte_length"])
            places[record["target_digest"]].append(place)
    requests = _build_requests(finder, operations, places)
    unattached = _attach_tools(finder, operations, requests)
    rows = _rows(finder, operations, requests)
    attempted = {request.get("api_request_id") for request in requests}
    for record in operations:
        call = record.get("api_request_id")
        if (record["action"], record["stage"]) == ("model_request", "started"):
            if call not in attempted:
                finder.missing[call] = [{"kind": "call_with_no_attempt", "api_request_id": call}]
    events = [
        _with_text(finder, record)
        for record in operations
        if record["action"] not in ("tool", "message") and not _of_attempt(record)
    ]
    # No record says how these ended: a kill, a cancel (Hermes's interrupt fires no hook), or a
    # call still running. None of these is a loss, so they stay out of ``missing``.
    ends = {"response", "partial_responses", "failed", "unread"}
    unended = [request["model_attempt_id"] for request in requests if not request.keys() & ends]
    return {
        "session_id": session_id,
        "workspace_id": next(iter(finder.workspaces), None),
        "session_ids": sorted(finder.session_ids),
        "requests": requests,
        "unended_attempts": unended,
        "rows": rows,
        "unattached_tools": unattached,
        "context": contexts,
        "events": events,
        "missing": [problem for problems in finder.missing.values() for problem in problems],
    }
