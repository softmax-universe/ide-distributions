"""The Hermes runs API (``/p/<profile>/v1/runs``) as a small stdlib client, plus an in-memory fake.

The watcher starts, polls, steers and stops agent runs through the Hermes gateway's HTTP API
(read against Hermes v0.21.6, plan ``reads/6.md`` section B; v0.22.6's
``gateway/platforms/api_server_runs.py`` still has the responses and error codes below). This
module is deliberately free of third-party imports so it loads anywhere the watcher or a test does.

Facts the client relies on:

* every route lives under a profile prefix, ``/p/<profile>/…``, and is authorised with that
  profile's own ``API_SERVER_KEY`` as ``Authorization: Bearer <key>``; a profile without a key
  of its own uses the default (Orchestrator) profile's key (amendment A5.8);
* ``POST /v1/runs`` answers ``202 {"run_id", "status", "replayed"}``; an ``Idempotency-Key``
  reused with the same body replays the stored run, reused with a different body it answers
  ``409 idempotency_key_conflict`` (A5.1);
* the gateway answers ``429 rate_limit_exceeded`` when its concurrency cap is reached and
  ``503 gateway_draining`` while shutting down: both mean "try again next tick";
* run statuses are ``queued | running | waiting_for_approval | stopping`` and the terminal set
  :data:`TERMINAL`; ``steer`` is accepted only while ``running`` (409 otherwise); ``stop``
  answers ``stopping`` and the run then ends ``cancelled``;
* the gateway does not serialise runs per session, so callers must;
* session ids minted by the watcher use the shape Hermes mints itself, ``api_<epoch>_<8hex>``.

Retries are the caller's; the client only classifies failures (see the exception classes).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opentelemetry import trace

from research_core import inference
from research_core.platform.errors import PlatformError
from research_core.telemetry import span

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
DETAIL_LIMIT = 2000  # characters of an error body kept in an exception's detail


# --- results and errors ----------------------------------------------------------------------


@dataclass
class RunStatus:
    """One run's state as the gateway reports it. ``raw`` is the whole status payload."""

    run_id: str
    status: str
    session_id: str | None
    output: str | None
    error: str | None
    terminal: bool
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict, run_id: str = "") -> RunStatus:
        status = str(payload.get("status") or "")
        error = payload.get("error")
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error)
        return cls(
            run_id=str(payload.get("run_id") or run_id),
            status=status,
            session_id=payload.get("session_id"),
            output=payload.get("output"),
            error=None if error is None else str(error),
            terminal=status in TERMINAL,
            raw=payload,
        )

    @classmethod
    def unknown(cls, run_id: str, detail: str = "", raw: dict | None = None) -> RunStatus:
        """A run absent from this gateway is interrupted, never reported as completed."""
        return cls(run_id, "unknown", None, None, detail or None, True, raw or {})


class RunsError(PlatformError):
    """A runs API call failed. ``status`` is the HTTP status, ``code`` the body's error code."""

    def __init__(self, detail: str, *, code: str = "", **kwargs):
        super().__init__(detail, **kwargs)
        self.code = code

    def to_dict(self) -> dict:
        return {**super().to_dict(), "code": self.code}


class RunsBusy(RunsError):
    """The gateway cannot take the run now (429 concurrency cap); retry next tick."""

    def __init__(self, detail: str, **kwargs):
        kwargs.setdefault("retryable", True)
        super().__init__(detail, **kwargs)


class RunsUnavailable(RunsBusy):
    """No gateway answered (connection refused, timeout), it is draining (503), or the inference
    gate is closed (``code="inference_unavailable"``: no lease, or a block in the inference state).

    A special case of :class:`RunsBusy`: ``except RunsBusy`` covers both, and code that cares
    whether the gateway is reachable at all can catch this one first.
    """


class RunsNotSent(RunsUnavailable):
    """Connection setup failed before HTTP request bytes could reach the gateway."""


class RunsAuthError(RunsError):
    """The profile's API key was missing or refused (401/403); retrying cannot help."""

    def __init__(self, detail: str, **kwargs):
        kwargs.setdefault("retryable", False)
        super().__init__(detail, **kwargs)


class RunsConflict(RunsError):
    """An ``Idempotency-Key`` reused with a different body (409 ``idempotency_key_conflict``)."""

    def __init__(self, detail: str, **kwargs):
        kwargs.setdefault("retryable", False)
        super().__init__(detail, **kwargs)


