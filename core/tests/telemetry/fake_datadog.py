"""The far end of the pipeline for tests: the Datadog Agent's handling of a line, and Logs search.

``FakeDatadog`` answers only the query forms the rebuild command uses, and refuses any other, so a
query that this fake understands by accident cannot pass. What it does with a line follows what the
real Agent and Logs did on 2026-11-05 (``docs/designs/datadog-agent-logging-build-scope.md``): a
line of JSON becomes its attributes, and anything else is text under the container's own service.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CONTAINER_SERVICE = "hermes-runtime"
# Reserved by Datadog's JSON preprocessing: taken off the line's attributes, so Logs search
# neither matches them as ``@trace_id`` nor returns them (seen on real data on 2026-11-12).
_RESERVED = ("trace_id", "span_id")
_TOKEN = re.compile(r'\s*(\(|\)|OR\b|service:[^\s()]+|@[a-z_]+:\(|@[a-z_]+:"[^"]*"|"[^"]*")')


def entry_of(line: bytes) -> dict[str, Any]:
    """What Datadog makes of one line of the stream."""
    try:
        parsed = json.loads(line)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return {key: value for key, value in parsed.items() if key not in _RESERVED}
    return {"service": CONTAINER_SERVICE, "message": line.decode("utf-8", "replace").strip()}


class _Query:
    """Implicit AND of terms. A term: ``service:x``, ``@f:"v"``, ``@f:("a" OR "b")``, or a group."""

    def __init__(self, text: str) -> None:
        self.tokens = self._tokenize(text)
        self.position = 0
        self.root = self._all_of(end=None)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        tokens = []
        position = 0
        while text[position:].strip():
            match = _TOKEN.match(text, position)
            if match is None:
                raise ValueError(f"query not understood at: {text[position : position + 60]}")
            tokens.append(match.group(1))
            position = match.end()
        return tokens

    def _next(self) -> str:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _all_of(self, end: str | None) -> Any:
        terms = []
        while self._peek() != end:
            terms.append(self._any_of(end))
        if end is not None:
            self._next()
        return lambda entry: all(term(entry) for term in terms)

    def _any_of(self, end: str | None) -> Any:
        options = [self._term()]
        while self._peek() == "OR":
            self._next()
            options.append(self._term())
        return lambda entry: any(option(entry) for option in options)

    def _term(self) -> Any:
        token = self._next()
        if token == "(":
            return self._all_of(")")
        if token.startswith("service:"):
            value = token.partition(":")[2]
            return lambda entry: entry.get("service") == value
        field, _, value = token[1:].partition(":")
        if value != "(":
            return lambda entry: entry.get(field) == value.strip('"')
        values = set()
        while (item := self._next()) != ")":
            if item != "OR":
                values.add(item.strip('"'))
        return lambda entry: entry.get(field) in values

    def matches(self, entry: dict[str, Any]) -> bool:
        return self.root(entry)


class FakeDatadog:
    """``POST /api/v2/logs/events/search``, holding what intake took.

    ``rate_limit_next`` answers that many searches with a 429, as Datadog words it. ``status`` is
    what each answer's ``meta.status`` says: ``timeout`` when Datadog ran out of time, and with
    ``None`` an answer has no status.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: list[dict[str, Any]] = []
        self.bytes_taken = 0
        self.searches: list[dict[str, Any]] = []
        self.rate_limit_next = 0
        self.rate_limited = 0
        self.status = "done"
        datadog = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, answer, headers = datadog.answer(self.path, self.headers, body)
                data = json.dumps(answer).encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        # Avoid a half-second shutdown poll in each function-scoped fixture.
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def take_lines(self, lines: Iterable[bytes]) -> None:
        """What the Agent collected from a container's stream."""
        lines = list(lines)
        with self.lock:
            self.bytes_taken += sum(len(line) for line in lines)
            self.entries.extend(entry_of(line) for line in lines)

    def take(self, entries: Iterable[dict[str, Any]]) -> None:
        """Entries already parsed, to change them or take them twice."""
        with self.lock:
            self.entries.extend(json.loads(json.dumps(entry)) for entry in entries)

    def remove_chunk(self, digest: str, chunk_index: int) -> None:
        with self.lock:
            self.entries = [
                entry
                for entry in self.entries
                if not (
                    entry.get("content_digest") == digest
                    and entry.get("chunk_index") == chunk_index
                )
            ]

    def answer(self, path: str, headers: Any, body: dict[str, Any]) -> tuple[int, Any, dict]:
        if path != "/api/v2/logs/events/search":
            return 404, {"errors": ["no such route"]}, {}
        if not headers.get("DD-API-KEY") or not headers.get("DD-APPLICATION-KEY"):
            return 403, {"errors": ["Forbidden"]}, {}
        with self.lock:
            self.searches.append(body)
            if self.rate_limit_next > 0:
                self.rate_limit_next -= 1
                self.rate_limited += 1
                return 429, {"errors": ["Too many requests"]}, {"X-RateLimit-Reset": "1"}
            entries = list(self.entries)
        limit = body["page"]["limit"]
        if limit > 1000 or body["filter"].get("storage_tier") != "indexes":
            return 400, {"errors": ["bad page limit or storage tier"]}, {}
        try:
            query = _Query(body["filter"]["query"])
        except (ValueError, IndexError) as error:
            return 400, {"errors": [str(error)]}, {}
        matched = [entry for entry in entries if query.matches(entry)]
        start = int(body["page"].get("cursor") or 0)
        page = matched[start : start + limit]
        after = start + limit
        meta: dict[str, Any] = {} if self.status is None else {"status": self.status}
        if after < len(matched):
            meta["page"] = {"after": str(after)}
        return 200, {"data": [self._row(entry) for entry in page], "meta": meta}, {}

    @staticmethod
    def _row(entry: dict[str, Any]) -> dict[str, Any]:
        """A parsed line has no ``message``. ``service`` moves above its fields, as a reserved name,
        and its tags still say the container's service.
        """
        fields = {k: v for k, v in entry.items() if k not in ("message", "service")}
        attributes = {
            "service": entry["service"],
            "status": "info",
            "tags": [f"service:{CONTAINER_SERVICE}", "source:hermes-runtime"],
            "timestamp": "2026-11-04T18:31:16.204Z",
            "attributes": fields,
        }
        if "message" in entry:
            attributes["message"] = entry["message"]
        return {"id": str(uuid.uuid4()), "type": "log", "attributes": attributes}
