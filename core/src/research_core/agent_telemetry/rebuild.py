"""``research telemetry rebuild``: one Session rebuilt from Datadog's Logs search API alone.

It uses no state.db, volume or workspace API. ``CONTRACT.md`` beside this file defines the lines,
the bodies and the joins. ``docs/agent-telemetry.md`` says how to run it and what it cannot show.
"""

from __future__ import annotations

import base64
import email.message
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from itertools import groupby
from operator import itemgetter
from typing import Any

from research_core.agent_telemetry.encoder import message_text
from research_core.agent_telemetry.records import EMPTY_DIGEST, format_time

SEARCH_PATH = "/api/v2/logs/events/search"
# The token broker's ``POST /proxy`` refuses an upstream answer over 2,097,152 bytes. A row Datadog
# returns is the line's fields plus about 2,000 bytes of envelope (1,937 to 2,057 measured on
# 2026-11-13), and Datadog writes ``&``, ``<`` and ``>`` as six-byte escapes. A 4,096-byte line with
# 3,000 escaped characters and 2,057 bytes of envelope is a 21,153-byte row, and 99 such rows are
# 2,094,147 bytes.
PROXY_PAGE_LIMIT = 99
# 1,500 digests make a 105,000-character query. On 2026-11-12 Datadog answered searches of 1,500,
# 2,000, 3,000 and 5,000 digest terms (up to 350,000 characters) with 200 and status ``done``. Those
# matched no rows, so 1,500 leaves room for a search that does.
VALUES_PER_SEARCH = 1500
# Agent content stays in the ``main`` index for 15 days, so nothing older can be there.
SEARCH_WINDOW_S = 15 * 24 * 3600


class AmbiguousSession(Exception):
    """More than one workspace has records under this Session ID."""


class IncompleteSearch(Exception):
    """Datadog answered a search with a status other than ``done``, such as ``timeout``, or with
    warnings, so what it returned may be partial."""