# --- helpers ---------------------------------------------------------------------------------


def mint_session_id(prefix: str = "api") -> str:
    """A new session id in the shape Hermes mints for ``POST /api/sessions``.

    Hermes recognises ``api_<epoch>_<8hex>`` as one of its own session-id shapes (salvage
    classifies rows by it), so leave ``prefix`` at ``"api"`` for real sessions.
    """
    return f"{prefix}_{int(time.time())}_{secrets.token_hex(4)}"


def read_dotenv(path: Path) -> dict[str, str]:
    """``KEY=value`` pairs of a dotenv file; a missing file reads as empty.

    Tolerates blank lines, ``#`` comments, an ``export`` prefix and matching single or double
    quotes around the value (the forms Hermes and hosting write). Nothing is interpolated.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def _error_fields(payload: Any) -> tuple[str, str]:
    """``(message, code)`` of a gateway error body; ``error`` may be a dict or a bare string."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or ""), str(error.get("code") or "")
    if isinstance(error, str):
        return error, ""
    return "", ""


def _parse_body(data: bytes) -> Any:
    if not data:
        return {}
    try:
        return json.loads(data)
    except ValueError:
        return {"error": data.decode("utf-8", "replace")[:DETAIL_LIMIT]}


@dataclass
class _Response:
    status: int
    payload: Any


# --- the client ------------------------------------------------------------------------------


