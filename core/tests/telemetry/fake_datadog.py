"""The far end of the pipeline for tests: the Datadog Agent's handling of a line, and Logs search.

``FakeDatadog`` answers only the query forms the rebuild command uses, and refuses any other, so a
query that this fake understands by accident cannot pass. What it does with a line follows what the
real Agent and Logs did on 2026-11-05 (``docs/designs/datadog-agent-logging-build-scope.md``): a
line of JSON becomes its attributes, and anything else is text under the container's own service.
"""

from __future__ import annotations

import base64
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
# A phrase is in double quotes, where a backslash escapes the next character, as in Datadog.
_PHRASE = r'"(?:[^"\\]|\\.)*"'
_TOKEN = re.compile(
    rf"\s*(\(|\)|OR\b|service:[^\s()]+|-@[a-z_]+:\*|@[a-z_]+:\(|@[a-z_]+:{_PHRASE}|{_PHRASE})"
)


# An absolute search bound, as the rebuild writes it. The fake refuses a relative one, and records
# the bounds in ``searches``, but does not filter by them: tests write lines at fixed times.
_INSTANT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")


def _unquoted(phrase: str) -> str:
    return re.sub(r"\\(.)", r"\1", phrase[1:-1])


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
    """Implicit AND of terms. A term: ``service:x``, ``@f:"v"``, ``@f:("a" OR "b")``, ``-@f:*``,
    or a group."""

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
        if token.startswith("-@"):  # ``-@f:*``: the entry has no field ``f``.
            absent = token[2:].partition(":")[0]
            return lambda entry: absent not in entry
        field, _, value = token[1:].partition(":")
        if value != "(":
            return lambda entry: entry.get(field) == _unquoted(value)
        values = set()
        while (item := self._next()) != ")":
            if item != "OR":
                values.add(_unquoted(item))
        return lambda entry: entry.get(field) in values

    def matches(self, entry: dict[str, Any]) -> bool:
        return self.root(entry)


class FakeDatadog:
    """``POST /api/v2/logs/events/search``, holding what intake took.

    ``rate_limit_next`` answers that many searches with a 429, as Datadog words it. ``status`` is
    what each answer's ``meta.status`` says: ``timeout`` when Datadog ran out of time, and with
    ``None`` an answer has no status. ``warnings`` is what each answer's ``meta.warnings`` says, a
    list of ``{code, title, detail}``, and with ``None`` an answer has none.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: list[dict[str, Any]] = []
        self.bytes_taken = 0
        self.searches: list[dict[str, Any]] = []
        self.rate_limit_next = 0
        self.rate_limited = 0
        self.status = "done"
        self.warnings: list[dict[str, str]] | None = None
        self.brokers: list[ThreadingHTTPServer] = []
        # A hosted session's agent proxy adds the token to what goes to the broker.
        self.agent_proxy_adds_token = False
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
        for server in (self.server, *self.brokers):
            server.shutdown()
            server.server_close()

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
        if limit > 5000 or body["filter"].get("storage_tier") != "indexes":
            return 400, {"errors": ["bad page limit or storage tier"]}, {}
        window = (body["filter"].get("from"), body["filter"].get("to"))
        if not all(isinstance(bound, str) and _INSTANT.fullmatch(bound) for bound in window):
            return 400, {"errors": [f"this fake takes only absolute bounds: {window}"]}, {}
        try:
            query = _Query(body["filter"]["query"])
        except (ValueError, IndexError) as error:
            return 400, {"errors": [str(error)]}, {}
        matched = [entry for entry in entries if query.matches(entry)]
        start = int(body["page"].get("cursor") or 0)
        page = matched[start : start + limit]
        after = start + limit
        meta: dict[str, Any] = {} if self.status is None else {"status": self.status}
        if self.warnings is not None:
            meta["warnings"] = self.warnings
        if after < len(matched):
            meta["page"] = {"after": str(after)}
        return 200, {"data": [self._row(entry) for entry in page], "meta": meta}, {}

    def broker_url(self) -> str:
        """The URL of this search's token broker, started on first use."""
        if not self.brokers:
            self.serve_broker()
        host, port = self.brokers[0].server_address[:2]
        return f"http://{host}:{port}"

    def search(self, sleep: Any = None) -> Any:
        """The rebuild's search, through this search's token broker."""
        from research_core.agent_telemetry.rebuild import BrokerLogsSearch

        waits = {"sleep": sleep} if sleep else {}
        return BrokerLogsSearch(
            self.broker_url(), "tbk_test", "a test rebuild", "session-1", **waits
        )

    def serve_broker(self) -> str:
        """Start the token broker's ``POST /proxy`` in front of this search, as metta's spec 0156
        and the broker on 2026-11-13 define it, and give its URL. It takes the token ``tbk_test``
        and the scope ``datadog.content.read``, adds the keys, and refuses an answer over 2 MB."""
        datadog = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, answer = datadog._proxy(self.path, self.headers, request)
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        broker = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=broker.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        self.brokers.append(broker)
        return f"http://127.0.0.1:{broker.server_address[1]}"

    def _proxy(self, path: str, headers: Any, request: dict[str, Any]) -> tuple[int, Any]:
        if path != "/proxy":
            return 404, {"detail": "Not Found"}
        authorization = headers.get("Authorization")
        if authorization is None and self.agent_proxy_adds_token:
            authorization = "Bearer tbk_test"
        if authorization != "Bearer tbk_test":
            return 401, {"detail": "invalid token"}
        if request.get("scope") != "datadog.content.read" or request.get("method") != "POST":
            return 403, {"detail": "method_not_allowed_for_scope"}
        if not 5 <= len(request.get("reason") or "") <= 1000:
            return 422, {"detail": "reason"}
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", request.get("session_id") or ""):
            return 422, {"detail": "session_id"}
        keyed = {**request["headers"], "DD-API-KEY": "a", "DD-APPLICATION-KEY": "b"}
        body = json.loads(base64.b64decode(request["body_b64"]))
        status, answer, answer_headers = self.answer(request["path"], keyed, body)
        data = json.dumps(answer).encode()
        if len(data) > 2_097_152:
            return 502, {"detail": "upstream_response_too_large"}
        # Datadog's headers as the broker passes them on: lower case.
        answer_headers = {name.lower(): value for name, value in answer_headers.items()}
        return 200, {
            "request_id": "req_test",
            "status": "approved",
            "scope": request["scope"],
            "tier": "auto",
            "upstream_status": status,
            "headers": answer_headers,
            "body_b64": base64.b64encode(data).decode(),
            "duration_ms": 1,
        }

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