class BrokerLogsSearch:
    """Logs search over the Standard indexes, through the Softmax token broker's ``POST /proxy``,
    which adds the ``datadog.content.read`` keys itself and never hands them out. Metta's
    ``docs/specs/0156-broker-http-proxy.md`` defines the request and the answer. It waits out a
    429 for as long as Datadog says.

    The time window is fixed when this is made, one per rebuild, and every search and page uses
    it: Datadog warns that a relative window such as ``now-15d`` can miss results across pages.
    """

    def __init__(
        self,
        broker_url: str,
        token: str,
        reason: str,
        session_id: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        now = time.time()
        self._window = {"from": format_time(now - SEARCH_WINDOW_S), "to": format_time(now)}
        self._url = broker_url.rstrip("/") + "/proxy"
        # A hosted session's agent proxy adds the token to what it sends to the broker.
        self._headers = {"Content-Type": "application/json"}
        if token != "proxy-injected":
            self._headers["Authorization"] = f"Bearer {token}"
        self._request = {
            "scope": "datadog.content.read",
            "reason": reason,
            "session_id": session_id,
            "method": "POST",
            "path": SEARCH_PATH,
            "headers": {"Content-Type": "application/json"},
        }

    def _send(self, body: dict[str, Any]) -> dict[str, Any]:
        """One search. The broker answers Datadog's own error, a 429 too, as a 200 that names
        ``upstream_status``, and that raises the ``HTTPError`` Datadog would have."""
        payload = {
            **self._request,
            "body_b64": base64.b64encode(json.dumps(body).encode()).decode(),
        }
        request = urllib.request.Request(self._url, json.dumps(payload).encode(), self._headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            answer = json.load(response)
        data = base64.b64decode(answer["body_b64"])
        status = int(answer["upstream_status"])
        if status >= 400:
            headers = email.message.Message()
            for name, value in answer["headers"].items():
                headers[name] = value
            raise urllib.error.HTTPError(
                SEARCH_PATH,
                status,
                "Datadog's answer through the broker",
                headers,
                io.BytesIO(data),
            )
        return json.loads(data)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        while True:
            try:
                return self._send(body)
            except urllib.error.HTTPError as error:
                if error.code != 429:
                    raise
                self._sleep(max(1.0, float(error.headers.get("X-RateLimit-Reset") or 1)))

    def entries(self, query: str) -> Iterator[dict[str, Any]]:
        """Each log entry's own fields, which Datadog nests under ``attributes.attributes``."""
        filters = {"query": query, **self._window, "storage_tier": "indexes"}
        page: dict[str, Any] = {"limit": PROXY_PAGE_LIMIT}
        while True:
            answer = self._post({"filter": filters, "page": page, "sort": "timestamp"})
            meta = answer.get("meta") or {}
            status = meta.get("status")
            if status not in (None, "done"):
                raise IncompleteSearch(f"the search answered status {status!r}")
            # Datadog warns, even with status ``done``, when the results may be partial.
            if warnings := meta.get("warnings"):
                titles = "; ".join(str(w.get("title") or w.get("code")) for w in warnings)
                raise IncompleteSearch(f"the search answered with warnings: {titles}")
            for row in answer.get("data") or ():
                yield row["attributes"]["attributes"]
            page["cursor"] = (meta.get("page") or {}).get("after")
            if not page["cursor"]:
                return


def _quoted(value: str) -> str:
    """A phrase in Datadog search syntax: a backslash escapes ``\\`` and ``"`` inside quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _any_of(field: str, values: Iterable[str]) -> str:
    return f"@{field}:(" + " OR ".join(_quoted(value) for value in values) + ")"


def _batches(values: Iterable[Any]) -> Iterator[list[Any]]:
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

    def __init__(self, search: BrokerLogsSearch, session_id: str, workspace: str | None) -> None:
        self.search = search
        self.session_ids = {session_id}
        self.workspaces = {workspace} if workspace else set()
        self.records: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[int, dict]] = defaultdict(dict)
        self.objects: dict[str, bytes | None] = {EMPTY_DIGEST: b""}
        self.parsed: dict[str, Any] = {}
        self.missing: dict[str, list[dict[str, Any]]] = {}

    def _find(self, condition: str) -> None:
        scope = _any_of("workspace_id", sorted(self.workspaces)) if self.workspaces else ""
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
                # After a compaction in place a record names the first ID and the live one. Each
                # leads to the other, so a lost compaction record cannot hide part of the Session.
                else:
                    aliases = {record.get("session_id"), record.get("hermes_session_id")} - {None}
                    if aliases & self.session_ids:
                        self.session_ids |= aliases
        # A retried request names only its admission, and a store event or a workspace tool's
        # record only the trace of the message it served. A record written before records named
        # their trace (retained for 15 days) names its run instead. A record with a Session ID
        # was found above if it is this Session's, and ``owned`` drops it otherwise. A record
        # found by one link can name another, so the searches repeat until none finds a new value.
        # Each round is one search of all three fields, batched by the number of values.
        links = ("admission_id", "otel_trace_id", "run_id")
        linked: set[tuple[str, str]] = set()
        while True:
            owned = [r for r in self.records.values() if self.owned(r) and not self._caused(r)]
            new = {(f, r[f]) for r in owned for f in links if f in r} - linked
            if not new:
                return
            linked |= new
            for batch in _batches(new):
                terms = [
                    _any_of(field, [value for _, value in pairs])
                    for field, pairs in groupby(batch, key=itemgetter(0))
                ]
                self._find("(" + " OR ".join(terms) + ") -@session_id:*")

    def _caused(self, record: dict[str, Any]) -> bool:
        """A store event another Session caused on this one, such as ``task.assigned``. It is this
        Session's, but its trace, run and admission are the other Session's, so no link follows it.
        """
        actor = record.get("reported_actor") or ""
        return (
            record.get("action") == "store_event"
            and actor.startswith("session:")
            and actor.removeprefix("session:") not in self.session_ids
        )

    def owned(self, record: dict[str, Any]) -> bool:
        """A record with another Session's ID came here by a link, and is not this Session's."""
        session = record.get("session_id") or record.get("hermes_session_id")
        return session is None or session in self.session_ids

    def load(self, references: Iterable[tuple[str, str]]) -> None:
        """Find each object named, wherever it was first stored, and all that those name in turn.

        ``references`` are ``(digest, name)``. The name says whether an object refers to others,
        not the kind it was stored as: the same bytes can be stored first as a tool's text.
        """
        frontier = {
            (digest, "manifest" if name == "request_manifest" else None)
            for digest, name in references
        }
        seen: set[tuple[str, str | None]] = set()
        while frontier:
            seen |= frontier
            wanted = {digest for digest, _ in frontier} - self.objects.keys()
            for batch in _batches(wanted):
                self._find(_any_of("content_digest", batch))
            for digest in wanted:
                self.read(digest)
            frontier = {
                named for digest, shape in frontier for named in self._names(digest, shape)
            } - seen

    def read(self, digest: str) -> bytes | None:
        if digest not in self.objects:
            data, problems = None, [{"kind": "no_content", "digest": digest}]
            # Taken out, so that a joined object is not also held as its chunks.
            if chunks := self.chunks.pop(digest, None):
                data, problems = _join_object(digest, chunks)
            self.objects[digest] = data
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

    def _names(self, digest: str, shape: str | None) -> list[tuple[str, str | None]]:
        """What a manifest or tool list refers to, and the shape of each. Anything else, such as a
        message, a text or a request's options, refers to nothing."""
        body = self.json(digest) if shape else None
        if not isinstance(body, dict):
            return []
        if shape == "tool_list":
            return [(tool, None) for tool in body.get("tools", ())]
        named: list[tuple[str, str | None]] = []
        if "parent_manifest_digest" in body:
            named.append((body["parent_manifest_digest"], "manifest"))
        if "tool_list_digest" in body:
            named.append((body["tool_list_digest"], "tool_list"))
        leaves = ("prompt_sections_digest", "options_digest")
        named += [(body[name], None) for name in leaves if name in body]
        return named + [(message, None) for message in body.get("messages", ())]


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
        chain.append(link)
        if link["operation"] != "append":
            break
        digest = link["parent_manifest_digest"]
    if link is None:
        return {"request": None, "blocked_by": [digest]}
    chain.reverse()
    manifest = chain[-1]
    digests = [listed for part in chain for listed in part["messages"]]
    tool_list = finder.json(manifest.get("tool_list_digest"))
    tools = (tool_list or {}).get("tools", [])
    others = [manifest.get("options_digest"), manifest.get("tool_list_digest"), *tools]
    blocked = {d for d in [*digests, *others] if d and finder.read(d) is None}
    if blocked:
        return {"request": None, "blocked_by": sorted(blocked)}
    if len(digests) != manifest["message_count"]:
        finder.missing[manifest_digest] = [
            {
                "kind": "manifest_mismatch",
                "digest": manifest_digest,
                "message_count": manifest["message_count"],
                "messages_listed": len(digests),
            }
        ]
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
    for position, message in enumerate(request["messages"]):
        text = message_text(message) or ""
        role = message.get("role")
        if role == "system":
            # The sections point into the first message alone, the one the manifest describes.
            spans = [
                (section["name"], section["byte_offset"], section["byte_length"])
                for section in (sections.get("sections", ()) if position == 0 else ())
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
    total = sum(chars.values())
    rows = []
    for source, count in sorted(chars.items(), key=lambda item: -item[1]):
        rows.append({"source": source, "chars": count})
        if prompt_tokens is not None:
            rows[-1]["estimated_tokens"] = round(prompt_tokens * count / total)
    return rows


def _order(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """By time across all processes, so ``research serve``'s records interleave with the
    gateway's that run beside it; ``sequence`` breaks a tie inside one process.

    A record carries the time of what it describes, not of its writing (a request its start, a
    reply its end, a committed row its own timestamp, read after the turn's end), so ``sequence``
    alone is not wire order. Requests come from one gateway process at a time, so their order is
    their time order.
    """
    return sorted(records, key=lambda r: (r["occurred_at"], r["source_instance"], r["sequence"]))


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


# What ends an attempt. A partial reply does not: Hermes can retry the stream inside the attempt.
_ATTEMPT_ENDS = ("response", "failed", "unread")


def _build_requests(
    finder: _Finder, operations: list[dict], places: dict
) -> tuple[list[dict], list[str]]:
    """Each request, and the attempt IDs of those no record says the end of."""
    replies: dict[str, dict] = defaultdict(dict)
    # What a broken stream had shown, which Hermes can retry inside the same attempt.
    partials: dict[str, list] = defaultdict(list)
    for record in filter(_of_attempt, operations):
        if record.get("partial"):
            partials[record["model_attempt_id"]].append(record)
        else:
            replies[record["model_attempt_id"]][record["stage"]] = record
    requests = []
    unended: list[str] = []
    attempts = [r for r in operations if r["action"] == "model_attempt" and r["stage"] == "request"]
    for position, attempt in enumerate(attempts, start=1):
        entry = {"position": position, **attempt, "tools": []}
        manifest_digest = _ref(attempt, "request_manifest")
        entry["manifest_digest"] = manifest_digest
        if manifest_digest is None:
            attempt_id = attempt["model_attempt_id"]
            problem = {"kind": "no_request_manifest", "model_attempt_id": attempt_id}
            finder.missing[attempt_id] = [problem]
            entry.update({"request": None, "blocked_by": []})
        else:
            entry.update(_rebuild_request(finder, manifest_digest))
        stages = replies[attempt["model_attempt_id"]]
        if not stages.keys() & _ATTEMPT_ENDS:
            unended.append(attempt["model_attempt_id"])
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
    return requests, unended


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
    """A row no request lists. A tool row takes the result recorded for it, which is the text
    Hermes passed on and can differ from the row's stored text; its commit's text stands in only
    when there is no such result. Any other row takes its commit's text. ``None`` when that text
    is lost or was never recorded."""
    text = finder.text(_ref(result, "result")) if result else None
    if text is not None:
        return {"role": "tool", "tool_call_id": row["tool_call_id"], "content": text}
    own = _ref(row, "message")
    return finder.json(own) if own else None


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
        # An auxiliary call can carry the conversation's tool messages, capped, with no row IDs.
        if request["model_attempt_id"].startswith("aux-"):
            continue
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
        if key[0] == "call" and key in first:
            # A call ID can come back on a new row. The row's own result says which it is, or,
            # with none recorded, as at a guard halt, the text its commit holds.
            listed_text = message_text(first[key][1])
            if result:
                text = finder.text(_ref(result, "result"))
            else:
                own = finder.json(_ref(record, "message"))
                text = listed_text if own is None else message_text(own)
            if listed_text != text:
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


def rebuild_session(
    search: BrokerLogsSearch, session_id: str, workspace: str | None = None
) -> dict:
    """The whole report. Raises ``AmbiguousSession`` when no one workspace can be chosen."""
    finder = _Finder(search, session_id, workspace)
    finder.find_session()
    owned = _order(record for record in finder.records.values() if finder.owned(record))
    operations = [record for record in owned if record["family"] == "operation"]
    contexts = [record for record in owned if record["family"] == "context"]
    finder.load(
        (ref["digest"], ref["name"])
        for record in operations
        for ref in record.get("content_refs", ())
    )
    if not owned:
        finder.missing[session_id] = [{"kind": "no_records", "session_id": session_id}]

    places: dict[str, list] = defaultdict(list)
    for record in contexts:
        if "target_digest" in record:
            place = (record["source_type"], record["byte_offset"], record["byte_length"])
            places[record["target_digest"]].append(place)
    requests, unended = _build_requests(finder, operations, places)
    unattached = _attach_tools(finder, operations, requests)
    rows = _rows(finder, operations, requests)
    # Hermes retries a call under its one ``api_request_id`` and starts each attempt with its own
    # ``model_request`` / ``started``, so a call is short when it has fewer attempts than starts.
    started = Counter(
        record.get("api_request_id")
        for record in operations
        if (record["action"], record["stage"]) == ("model_request", "started")
    )
    attempted = Counter(request.get("api_request_id") for request in requests)
    for call, starts in started.items():
        if attempted[call] < starts:
            finder.missing[call] = [{"kind": "call_with_no_attempt", "api_request_id": call}]
    # An attempt's records are under its request. Those of a lost request stay among the events,
    # so what survives of them is printed.
    in_requests = {request["model_attempt_id"] for request in requests}
    events = [
        _with_text(finder, record)
        for record in operations
        if record["action"] not in ("tool", "message")
        and not (_of_attempt(record) and record["model_attempt_id"] in in_requests)
    ]
    # ``unended``: no record says how these ended, as after a kill, a cancel (Hermes's interrupt
    # fires no hook), a call still running, or a reply record that a full queue dropped or that was
    # lost on the way. Nothing tells these apart, so they stay out of ``missing``.
    return {
        "session_id": session_id,
        # None when no record is this Session's, even for a workspace named with ``--workspace``.
        "workspace_id": next(iter(finder.workspaces), None) if owned else None,
        "session_ids": sorted(finder.session_ids),
        "requests": requests,
        "unended_attempts": unended,
        "rows": rows,
        "unattached_tools": unattached,
        "context": contexts,
        "events": events,
        "missing": [problem for problems in finder.missing.values() for problem in problems],
    }
