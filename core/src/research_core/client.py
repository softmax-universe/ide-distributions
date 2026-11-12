"""The scripted client of the seam (plan Task 6.3): drives ``research serve`` from tests and demos.

One synchronous, stdlib-only client over the routes :mod:`research_core.serve` offers. It is the
reference for what a real client does on the seam: open a Session once, send each message with
one context block, read a snapshot and then the event stream from its ``seq``, and make user
edits through ``/store/apply``. Reading the stream is bounded here (frames stop arriving for
``idle`` seconds, or ``until`` matches) because a test needs to return; a real client keeps the
connection open.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from research_core.watcher.hermes_api import TERMINAL

DEFAULT_TIMEOUT_S = 30.0


class SeamError(Exception):
    """The server answered with an error status; ``body`` is its JSON, ``headers`` its headers."""

    def __init__(self, status: int, body: dict, headers: dict[str, str], route: str) -> None:
        self.status = status
        self.body = body
        self.headers = headers
        self.route = route
        super().__init__(f"{route} -> HTTP {status}: {body.get('error', body)}")


class ScriptedClient:
    """A client of one ``research serve`` (or bridge) at ``base_url``.

    ``actor`` is who the client acts for on writes (``user:<id>``); ``session_id`` is set by
    :meth:`open` and used by :meth:`say`.
    """

    def __init__(
        self, base_url: str, *, actor: str = "user:local", timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.actor = actor
        self.timeout_s = timeout_s
        self.session_id: str | None = None

    # --- the session ---------------------------------------------------------------------

    def open(
        self,
        client_id: str = "scripted",
        kind: str = "test",
        guide: str | None = None,
        session_context: dict | None = None,
        capabilities: dict | list | None = None,
    ) -> str:
        """``POST /session-open``; the new session id, kept for the other calls."""
        client: dict[str, Any] = {"id": client_id, "kind": kind}
        if guide is not None:
            client["guide"] = guide
        body: dict[str, Any] = {"client": client, "actor": self.actor}
        if session_context is not None:
            body["session_context"] = session_context
        if capabilities is not None:
            body["capabilities"] = capabilities
        self.session_id = self._post("/session-open", body)["session_id"]
        return self.session_id

    def say(self, text: str, context: dict | None = None, *, wait: bool = False) -> dict:
        """Confirm a queued message; optionally wait until its run is assigned."""
        body: dict[str, Any] = {
            "session_id": self._session(),
            "message": {"role": "user", "content": text},
            "actor": self.actor,
        }
        if context is not None:
            body["context"] = context
        receipt = self._post("/message", body)
        return receipt | self.wait_admission(receipt["op_id"]) if wait else receipt

    def run(self, run_id: str) -> dict:
        """``GET /runs/<id>``: the run's status as the gateway reports it."""
        return self._get(f"/runs/{urllib.parse.quote(run_id, safe='')}")

    def wait_admission(self, op_id: str, timeout: float = 30) -> dict:
        """Wait for a durable message to start, fail or be cancelled; timeout does not cancel it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for admission in self.snapshot("workspace")["records"]["admissions"]:
                if admission["op_id"] == op_id and (
                    admission["run_id"] or admission["status"] in TERMINAL
                ):
                    return admission
            time.sleep(0.1)
        raise TimeoutError(f"Message {op_id} is still queued")

    def wait_run(self, run_id: str, timeout: float = 120, poll: float = 0.2) -> dict:
        """Poll ``/runs/<id>`` until the run is terminal; ``TimeoutError`` after ``timeout`` s."""
        deadline = time.monotonic() + timeout
        while True:
            status = self.run(run_id)
            if status.get("terminal") or status.get("status") in TERMINAL:
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} still {status.get('status')} after {timeout} s")
            time.sleep(poll)

    # --- the stream and snapshots ---------------------------------------------------------

    def snapshot(self, store: str = "workspace", *, coworld: str | None = None) -> dict:
        return self._get("/snapshot", {"store": store, **({"coworld": coworld} if coworld else {})})

    def events(
        self,
        store: str = "workspace",
        after: int = 0,
        *,
        idle: float = 1.0,
        until: Callable[[dict], bool] | None = None,
        timeout: float | None = None,
        coworld: str | None = None,
    ) -> list[dict]:
        """Events after ``after`` from ``/stream``, read until the stream is idle for ``idle``
        seconds, ``until(event)`` is true, or ``timeout`` seconds passed. Each event carries
        ``store`` and its ``seq`` (the SSE id)."""
        url = f"{self.base_url}/stream?" + urllib.parse.urlencode(
            {"store": store, "after": after, **({"coworld": coworld} if coworld else {})}
        )
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        events: list[dict] = []
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            response = urllib.request.urlopen(request, timeout=idle)
        except urllib.error.HTTPError as error:
            raise self._error(error, f"GET /stream?store={store}") from None
        with response:
            frame: dict[str, str] = {}
            while deadline is None or time.monotonic() < deadline:
                try:
                    line = response.readline()
                except TimeoutError:
                    break
                if not line:
                    break  # the server closed the stream
                text = line.decode("utf-8").rstrip("\n")
                if text == "":
                    if "data" in frame:
                        event = json.loads(frame["data"])
                        if "id" in frame:
                            event.setdefault("seq", int(frame["id"]))
                        events.append(event)
                        if until is not None and until(event):
                            break
                    frame = {}
                elif not text.startswith(":"):
                    field, _, value = text.partition(":")
                    frame[field] = value[1:] if value.startswith(" ") else value
        return events

    def notices(self, after: int = 0, **options: Any) -> list[dict]:
        """The ``notice`` events of the workspace stream after ``after``."""
        return [e for e in self.events("workspace", after, **options) if e["kind"] == "notice"]

    # --- edits -------------------------------------------------------------------------

    def apply(
        self,
        actor: str,
        op: str,
        args: dict,
        store: str | None = None,
        *,
        coworld: str | None = None,
    ) -> dict:
        """``POST /store/apply``: one allowed library function as ``actor`` (``user:<id>``)."""
        body: dict[str, Any] = {"actor": actor, "op": op, "args": args, "coworld": coworld}
        if store is not None:
            body["store"] = store
        return self._post("/store/apply", body)

    def health(self) -> dict:
        return self._get("/health")

    def models(self) -> dict:
        """Each profile's model, provider and fallbacks (``GET /models``)."""
        return self._get("/models")

    def model_catalog(self) -> dict:
        """The models a picker may offer (``GET /models/catalog``)."""
        return self._get("/models/catalog")

    def set_model(
        self,
        profile: str,
        model: str,
        *,
        provider: str | None = None,
        fallbacks: list[dict | str] | None = None,
    ) -> dict:
        """Change one profile's model as this client's user (``POST /models``)."""
        body: dict = {"profile": profile, "model": model, "actor": self.actor}
        if provider is not None:
            body["provider"] = provider
        if fallbacks is not None:
            body["fallbacks"] = fallbacks
        return self._post("/models", body)

    # --- transport -----------------------------------------------------------------------

    def _session(self) -> str:
        if self.session_id is None:
            raise RuntimeError("no session: call open() first")
        return self.session_id

    def _post(self, path: str, body: dict) -> dict:
        if path in {"/session-open", "/message"}:
            body = {"op_id": str(uuid4()), **body}
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return self._send(request, f"POST {path}")

    def _get(self, path: str, query: dict[str, Any] | None = None) -> dict:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        return self._send(request, f"GET {path}")

    def _send(self, request: urllib.request.Request, route: str) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raise self._error(error, route) from None

    @staticmethod
    def _error(error: urllib.error.HTTPError, route: str) -> SeamError:
        raw = error.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"error": raw.decode("utf-8", "replace")}
        return SeamError(error.code, body, dict(error.headers.items()), route)