class HermesRuns:
    """The runs API of one Hermes gateway, with a key per profile.

    ``keys`` maps a profile name to its ``API_SERVER_KEY``; a profile absent from it uses
    ``default_profile``'s key. ``opener`` is ``urllib.request.urlopen`` or a stand-in with the
    same ``(request, timeout=…)`` signature.
    """

    def __init__(
        self,
        *,
        base_url: str,
        keys: dict[str, str],
        default_profile: str = "orchestrator",
        timeout_s: float = 30,
        opener: Callable[..., Any] = urllib.request.urlopen,
        inference_lease_file: Path | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.keys = dict(keys)
        self.default_profile = default_profile
        self.timeout_s = timeout_s
        self._opener = opener
        self.inference_lease_file = inference_lease_file

    @classmethod
    def from_profile(
        cls, profile_home: Path, *, researcher_profiles: Iterable[str] = ()
    ) -> HermesRuns:
        """The client for the gateway described by ``<profile_home>/.env``.

        ``API_SERVER_HOST`` and ``API_SERVER_PORT`` default to the gateway's own defaults. Each
        researcher profile's key is read from ``<profile_home>/../<profile>/.env``; a profile
        without one falls back to this profile's key at request time (A5.8).
        """
        env = read_dotenv(profile_home / ".env")
        host = env.get("API_SERVER_HOST") or DEFAULT_HOST
        port = env.get("API_SERVER_PORT") or DEFAULT_PORT
        keys = {profile_home.name: env.get("API_SERVER_KEY", "")}
        for profile in researcher_profiles:
            sibling_env = read_dotenv(profile_home.parent / profile / ".env")
            keys[profile] = sibling_env.get("API_SERVER_KEY", "")
        return cls(
            base_url=f"http://{host}:{port}",
            keys={name: key for name, key in keys.items() if key},
            default_profile=profile_home.name,
            inference_lease_file=Path(env["IDE_INFERENCE_LEASE_FILE"])
            if "IDE_INFERENCE_LEASE_FILE" in env
            else None,
        )

    def require_inference(self) -> None:
        """Raise :class:`RunsUnavailable` while the lease is missing or the inference state
        blocks model work; callers defer as for a busy gateway."""
        if inference.available(self.inference_lease_file):
            return
        blocking = inference.blocking_state(self.inference_lease_file)
        detail = (
            f"Inference blocked: {blocking.blocked} until {blocking.until.isoformat()}"
            if blocking is not None and blocking.until is not None
            else "Inference lease unavailable"
        )
        raise RunsUnavailable(detail, code="inference_unavailable")

    def key_for(self, profile: str) -> str:
        """The bearer key used for ``profile``: its own, else the default profile's."""
        return self.keys.get(profile) or self.keys.get(self.default_profile) or ""

    # -- the API ------------------------------------------------------------------------------

    def auth_scope(self, profile: str) -> str:
        return hashlib.sha256(f"{profile}\0{self.key_for(profile)}".encode()).hexdigest()

    def capabilities(self, profile: str) -> dict:
        return self._request("GET", profile, "/v1/capabilities").payload["features"][
            "runs_idempotency"
        ]

    def start(
        self,
        *,
        profile: str,
        session_id: str,
        text: str | list[dict],
        instructions: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """``POST /p/<profile>/v1/runs``; the run id (a replayed key gives the stored run's id).

        Raises :class:`RunsBusy` (429), :class:`RunsUnavailable` (503, no gateway, or the
        inference gate closed: no lease, or a block in the inference state),
        :class:`RunsAuthError` (401/403) and :class:`RunsConflict` (409).
        """
        self.require_inference()
        body: dict[str, Any] = {"input": text, "session_id": session_id}
        if instructions is not None:
            body["instructions"] = instructions
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        response = self._request("POST", profile, "/v1/runs", body=body, headers=headers)
        run_id = response.payload.get("run_id") if isinstance(response.payload, dict) else None
        if not run_id:
            # A successful response may follow acceptance. Recover its receipt with the
            # original durable key rather than authorizing a replacement execution.
            raise RunsError(
                f"run started but no run_id in the response: {response.payload!r}"[:DETAIL_LIMIT],
                route=f"POST /p/{profile}/v1/runs",
                status=response.status,
                retryable=True,
            )
        return str(run_id)

    def status(self, run_id: str, *, profile: str) -> RunStatus:
        """``GET /p/<profile>/v1/runs/<id>``; a 404 is an ``unknown`` (terminal) status."""
        response = self._request("GET", profile, f"/v1/runs/{run_id}", accept=(404,))
        if response.status == 404:
            return RunStatus.unknown(run_id, _error_fields(response.payload)[0], response.payload)
        return RunStatus.from_payload(response.payload, run_id)

    def steer(self, run_id: str, text: str, *, profile: str) -> bool:
        """``POST …/steer``; False when the run is not accepting steers (409), True when queued."""
        response = self._request(
            "POST", profile, f"/v1/runs/{run_id}/steer", body={"input": text}, accept=(409,)
        )
        return response.status != 409

    def approve_once(self, run_id: str, control: dict, *, profile: str) -> None:
        """``POST …/approval`` answering one pending approval ``once``. A 404 or 409 means the
        prompt or the run is already gone, which leaves nothing to answer."""
        self._request(
            "POST",
            profile,
            f"/v1/runs/{run_id}/approval",
            body={
                "request_id": control["prompt_id"],
                "execution_generation": control["execution_generation"],
                "choice": "once",
            },
            accept=(404, 409),
        )

    def stop(self, run_id: str, *, profile: str) -> RunStatus:
        """``POST …/stop``; the status after the request.

        A live run answers ``stopping`` (it ends ``cancelled`` shortly after), a terminal run its
        unchanged status. When the gateway no longer holds the run (409 ``run_not_active``,
        for instance after a restart) the current status is fetched instead.
        """
        response = self._request("POST", profile, f"/v1/runs/{run_id}/stop", accept=(409,))
        if response.status == 409:
            return self.status(run_id, profile=profile)
        return RunStatus.from_payload(response.payload, run_id)

    def session_messages(self, session_id: str, *, profile: str) -> list[dict]:
        """``GET /p/<profile>/api/sessions/<id>/messages``; ``[]`` for a session Hermes lacks."""
        response = self._request(
            "GET", profile, f"/api/sessions/{session_id}/messages", accept=(404,)
        )
        payload = response.payload
        if response.status == 404:
            return []
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        # Hermes answers ``{"object": ..., "data": [messages], "pagination": {...}}``.
        for key in ("data", "messages"):
            messages = payload.get(key)
            if isinstance(messages, list):
                return messages
        return []

    @span("hermes.session.create")
    def create_session(self, session_id: str, *, profile: str) -> None:
        """Create the native session before any run; replay keeps the existing transcript."""
        trace.get_current_span().set_attributes({"session.id": session_id, "role": profile})
        response = self._request(
            "POST", profile, "/api/sessions", body={"id": session_id}, accept=(409,)
        )
        if response.status == 409 and _error_fields(response.payload)[1] != "session_exists":
            raise self._error_for(f"POST /p/{profile}/api/sessions", response)

    # -- transport ----------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        profile: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        accept: tuple[int, ...] = (),
    ) -> _Response:
        """One HTTP exchange with ``/p/<profile><path>``.

        A 2xx answer, or an error status listed in ``accept``, is returned for the caller to
        read; every other status raises the matching :class:`RunsError` subclass. Transport
        failures raise :class:`RunsUnavailable`.
        """
        route = f"{method} /p/{profile}{path}"
        key = self.key_for(profile)
        if not key:
            raise RunsAuthError(
                f"no API_SERVER_KEY for profile {profile!r} or {self.default_profile!r}",
                route=route,
            )
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/p/{profile}{path}", data=data, method=method
        )
        request.add_header("Authorization", f"Bearer {key}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                return _Response(response.status, _parse_body(response.read()))
        except urllib.error.HTTPError as error:
            response = _Response(error.code, _parse_body(error.read()))
        except OSError as error:  # URLError (connection refused), timeouts, resets
            detail = f"{type(error).__name__}: {error}".strip(": ")
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            error_type = (
                RunsNotSent
                if isinstance(cause, (ConnectionRefusedError, socket.gaierror))
                else RunsUnavailable
            )
            raise error_type(f"gateway unreachable: {detail}", route=route) from error
        if response.status in accept:
            return response
        raise self._error_for(route, response)

    @staticmethod
    def _error_for(route: str, response: _Response) -> RunsError:
        message, code = _error_fields(response.payload)
        detail = f"HTTP {response.status} {code or ''} {message}".strip()[:DETAIL_LIMIT]
        kwargs: dict[str, Any] = {"route": route, "status": response.status, "code": code}
        if response.status == 429:
            return RunsBusy(detail, **kwargs)
        if response.status == 503:
            return RunsUnavailable(detail, **kwargs)
        if response.status in (401, 403):
            return RunsAuthError(detail, **kwargs)
        if response.status == 409:
            return RunsConflict(detail, **kwargs)
        return RunsError(detail, retryable=response.status >= 500, **kwargs)


# --- the fake --------------------------------------------------------------------------------


@dataclass
class FakeRun:
    """One run held by :class:`FakeHermesRuns`; tests and ``script`` hooks may edit it."""

    run_id: str
    profile: str
    session_id: str
    input: str
    instructions: str | None
    status: str = "running"
    output: str | None = None
    error: str | None = None
    idempotency_key: str | None = None
    turn_exit_reason: str | None = None
    pending_controls: list[dict] = field(default_factory=list)

    def to_status(self) -> RunStatus:
        raw = {
            "object": "hermes.run",
            "run_id": self.run_id,
            "status": self.status,
            "session_id": self.session_id,
        }
        if self.output is not None:
            raw["output"] = self.output
        if self.error is not None:
            raw["error"] = self.error
        if self.turn_exit_reason is not None:
            raw["turn_exit_reason"] = self.turn_exit_reason
        if self.pending_controls:
            raw["status"] = "waiting_for_approval"
            raw["pending_controls"] = self.pending_controls
        return RunStatus.from_payload(raw)


class FakeHermesRuns(HermesRuns):
    """An in-memory gateway with the same methods, for watcher tests.

    A started run is ``running`` until a test (or the ``script`` hook, called with the new
    :class:`FakeRun` at every start) calls :meth:`complete`, :meth:`fail` or :meth:`crash`.
    ``stop`` answers ``stopping`` like the real gateway and the run reads ``cancelled`` from the
    next status call on. Every call is appended to ``calls`` as ``(method, kwargs)``; steers go
    to ``steered``; ``sessions`` holds each session's messages (user text at start and steer,
    the assistant's output at completion). With ``inference_lease_file`` set, ``start`` passes
    the same inference gate as the real client.
    """

    def __init__(
        self,
        *,
        script: Callable[[FakeRun], None] | None = None,
        default_profile: str = "orchestrator",
        inference_lease_file: Path | None = None,
    ):
        super().__init__(
            base_url="http://fake-hermes.invalid",
            keys={},
            default_profile=default_profile,
            inference_lease_file=inference_lease_file,
        )
        self.script = script
        self.runs: dict[str, FakeRun] = {}
        self.sessions: dict[str, list[dict]] = {}
        self.steered: dict[str, list[str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.busy = False
        # Idempotency-Key → (body fingerprint, run id), as the gateway's 24-hour store.
        self._idempotency: dict[str, tuple[tuple, str]] = {}

    def capabilities(self, profile: str) -> dict:
        return {"supported": True, "durable": True, "retention_seconds": 86400}

    # -- test controls ------------------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        """While busy, ``start`` raises :class:`RunsBusy` as a gateway at its cap does."""
        self.busy = busy

    def complete(self, run_id: str, output: str = "") -> None:
        run = self.runs[run_id]
        run.status, run.output = "completed", output
        self.sessions.setdefault(run.session_id, []).append(
            {"role": "assistant", "content": output}
        )

    def fail(self, run_id: str, error: str, *, turn_exit_reason: str | None = None) -> None:
        """The run failed; ``turn_exit_reason`` rides on its status as Hermes's does."""
        run = self.runs[run_id]
        run.status, run.error, run.turn_exit_reason = "failed", error, turn_exit_reason

    def crash(self, run_id: str) -> None:
        """The gateway died under the run: Hermes reports such runs as ``interrupted``."""
        run = self.runs[run_id]
        run.status, run.error = "interrupted", "gateway restarted"

    # -- the API ------------------------------------------------------------------------------

    def start(
        self,
        *,
        profile: str,
        session_id: str,
        text: str | list[dict],
        instructions: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        self.calls.append(
            (
                "start",
                {
                    "profile": profile,
                    "session_id": session_id,
                    "text": text,
                    "instructions": instructions,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        self.require_inference()
        if self.busy:
            raise RunsBusy(
                "HTTP 429 rate_limit_exceeded", route=f"POST /p/{profile}/v1/runs", status=429
            )
        fingerprint = (profile, session_id, text, instructions)
        if idempotency_key is not None and idempotency_key in self._idempotency:
            stored_fingerprint, stored_run_id = self._idempotency[idempotency_key]
            if stored_fingerprint == fingerprint:
                return stored_run_id
            raise RunsConflict(
                f"HTTP 409 idempotency_key_conflict: key {idempotency_key!r} reused",
                route=f"POST /p/{profile}/v1/runs",
                status=409,
                code="idempotency_key_conflict",
            )
        run = FakeRun(
            run_id=f"run_{secrets.token_hex(16)}",
            profile=profile,
            session_id=session_id,
            input=text if isinstance(text, str) else text[-1]["content"],
            instructions=instructions,
            idempotency_key=idempotency_key,
        )
        self.runs[run.run_id] = run
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = (fingerprint, run.run_id)
        self.sessions.setdefault(session_id, []).append(
            {
                "role": "user",
                "content": text if isinstance(text, str) else text[-1]["content"],
            }
        )
        if self.script is not None:
            self.script(run)
        return run.run_id

    def status(self, run_id: str, *, profile: str) -> RunStatus:
        self.calls.append(("status", {"run_id": run_id, "profile": profile}))
        return self._status(run_id)

    def steer(self, run_id: str, text: str, *, profile: str) -> bool:
        self.calls.append(("steer", {"run_id": run_id, "text": text, "profile": profile}))
        run = self.runs.get(run_id)
        if run is None or run.status != "running":
            return False
        self.steered.setdefault(run_id, []).append(text)
        self.sessions.setdefault(run.session_id, []).append({"role": "user", "content": text})
        return True

    def stop(self, run_id: str, *, profile: str) -> RunStatus:
        self.calls.append(("stop", {"run_id": run_id, "profile": profile}))
        run = self.runs.get(run_id)
        if run is not None and run.status not in TERMINAL and run.status != "stopping":
            run.status = "stopping"
            return run.to_status()
        return self._status(run_id)

    def approve_once(self, run_id: str, control: dict, *, profile: str) -> None:
        self.calls.append(
            (
                "approve_once",
                {"run_id": run_id, "prompt_id": control["prompt_id"], "profile": profile},
            )
        )
        run = self.runs[run_id]
        run.pending_controls = [
            pending
            for pending in run.pending_controls
            if pending["prompt_id"] != control["prompt_id"]
        ]

    def session_messages(self, session_id: str, *, profile: str) -> list[dict]:
        self.calls.append(("session_messages", {"session_id": session_id, "profile": profile}))
        return [dict(message) for message in self.sessions.get(session_id, [])]

    def create_session(self, session_id: str, *, profile: str) -> None:
        self.calls.append(("create_session", {"session_id": session_id, "profile": profile}))
        self.sessions.setdefault(session_id, [])

    def _status(self, run_id: str) -> RunStatus:
        run = self.runs.get(run_id)
        if run is None:
            return RunStatus.unknown(run_id, f"Run not found: {run_id}")
        if run.status == "stopping":  # the stop requested earlier has taken effect by now
            run.status = "cancelled"
        return run.to_status()
