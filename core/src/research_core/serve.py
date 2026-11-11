"""``research serve``: the local seam server (design §10, A.4, A.11; plan entry 6; A6.1).

Locally this is the bridge: a small stdlib HTTP server that gives a client the seam without
metta. It opens chat Sessions and durably queues each message for a Hermes run,
stamps the client and the active Coworld into the context block, streams each store's
``events`` rows as Server-Sent Events, serves snapshots, passes UI actions into the Session and
runs the user-edit allow-list. It is the reference for the bridge's per-session rule (A6.1):
one live run per Session; messages arriving during a run wait in the workspace inbox.

What the server stamps into a turn is only ``client``, ``coworld`` and ``contract_version``:
the profile's own turn hook (``plugin/turn.py``) adds the unread notices and the changed-since
note, so the server never duplicates them. ``ThreadingHTTPServer`` gives
each request its own thread, and each request opens its own store connections (SQLite
connections are per thread). A background dispatcher reconciles each persisted input using
Hermes's durable idempotency key. Its filesystem lock also fences the separate sleep command.
A chat run that fails is classified (``run_failures.py``, in the Coordinator's outcome
transition): a budget, rate-limit or provider failure holds the message, which the snapshot
reports as ``waiting``, and the dispatcher resumes it with a continuation prompt in the same
Session once its hold has passed and the inference gate is open; other failures end ``failed``
with an ``error_class`` beside Hermes's error (``research_core/contract/CONTRACT.md``, "Message
admissions").
Each resume holds longer than the last. A start the gateway refuses as retryable records
``retry_at`` and waits out ``recovery.delivery_backoff_seconds`` before the next try.
``GET /status``'s ``has_work`` counts neither a held or refused message, nor the messages queued
behind it, nor one that needs resolution, on purpose: a budget wait lasts until the daily
refill and must not keep the workspace awake; the message resumes once the workspace is awake.

The model routes (``GET /models``, ``GET /models/catalog``, ``POST /models``) show each
profile's model and change it on the user's request (``profile_models.py``); a change is a
``profile.model_updated`` event in the workspace store, so every attached client sees it.

Two conveniences sit beside the contract's routes: ``GET /`` (and ``/ui``) serves the client
page ``ui/index.html``, a self-contained page that exercises every route from a browser, and
``GET /status`` answers what ``research watcher status`` prints, read from the workspace store,
so the page can show the watcher's liveness without a shell.

The HTTP boundary also records each POST to ``RECORDED_ROUTES`` as metadata
(``SeamServer.record_request``).
``agent_telemetry/CONTRACT.md`` says what is recorded and how it joins.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from httpx import HTTPError
from opentelemetry import trace

from research_core import (
    attachments,
    contract,
    inference,
    intake,
    layout,
    profile_models,
    xp_followups,
)
from research_core.agent_telemetry import Producer, TelemetryWriter, observed, stream_path
from research_core.attachments import AttachmentError
from research_core.coordinator import Coordinator, ownership
from research_core.messages import UserMessage
from research_core.platform.errors import PlatformError
from research_core.prepare import Runner
from research_core.profile_models import Catalog, ModelError, Profile
from research_core.store import admissions, coworlds, sessions, settings, ui_state
from research_core.store import cli as store_cli
from research_core.store._common import parse_display_id, parse_iso
from research_core.store.actors import Actor
from research_core.store.db import Store, change_version, wait_for_change
from research_core.store.errors import NotFound, StoreError
from research_core.store.registry import GOAL, TASK, record_from_row
from research_core.telemetry import correlation, process_context, propagator, span
from research_core.tools.ui import coworld_conversation, pointer_token
from research_core.watcher import context, hermes_api
from research_core.watcher.heartbeat import HAS_WORK_REASONS_KEY, HEARTBEAT_KEY
from research_core.watcher.hermes_api import HermesRuns, RunsBusy, RunsError, RunsUnavailable
from research_core.watcher.tick import PAUSED_DRAINED_KEY

log = logging.getLogger(__name__)

LIVE_STATUSES = frozenset({"running", "queued"})
TAIL_INTERVAL_S = 0.5  # how often the stream looks for new events rows (A.11)
KEEPALIVE_S = 10.0  # an SSE comment keeps an idle connection open through proxies
STREAM_BATCH = 500
DEFAULT_PORT = 8650
SERVE_WRITER = "research-serve"
WATCHER_INTERVAL_S = 60.0
RUN_POLL_INTERVAL_S = 1.0
# How Hermes describes its prompt before a file-tool write to an AGENTS.md (``tools/
# file_tools_write_guards.py``), which it raises even with approvals off.
PROTECTED_INSTRUCTION_PROMPT = "Write to protected agent-instruction file(s): "
DEFAULT_ACTOR = "user:local"
RETRY_AFTER_S = 1
UI_ACTIONS = ("answer", "reaction")
# An applied input response is its ``client.input_responded`` event, keyed by the client's op_id.
INPUT_RESPONSE = "SELECT payload FROM events WHERE kind='client.input_responded' AND subject=?"
RECORDED_ROUTES = {
    "/session-open": "session_open",
    "/message": "message",
    "/message-cancel": "message_cancel",
    "/ui-action": "ui_action",
    "/store/apply": "store_apply",
    "/models": "models",
}
PAGE_PATHS = ("/", "/ui")
PAGE_FILE = resources.files("research_core").joinpath("ui", "index.html")


def _answer_text(ask_id: str, ask: dict, shown: str) -> str:
    """The answer as a Session reads it, often not the one that asked: it carries the question."""
    text = f"[UI action] answer to {ask_id}: {shown}"
    if ask.get("question"):
        text += f"\nThe question: {ask['question']}"
    if ask.get("refs"):
        text += f"\nAbout: {', '.join(ask['refs'])}"
    return text


class Refused(Exception):
    """A request the server answers with an error status and a JSON body, never a traceback."""

    def __init__(
        self,
        status: int,
        error: str,
        *,
        problems: list[str] | None = None,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(error)
        self.status = status
        self.headers = headers or {}
        self.body = {"ok": False, "error": error, "problems": list(problems or []), **extra}


def _gateway_error(error: RunsError) -> Refused:
    """A runs API failure as a reply: 503 with ``Retry-After`` when the gateway is busy or
    unreachable (the client retries), 502 otherwise. The gateway's own HTTP status is kept
    as ``gateway_status`` beside the reply's."""
    body = error.to_dict()
    detail = body.pop("error")
    body["gateway_status"] = body.pop("status")
    if isinstance(error, RunsBusy):
        return Refused(
            HTTPStatus.SERVICE_UNAVAILABLE,
            detail,
            headers={"Retry-After": str(RETRY_AFTER_S)},
            retry_after=RETRY_AFTER_S,
            **body,
        )
    return Refused(HTTPStatus.BAD_GATEWAY, detail, **body)


def _text(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise Refused(HTTPStatus.BAD_REQUEST, f"{key} is required: a non-empty string")
    return value


def _object(body: dict, key: str, default: dict | None = None) -> dict:
    value = body.get(key, default)
    if not isinstance(value, dict):
        raise Refused(HTTPStatus.BAD_REQUEST, f"{key} must be a JSON object")
    return value


def _actor(text: str | None) -> Actor:
    """``user:<id>`` or ``harness`` (``user:local`` when absent); a session actor is refused."""
    try:
        return store_cli.parse_actor(text or DEFAULT_ACTOR)
    except store_cli.Refusal as error:
        raise Refused(HTTPStatus.BAD_REQUEST, error.error, problems=error.problems) from None


def _error_name(error: BaseException) -> str:
    """An exception's type, as a record can hold it. Its text can hold anything."""
    return type(error).__name__.lower()


def _accepted_fields(path: str, body: dict, result: dict) -> dict[str, Any]:
    """What an accepted request's reply and validated body can say about it."""
    fields: dict[str, Any] = {
        "session_id": result.get("session_id"),
        "run_id": result.get("run_id"),
    }
    if result.get("op_id"):
        fields["admission_id"] = result["op_id"]
    if isinstance(body.get("actor"), str):
        fields["reported_actor"] = body["actor"]
    if path == "/session-open":
        scope = result["session_scope"]
        capabilities = body.get("capabilities")
        client_kind = result["client"]["kind"]
        fields.update(
            kind="chat",
            session_scope=scope["scope"],
            coworld_id=scope["coworld"],
            league_id=scope["league"],
            initiator=client_kind,
            details={"capabilities": len(capabilities)} if capabilities else {},
        )
    return fields


def _model_refusal(error: ModelError) -> Refused:
    status = HTTPStatus.NOT_FOUND if error.not_found else HTTPStatus.BAD_REQUEST
    return Refused(status, error.error, problems=error.problems)


class SeamServer:
    """The seam over one workspace and one Hermes gateway. See the module docstring."""

    def __init__(
        self,
        *,
        ws: layout.Workspace,
        hermes: HermesRuns,
        profile: str = "orchestrator",
        host: str = "127.0.0.1",
        port: int = 0,
        inference_lease_file: Path | None = None,
        profiles: list[Profile] | None = None,
        hermes_bin: str = "hermes",
        env: dict[str, str] | None = None,
        config_runner: Runner = subprocess.run,
        catalog: Catalog | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.ws = ws
        self.clock = clock
        self.hermes = hermes
        self.profile = profile
        self.inference_lease_file = (
            hermes.inference_lease_file if inference_lease_file is None else inference_lease_file
        )
        # The profiles whose model the bridge shows and changes, and how it changes them.
        self.profiles = profiles or []
        self.hermes_bin = hermes_bin
        self.env = {**dict(os.environ if env is None else env), "IDE_WORKSPACE": str(ws.root)}
        self.coordinator = Coordinator(
            ws,
            hermes,
            self.env.get("IDE_RUNTIME_ID"),
            inference_lease_file=self.inference_lease_file,
            clock=clock,
        )
        self.config_runner = config_runner
        self.catalog = catalog or Catalog()
        self._model_lock = threading.Lock()
        # The client that opened each Session, for the stamp (A.4). A Session this instance
        # did not open is stamped with the client the context block names, if any.
        self._clients: dict[str, dict[str, Any]] = {}
        self._deliver_lock = threading.Lock()
        self._acknowledged_receipts: dict[str, str] = {}
        self._last_native_poll: dict[tuple[str, str], float] = {}
        self.stopping = threading.Event()
        # What the coordinator records goes through here: metadata only, because it has no scanner.
        self.telemetry = TelemetryWriter(
            stream_path(), Producer.for_process(SERVE_WRITER, self.env)
        )
        # A record names the span it is written in, such as the request admitting a message.
        observed.bind(self.telemetry, lambda: correlation(trace.get_current_span()))
        # Initialize the schema before the dispatcher and HTTP handlers can open it concurrently.
        with Store.open(self.ws.store_path, kind="workspace") as store:
            self.coordinator.reconcile_inputs(store)
            self.coordinator.adopt_existing_workers(self.ws, store, self.coordinator.runtime_id)
        self._http = _HttpServer((host, port), self)

    @property
    def url(self) -> str:
        host, port = self._http.server_address[:2]
        return f"http://{host}:{port}"

    def serve_forever(self) -> None:
        with ownership(self.ws):
            with Store.open(self.ws.store_path, kind="workspace") as store:
                store.set_meta("coordinator_instance", self.coordinator.instance_id)
            with ThreadPoolExecutor(max_workers=1) as worker:
                dispatch = worker.submit(asyncio.run, self.dispatch_forever())
                # A completed Future invokes callbacks inline, before the HTTP loop starts.
                dispatch.add_done_callback(
                    lambda _: threading.Thread(target=self._http.shutdown, daemon=True).start()
                )
                # The constructor bound the listener and initialized/recovered the stores.
                # Notify the supervisor only after acquiring ownership and starting dispatch.
                if address := self.env.get("NOTIFY_SOCKET"):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
                        sender.connect(address)
                        sender.send(b"READY=1")
                try:
                    self._http.serve_forever(poll_interval=0.2)
                finally:
                    self.stopping.set()
                dispatch.result()

    async def dispatch_forever(self) -> None:
        observer = asyncio.create_task(self.observe_forever())
        workflow = asyncio.create_task(self.coordinate_forever())
        try:
            while not self.stopping.is_set():
                (result,) = await asyncio.gather(self.dispatch_messages(), return_exceptions=True)
                if isinstance(result, RunsError):
                    log.warning("Admission unavailable: %s", result)
                elif isinstance(result, BaseException):
                    raise result
                await asyncio.to_thread(wait_for_change, change_version(), 0.5)
        finally:
            self.stopping.set()
            await asyncio.gather(observer, workflow)

    async def observe_forever(self) -> None:
        if not self.env.get("HERMES_HOME"):
            return
        while not self.stopping.is_set():
            (result,) = await asyncio.gather(
                asyncio.to_thread(self.observe_once), return_exceptions=True
            )
            if isinstance(result, BaseException):
                log.error("Watcher observation failed", exc_info=result)
                observed.record(
                    "coordinator",
                    "failed",
                    native_event_type="observe",
                    reason=_error_name(result),
                    owner_instance=self.coordinator.instance_id,
                )
            await asyncio.to_thread(self.stopping.wait, WATCHER_INTERVAL_S)

    def observe_once(self) -> None:
        self.coordinator.reconcile()

    async def coordinate_forever(self) -> None:
        while not self.stopping.is_set():
            (result,) = await asyncio.gather(
                asyncio.to_thread(self.coordinate_once), return_exceptions=True
            )
            if isinstance(result, BaseException):
                log.error("Coordinator workflow failed", exc_info=result)
                observed.record(
                    "coordinator",
                    "failed",
                    native_event_type="workflow",
                    reason=_error_name(result),
                    owner_instance=self.coordinator.instance_id,
                )
            await asyncio.to_thread(wait_for_change, change_version(), 0.5)

    def coordinate_once(self) -> None:
        with self.stores() as stores:
            coworld_ids = [row["coworld_id"] for row in coworlds.list(stores.wstore)]
        for coworld_id in [None, *coworld_ids]:
            if not self.env.get("HERMES_HOME"):
                from research_core import transitions

                with self.stores(coworld_id) as stores:
                    ctx = context.TickContext(
                        ws=self.ws,
                        wstore=stores.wstore,
                        store=None if coworld_id is None else stores.store(),
                        coworld=None if coworld_id is None else self.ws.coworld(coworld_id),
                        hermes=self.coordinator,
                        platform=None,
                        env=self.env,
                        now=datetime.now(UTC),
                    )
                    transitions.apply(ctx, self.coordinator.instance_id)
                continue
            ctx = context.build(self.env, hermes=self.coordinator, coworld_id=coworld_id)
            try:
                self.coordinator.advance(ctx)
            finally:
                ctx.wstore.close()
                if ctx.store is not None:
                    ctx.store.close()

    def shutdown(self) -> None:
        """Stop accepting requests and end every open stream; safe to call more than once."""
        self.stopping.set()
        self._http.shutdown()
        self._http.server_close()

    # --- stores ---------------------------------------------------------------------------

    @contextmanager
    def stores(self, coworld_id: str | None = None) -> Iterator[store_cli.Stores]:
        """The workspace store and an explicitly selected Coworld; closed on exit."""
        wstore = Store.open(self.ws.store_path, kind="workspace")
        with store_cli.Stores(self.ws, wstore, coworld_id) as s:
            yield s

    @staticmethod
    def store_for(stores: store_cli.Stores, kind: str) -> Store:
        if kind not in store_cli.STORE_KINDS:
            raise Refused(
                HTTPStatus.BAD_REQUEST, f"store must be one of {', '.join(store_cli.STORE_KINDS)}"
            )
        try:
            return stores.by_kind(kind)
        except StoreError as error:
            raise Refused(HTTPStatus.NOT_FOUND, error.problems[0]) from None

    # --- the routes -----------------------------------------------------------------------

    def health(self) -> dict:
        with self.stores() as stores:
            return {
                "ok": True,
                "profile": self.profile,
                "coworld": stores.coworld_id,
                "contract_version": contract.CONTRACT_VERSION,
            }

    def status(self) -> dict:
        """What ``research watcher status`` prints (``watcher/cli.py``): the last heartbeat and
        its age, the has-work signal with its reasons, and the pause state, from the workspace
        store's meta, settings and admissions and every registered Coworld store's unfinished
        transitions."""
        with self.stores() as stores:
            wstore = stores.wstore
            heartbeat = wstore.get_meta(HEARTBEAT_KEY)
            age_s = None
            if heartbeat is not None:
                age_s = round((datetime.now(UTC) - parse_iso(heartbeat)).total_seconds(), 1)
            pending_transitions = []
            for registered in coworlds.list(wstore):
                coworld = self.ws.coworld(registered["coworld_id"])
                store = context._open_coworld_store(coworld)
                if store is None:
                    continue
                try:
                    pending_transitions.extend(
                        {"coworld_id": coworld.coworld_id, **dict(row)}
                        for row in store.q(
                            "SELECT id,tool,actor,owner_instance,attempts,last_error,retry_at"
                            " FROM transition_commands WHERE result IS NULL"
                        )
                    )
                finally:
                    store.close()
            paused = bool(settings.get(wstore, "paused"))
            waiting_on = (
                json.loads(wstore.get_meta(HAS_WORK_REASONS_KEY) or "[]")
                + admissions.work_reasons(wstore)
                + ([] if paused else xp_followups.work_reasons(wstore))
            )
            return {
                "ok": True,
                "heartbeat": heartbeat,
                "heartbeat_age_s": age_s,
                "has_work": bool(waiting_on),
                "waiting_on": waiting_on,
                "paused": paused,
                "paused_drained": wstore.get_meta(PAUSED_DRAINED_KEY) == "1",
                "sleep_started": wstore.get_meta("sleep_started"),
                "blocked_executions": admissions.problems(wstore),
                "work": admissions.work_status(wstore, self.inference_lease_file),
                "coordinator_instance": wstore.get_meta("coordinator_instance"),
                "pending_transitions": pending_transitions,
                "restart_required": admissions.restart_required(wstore),
                "runtime_recovery_count": int(wstore.get_meta("runtime_recovery_count") or "0"),
            }

    @staticmethod
    def page() -> bytes:
        """The client page, read from the package on each request so edits show on reload."""
        return PAGE_FILE.read_bytes()

    @span("session.open")
    def session_open(self, body: dict) -> dict:
        """Mint a session id and register a chat Session with the client's context (A.2)."""
        current = trace.get_current_span()
        client = _object(body, "client")
        kept, problems = contract.validate_context(
            {"contract_version": contract.CONTRACT_VERSION, "client": client}
        )
        client = kept.get("client") or {}
        if not client.get("id") or not client.get("kind"):
            raise Refused(
                HTTPStatus.BAD_REQUEST, "client is required: {id, kind, guide?}", problems=problems
            )
        actor = _actor(body.get("actor"))
        op_id = str(UUID(_text(body, "op_id")))
        current.set_attribute("op.id", op_id)
        current.add_event("client.validated")
        raw_context = body.get("session_context")
        if raw_context is not None and not isinstance(raw_context, dict):
            raise Refused(HTTPStatus.BAD_REQUEST, "session_context must be an object")
        session_context, context_problems = contract.validate_context(raw_context or {})
        problems.extend(context_problems)
        coworld_id = session_context.get("coworld")
        scope = session_context.get("scope")
        if scope == "workspace" and (
            coworld_id is not None or session_context.get("league") is not None
        ):
            raise Refused(
                HTTPStatus.BAD_REQUEST, "workspace sessions cannot select a Coworld or league"
            )
        if scope == "coworld" and not coworld_id:
            raise Refused(
                HTTPStatus.BAD_REQUEST, "Coworld sessions require session_context.coworld"
            )
        with self.stores(coworld_id) as stores:
            current.add_event("store.opened")
            if coworld_id is not None:
                coworlds.register(
                    stores.wstore, actor, coworld_id, folder=layout.folder_name_for(coworld_id)
                )
                self.ws.coworld(coworld_id).ensure_dirs()
            session_id = (
                _text(body, "session_id") if "session_id" in body else hermes_api.mint_session_id()
            )
            existing = sessions.read(stores.wstore, session_id)
            if existing is not None:
                previous_context = existing["session_context"] or {}
                if (existing["coworld_id"], previous_context.get("league")) != (
                    coworld_id,
                    session_context.get("league"),
                ):
                    raise Refused(
                        HTTPStatus.CONFLICT,
                        "a session's Coworld and league cannot change",
                        kind="scope_mismatch",
                    )
            registration = intake.OpenRegistration(
                profile=self.profile,
                coworld_id=stores.coworld_id,
                session_context=session_context,
                client_capabilities=body.get("capabilities"),
            )
            current.add_event("metadata.prepared")
            receipt = intake.accept(
                stores.wstore,
                actor,
                op_id=op_id,
                kind="open",
                request=body,
                session_id=session_id,
                message="",
                payload="",
                registration=registration,
            )
            session_id = receipt.session_id
            self._clients[session_id] = client
            current.set_attributes({"session.id": receipt.session_id, "replayed": receipt.replayed})
            return {
                "session_id": session_id,
                "client": client,
                "coworld": coworld_id,
                "session_scope": {
                    "scope": "coworld" if coworld_id else "workspace",
                    "coworld": coworld_id,
                    "league": session_context.get("league"),
                },
                "contract_version": contract.CONTRACT_VERSION,
                "problems": problems,
            }

    @span("message.admit")
    def message(self, body: dict) -> dict:
        """Confirm a durable input once; dispatch serially with Hermes's durable run key."""
        current = trace.get_current_span()
        session_id = _text(body, "session_id")
        message = UserMessage.model_validate(body["message"])
        content = message.root["content"]
        text = (
            content
            if isinstance(content, str)
            else "\n".join(part["text"] for part in content if part["type"] == "text")
        )
        actor = _actor(body.get("actor"))
        op_id = str(UUID(_text(body, "op_id")))
        current.set_attribute("op.id", op_id)
        current.add_event("request.validated")
        log.info("Message admission started: op_id=%s", op_id)
        with self.stores() as stores:
            current.add_event("store.opened")
            self._require_session(stores, session_id)
            block, problems = self._context_block(stores, session_id, body.get("context"))
            payload = json.dumps(
                {
                    "input": [message.model_dump(mode="json", exclude_none=True)],
                    "context": block,
                    "instructions": body.get("instructions"),
                }
            )
            current.add_event("metadata.prepared")
            receipt = intake.accept(
                stores.wstore,
                actor,
                op_id=op_id,
                kind="message",
                request=body,
                session_id=session_id,
                message=text,
                payload=payload,
            )
            current.set_attributes({"session.id": receipt.session_id, "replayed": receipt.replayed})
            return {
                "op_id": op_id,
                "session_id": session_id,
                "run_id": receipt.run_id,
                "delivered": "queued",
                "problems": [] if receipt.replayed else problems,
                "seq": stores.wstore.last_seq(),
            }

    async def dispatch_messages(self) -> None:
        with self._deliver_lock, self.stores() as stores:
            actor = Actor.user("client-dispatch")
            self.coordinator.reconcile_inputs(stores.wstore)
            for opening in stores.wstore.q(
                "SELECT op_id,session_id FROM client_admissions"
                " WHERE kind='open' AND status='queued' ORDER BY rowid"
            ):
                (created,) = await asyncio.gather(
                    asyncio.to_thread(
                        self.hermes.create_session, opening["session_id"], profile=self.profile
                    ),
                    return_exceptions=True,
                )
                if isinstance(created, RunsError):
                    with stores.wstore.tx(actor) as tx:
                        tx.execute(
                            "UPDATE client_admissions SET status=?,error=? WHERE op_id=?",
                            (
                                "queued" if created.retryable else "failed",
                                str(created),
                                opening["op_id"],
                            ),
                        )
                        tx.event(
                            "client.open_failed", opening["op_id"], {"retryable": created.retryable}
                        )
                    continue
                if isinstance(created, BaseException):
                    raise created
                with stores.wstore.tx(actor) as tx:
                    tx.execute(
                        "UPDATE client_admissions SET status='completed',error=NULL WHERE op_id=?",
                        (opening["op_id"],),
                    )
            reserved: set[str] = set()
            pending = stores.wstore.q(
                "SELECT * FROM client_admissions AS message WHERE kind='message'"
                " AND (status IN ('queued','running') OR resolution_required=1)"
                " AND (message.dispatch_started=1 OR NOT EXISTS("
                " SELECT 1 FROM client_admissions AS opening"
                " WHERE opening.kind='open' AND opening.status='queued'"
                " AND opening.session_id=message.session_id)) ORDER BY rowid"
            )
            active = {row["op_id"] for row in pending}
            self._acknowledged_receipts = {
                op_id: run_id
                for op_id, run_id in self._acknowledged_receipts.items()
                if op_id in active
            }
            active_runs = {(row["profile"], row["run_id"]) for row in pending if row["run_id"]}
            self._last_native_poll = {
                key: at for key, at in self._last_native_poll.items() if key in active_runs
            }
            for row in pending:
                if row["session_id"] in reserved:
                    continue
                if row["resume_after"] is not None:
                    # A held (``waiting``) message keeps its Session's place until it resumes.
                    reserved.add(row["session_id"])
                    with admissions.dispatch_lock(stores.wstore):
                        resumed = self.coordinator.resume_if_due(stores.wstore, row)
                    if resumed is None:
                        continue
                    row = resumed
                if row["run_id"]:
                    if self._acknowledged_receipts.get(row["op_id"]) != row["run_id"]:
                        self.coordinator.acknowledge_inputs(stores.wstore, row)
                        self._acknowledged_receipts[row["op_id"]] = row["run_id"]
                    key = (row["profile"], row["run_id"])
                    now = time.monotonic()
                    if row["cancel_requested"]:
                        status = self.hermes.stop(row["run_id"], profile=row["profile"])
                    elif (
                        now - self._last_native_poll.get(key, float("-inf")) >= RUN_POLL_INTERVAL_S
                    ):
                        status = self.hermes.status(row["run_id"], profile=row["profile"])
                    else:
                        continue
                    self._last_native_poll[key] = now
                    if status.status == "waiting_for_approval":
                        self._approve_unattended_instruction_writes(stores, row, status)
                    self.coordinator.settle_attempt(stores.wstore, row["op_id"], status)
                    continue
                opening = stores.wstore.one(
                    "SELECT status,error FROM client_admissions WHERE kind='open'"
                    " AND session_id=? ORDER BY rowid DESC LIMIT 1",
                    (row["session_id"],),
                )
                if (
                    opening is not None
                    and opening["status"] == "failed"
                    and not row["dispatch_started"]
                ):
                    with stores.wstore.tx(actor) as tx:
                        tx.execute(
                            "UPDATE client_admissions SET status='failed',error=? WHERE op_id=?",
                            (opening["error"], row["op_id"]),
                        )
                        tx.event("client.finished", row["op_id"], {"status": "failed"})
                    continue
                if row["cancel_requested"] and not row["dispatch_started"]:
                    with stores.wstore.tx(actor) as tx:
                        tx.execute(
                            "UPDATE client_admissions SET status='cancelled' WHERE op_id=?",
                            (row["op_id"],),
                        )
                        tx.event("client.cancelled", row["op_id"], {})
                    continue
                if row["retry_at"] and parse_iso(stores.wstore.now()) < parse_iso(row["retry_at"]):
                    # A refused start waits out its backoff and keeps its Session's place.
                    reserved.add(row["session_id"])
                    continue
                if settings.get(stores.wstore, "paused") and not row["dispatch_started"]:
                    continue
                if not inference.available(self.inference_lease_file, self.clock()):
                    continue
                session = self._require_session(stores, row["session_id"])
                if (
                    row["origin"] not in {"client", "answer", "continuation"}
                    and not row["dispatch_started"]
                    and session["run_id"]
                    and not self.hermes.status(session["run_id"], profile=row["profile"]).terminal
                ):
                    continue
                reserved.add(row["session_id"])
                payload = json.loads(row["payload"])
                # Attachments are prepared once, at the first start. A resume sends the
                # continuation prompt, not the admitted input, so it prepares nothing.
                if (
                    not row["dispatch_started"]
                    and not row["resumes"]
                    and isinstance(payload["input"], list)
                ):
                    (prepared,) = await asyncio.gather(
                        asyncio.to_thread(
                            attachments.prepare,
                            UserMessage.model_validate(payload["input"][0]),
                            ws=self.ws,
                            env=self.env,
                            hermes_bin=self.hermes_bin,
                            admission_id=row["op_id"],
                            session_id=row["session_id"],
                        ),
                        return_exceptions=True,
                    )
                    if isinstance(
                        prepared,
                        (AttachmentError, PlatformError, HTTPError, subprocess.TimeoutExpired),
                    ):
                        detail = (
                            str(prepared)
                            if isinstance(prepared, AttachmentError)
                            else type(prepared).__name__
                        )
                        observed.record(
                            "attachment",
                            "failed",
                            admission_id=row["op_id"],
                            session_id=row["session_id"],
                            reason=_error_name(prepared),
                        )
                        with stores.wstore.tx(actor) as tx:
                            tx.execute(
                                "UPDATE client_admissions SET status='failed',error=?"
                                " WHERE op_id=?",
                                (detail, row["op_id"]),
                            )
                            tx.event("client.delivery_failed", row["op_id"], {"error": detail})
                        continue
                    if isinstance(prepared, BaseException):
                        raise prepared
                    payload["input"] = [prepared.model_dump(mode="json")]
                    # Freeze the prepared body before native admission, retaining the user's
                    # original message in request for the application contract and history.
                    with admissions.dispatch_lock(stores.wstore), stores.wstore.tx(actor) as tx:
                        tx.execute(
                            "UPDATE client_admissions SET payload=? WHERE op_id=?"
                            " AND dispatch_started=0",
                            (json.dumps(payload), row["op_id"]),
                        )
                with span(
                    "workspace.dispatch",
                    context=propagator.extract(payload.get("trace_context", {})),
                ):
                    (run_id,) = await asyncio.gather(
                        asyncio.to_thread(
                            self.coordinator.start,
                            profile=row["profile"],
                            session_id=row["session_id"],
                            text=payload["input"],
                            instructions=payload["instructions"],
                            idempotency_key=row["native_key"] or row["op_id"],
                        ),
                        return_exceptions=True,
                    )
                if isinstance(run_id, RunsError):
                    backoff = int(settings.get(stores.wstore, "recovery.delivery_backoff_seconds"))
                    with stores.wstore.tx(actor) as tx:
                        # A retryable refusal records when to try again, like a held message's
                        # resume_after, so the dispatcher does not spin and has_work skips it.
                        # A start that may have reached Hermes (no answer, or a malformed one)
                        # replays at once; a gateway at its cap (429) refused it definitely.
                        tx.execute(
                            "UPDATE client_admissions SET error=?,"
                            "retry_at=CASE WHEN ? OR dispatch_started=0 THEN ? END,"
                            "status=CASE WHEN status IN ('queued','running')"
                            " THEN ? ELSE status END WHERE op_id=?",
                            (
                                run_id.detail,
                                isinstance(run_id, RunsBusy)
                                and not isinstance(run_id, RunsUnavailable),
                                (parse_iso(tx.at) + timedelta(seconds=backoff)).isoformat()
                                if run_id.retryable
                                else None,
                                "queued" if run_id.retryable else "failed",
                                row["op_id"],
                            ),
                        )
                        if not run_id.retryable:
                            # A resume's class described the earlier run, not this refusal.
                            tx.execute(
                                "UPDATE client_admissions SET error_class=NULL WHERE op_id=?",
                                (row["op_id"],),
                            )
                        tx.event(
                            "client.delivery_failed",
                            row["op_id"],
                            {"retryable": run_id.retryable},
                        )
                    continue
                if isinstance(run_id, BaseException):
                    raise run_id
                if row["cancel_requested"]:
                    self.hermes.stop(run_id, profile=row["profile"])

    def session_pending(self, session_id: str) -> dict:
        """A locally accepted chat is empty until its serial native opening completes."""
        with self.stores() as stores:
            return {
                "pending": bool(
                    stores.wstore.one(
                        "SELECT EXISTS(SELECT 1 FROM client_admissions"
                        " WHERE kind='open' AND session_id=? AND status='queued')"
                        " AND NOT EXISTS(SELECT 1 FROM client_admissions"
                        " WHERE kind='open' AND session_id=? AND status='completed')",
                        (session_id, session_id),
                    )[0]
                )
            }

    def message_context(self, body: dict) -> dict:
        """Join application metadata by native row identity, never by parsing message text."""
        session_id = _text(body, "session_id")
        with self.stores() as stores:
            admissions_by_message = {
                row["native_message_id"]: (
                    json.loads(row["payload"])["context"],
                    json.loads(row["request"])["message"]["content"],
                )
                for row in stores.wstore.q(
                    "SELECT native_message_id,payload,request FROM client_admissions"
                    " WHERE session_id=? AND native_message_id IS NOT NULL AND origin='client'",
                    (session_id,),
                )
            }
            followup_messages = {
                row["native_message_id"]
                for row in stores.wstore.q(
                    "SELECT native_message_id FROM client_admissions"
                    " WHERE session_id=? AND origin='followup'",
                    (session_id,),
                )
            }
            return {
                "data": [
                    {
                        **message,
                        **(
                            {"content": admissions_by_message[message["id"]][1]}
                            if message["id"] in admissions_by_message
                            else {}
                        ),
                        **({"runtime_input": True} if message["id"] in followup_messages else {}),
                        "client_context": admissions_by_message[message["id"]][0]
                        if message["id"] in admissions_by_message
                        else None,
                    }
                    for message in body["data"]
                ]
            }

    def message_event(self, body: dict) -> dict:
        """Append one AIDE event to a message's log; the runtime pushes it only after this."""
        op_id = str(UUID(_text(body, "op_id")))
        event = _object(body, "event")
        with self.stores() as stores, stores.wstore.tx(_actor(body.get("actor"))) as tx:
            admissions.append_event(tx, op_id, event, body["run_id"], body["source_seq"])
        return {"op_id": op_id, "attempt": event["attempt"], "seq": event["seq"]}

    def message_events(self, query: dict[str, str]) -> dict:
        """A message's logged AIDE events after a cursor (``admissions.events_after``)."""
        with self.stores() as stores:
            return admissions.events_after(
                stores.wstore,
                str(UUID(query["op_id"])),
                int(query["attempt"]),
                int(query["seq"]),
                int(query["limit"]),
            )

    def message_cancel(self, body: dict) -> dict:
        op_id = str(UUID(_text(body, "op_id")))
        with self._deliver_lock, self.stores() as stores:
            row = stores.wstore.one(
                "SELECT * FROM client_admissions WHERE op_id=? AND kind='message'", (op_id,)
            )
            if row is None:
                raise Refused(HTTPStatus.NOT_FOUND, "Unknown message admission")
            if row["dispatch_started"] and row["delivery_mode"] == "steer":
                # Its run is another admission's: stopping it would end that request too.
                raise Refused(HTTPStatus.CONFLICT, "The message was merged into a live run")
            if row["status"] in {"queued", "running"}:
                with stores.wstore.tx(_actor(body.get("actor"))) as tx:
                    tx.execute(
                        "UPDATE client_admissions SET cancel_requested=1 WHERE op_id=?", (op_id,)
                    )
                    if not row["dispatch_started"]:
                        tx.execute(
                            "UPDATE client_admissions SET status='cancelled' WHERE op_id=?",
                            (op_id,),
                        )
                    tx.event("client.cancel_requested", op_id, {})
            if row["run_id"]:
                self.hermes.stop(row["run_id"], profile=self.profile)
            return {"op_id": op_id, "stop_requested": True}

    def input_response(self, op_id: str) -> dict:
        """The input response a client applied under ``op_id``, or ``request: null``."""
        op_id = str(UUID(op_id))
        with self.stores() as stores:
            row = stores.wstore.one(INPUT_RESPONSE, (op_id,))
        return {"op_id": op_id, "request": None if row is None else json.loads(row["payload"])}

    def input_responded(self, body: dict) -> dict:
        """Record an input response the gateway applied, so an identical retry replays it: the
        gateway keeps no record of how a prompt was resolved."""
        op_id = str(UUID(_text(body, "op_id")))
        request = _object(body, "request")
        with self.stores() as stores, stores.wstore.tx(_actor(body.get("actor"))) as tx:
            row = tx.one(INPUT_RESPONSE, (op_id,))
            if row is None:
                tx.event("client.input_responded", op_id, request)
            elif json.loads(row["payload"]) != request:
                raise Refused(HTTPStatus.BAD_REQUEST, "op_id was already used for different input")
        return {"op_id": op_id, "replayed": row is not None}

    def ui_action(self, body: dict) -> dict:
        """An answer goes into the Session at once; a reaction is feedback and starts no run."""
        session_id = _text(body, "session_id")
        action = _text(body, "action")
        payload = _object(body, "payload", {})
        actor = _actor(body.get("actor"))
        if action not in UI_ACTIONS:
            raise Refused(
                HTTPStatus.BAD_REQUEST, f"unknown action {action!r}; one of {', '.join(UI_ACTIONS)}"
            )
        with self.stores() as stores:
            self._require_session(stores, session_id)
            if action == "answer":
                return self._answer(stores, actor, session_id, payload)
            return self._reaction(stores, actor, session_id, payload)

    def store_apply(self, body: dict) -> dict:
        """One allowed library function as a user or harness actor (A.10, A2.10)."""
        op = _text(body, "op")
        actor = _actor(_text(body, "actor"))
        args = _object(body, "args", {})
        try:
            if op not in store_cli.ALLOWED:
                allowed = ", ".join(sorted(store_cli.ALLOWED))
                raise store_cli.Refusal(f"{op!r} is not an op store apply runs; allowed: {allowed}")
            op_args = store_cli.parse_op_args(json.dumps(args))
            with self.stores(body.get("coworld")) as stores:
                kind = body.get("store") or store_cli.store_kind_for(op, op_args)
                if kind not in store_cli.STORE_KINDS:
                    raise store_cli.Refusal(
                        f"store must be one of {', '.join(store_cli.STORE_KINDS)}"
                    )
                record = store_cli._call(op, kind, stores, actor, op_args)
                seq = stores.by_kind(kind).last_seq()
        except store_cli.Refusal as error:
            raise Refused(HTTPStatus.BAD_REQUEST, error.error, problems=error.problems) from None
        except StoreError as error:
            raise Refused(
                HTTPStatus.BAD_REQUEST, f"{op} refused", problems=error.problems
            ) from None
        from research_core.transitions import CommandReceipt

        if isinstance(record, CommandReceipt):
            return {
                "status": "pending",
                "op": op,
                "store": kind,
                "coworld_id": stores.coworld_id,
                "command": record.payload,
                "seq": seq,
            }
        return {"ok": True, "op": op, "store": kind, "record": record, "seq": seq}

    def command_status(self, coworld_id: str, command_id: str) -> dict:
        from research_core.transitions import receipt

        with self.stores() as stores:
            coworlds.read(stores.wstore, coworld_id)
            with Store.open(self.ws.coworld(coworld_id).store_path, kind="coworld") as store:
                return receipt(store, command_id)

    def snapshot(
        self,
        kind: str,
        coworld_id: str | None = None,
        *,
        session_id: str | None = None,
        league: str | None = None,
        requests: bool = False,
        request_id: str | None = None,
    ) -> dict:
        """One store's snapshot. ``requests`` reads AIDE's view of the workspace: top-level chat
        Sessions (all of them unless a scope or Session selects) with every message admission as
        ``admissions.requests`` reads it; ``request_id`` selects the Session of that admission."""
        if kind == "coworld" and coworld_id is None:
            raise Refused(HTTPStatus.BAD_REQUEST, "coworld is required for a Coworld snapshot")
        with self.stores(coworld_id) as stores:
            if kind == "workspace" and (session_id or coworld_id or league or requests):
                with stores.wstore.read_tx():
                    if request_id:
                        owner = stores.wstore.one(
                            "SELECT session_id FROM client_admissions WHERE op_id=?"
                            " AND kind='message'",
                            (request_id,),
                        )
                        if owner is None:
                            raise Refused(HTTPStatus.NOT_FOUND, "Unknown message admission")
                        session_id = owner["session_id"]
                    if session_id:
                        session = self._require_session(stores, session_id)
                        if (
                            session["coworld_id"],
                            (session["session_context"] or {}).get("league"),
                        ) != (coworld_id, league):
                            raise Refused(
                                HTTPStatus.CONFLICT,
                                "this session belongs to a different Coworld or league",
                                kind="scope_mismatch",
                            )
                        selected = [session]
                    else:
                        selected = [
                            sessions.read(stores.wstore, row["id"])
                            for row in stores.wstore.q(
                                "SELECT id FROM sessions WHERE kind='chat' AND parent IS NULL"
                                " AND (? OR (coworld_id IS ?"
                                " AND json_extract(session_context, '$.league') IS ?))"
                                " ORDER BY rowid",
                                (requests and not (coworld_id or league), coworld_id, league),
                            )
                        ]
                    records = {"sessions": selected}
                    if requests:
                        ids = [session["id"] for session in selected]
                        records["requests"] = admissions.requests(stores.wstore, ids)
                    else:
                        records["admissions"] = (
                            admissions.snapshot(stores.wstore, session_id) if session_id else []
                        )
                    return {
                        "store": "workspace",
                        "seq": stores.wstore.last_seq(),
                        "records": records,
                        "ui_state": [],
                    }
            if coworld_id is not None:
                coworlds.read(stores.wstore, coworld_id)
            return store_cli.snapshot(self.store_for(stores, kind), stores.coworld_id)

    def board_snapshot(self, coworld_id: str, resource: str, item_id: str | None) -> dict:
        """Only the requested board rows and the event cursor from the same read transaction."""
        specs = {"tasks": TASK, "goals": GOAL}
        if resource not in specs:
            raise Refused(HTTPStatus.BAD_REQUEST, "Board resource must be tasks or goals")
        spec = specs[resource]
        with self.stores(coworld_id) as stores:
            coworlds.read(stores.wstore, coworld_id)
            store = self.store_for(stores, "coworld")
            with store.read_tx():
                if item_id is None:
                    rows = store.q(f"SELECT * FROM {spec.table} ORDER BY rowid")
                else:
                    kind, number = parse_display_id(item_id)
                    if kind != resource[:-1]:
                        raise Refused(
                            HTTPStatus.BAD_REQUEST, "Item ID does not match the board resource"
                        )
                    rows = store.q(f"SELECT * FROM {spec.table} WHERE id = ?", (number,))
                return {
                    "seq": store.last_seq(),
                    "records": {resource: [record_from_row(spec, row) for row in rows]},
                }

    def run_status(self, run_id: str) -> dict:
        return dataclasses.asdict(self.hermes.status(run_id, profile=self.profile))

    # --- what the HTTP boundary records ----------------------------------------------------

    def record_request(
        self,
        path: str,
        headers: Mapping[str, str],
        body: dict,
        status: int,
        *,
        result: dict | None = None,
        reason: str | None = None,
    ) -> None:
        """One record per request. A second attempt of an admission writes no second
        ``client.accepted``."""
        try:
            # Only a string has its tokens masked, and a refused body can hold anything.
            op_id = body.get("op_id")
            fields: dict[str, Any] = {
                "admission_id": op_id if isinstance(op_id, str) else None,
                "size_bytes": int(headers.get("Content-Length") or 0),
                "native_event_type": RECORDED_ROUTES[path],
            }
            if reason is not None:
                # What the client sent, unchecked. Only a string goes through the token mask.
                session_id = body.get("session_id")
                fields["session_id"] = session_id if isinstance(session_id, str) else None
                observed.record(
                    "admission", "refused", reason=reason, details={"status": status}, **fields
                )
                return
            fields.update(_accepted_fields(path, body, result))
            fields["details"] = {"status": status, **fields.get("details", {})}
            observed.record("admission", "accepted", **fields)
        except Exception as error:  # A record must never change the reply.
            log.warning("Request not recorded: %s", type(error).__name__)

    # --- models ---------------------------------------------------------------------------

    def models(self) -> dict:
        """Each profile's model, provider and fallbacks, read from its ``config.yaml``."""
        try:
            return {"ok": True, "profiles": [profile_models.read(p) for p in self.profiles]}
        except ModelError as error:
            raise _model_refusal(error) from None

    def models_catalog(self) -> dict:
        """OpenRouter's models that take tool calls, for a client's model picker."""
        models = self.catalog.models()
        if models is None:
            raise Refused(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "the OpenRouter model list cannot be read now",
                headers={"Retry-After": "30"},
            )
        return {"ok": True, "source": profile_models.CATALOG_URL, "models": models}

    def set_model(self, body: dict) -> dict:
        """Change one profile's model (and optionally its fallbacks) as the user asked; record
        it as a ``profile.model_updated`` event. One change at a time."""
        actor = _actor(body.get("actor"))
        if not str(actor).startswith("user:"):
            raise Refused(HTTPStatus.BAD_REQUEST, "only a user changes a profile's model")
        try:
            profile = profile_models.find(self.profiles, _text(body, "profile"))
            with self._model_lock:
                previous = profile_models.read(profile)
                change = profile_models.parse_change(body, previous)
                verified = profile_models.check_against_catalog(change, self.catalog)
                written = profile_models.write(
                    profile,
                    change,
                    previous,
                    hermes=self.hermes_bin,
                    env=self.env,
                    run=self.config_runner,
                )
                current = profile_models.read(profile)
                seq = self._record_model_change(actor, current, previous, written)
        except ModelError as error:
            raise _model_refusal(error) from None
        return {
            "ok": True,
            "profile": current,
            "previous": previous,
            "changed": written,
            "verified": verified,
            "seq": seq,
        }

    def _record_model_change(
        self, actor: Actor, current: dict, previous: dict, written: list[str]
    ) -> int | None:
        if not written:
            return None  # nothing differed: no event
        with self.stores() as stores, stores.wstore.tx(actor) as tx:
            return tx.event(
                "profile.model_updated",
                current["profile"],
                {**current, "previous": previous, "changed": written},
            )

    # --- the pieces -----------------------------------------------------------------------

    def _approve_unattended_instruction_writes(
        self, stores: store_cli.Stores, row: Mapping[str, Any], status: hermes_api.RunStatus
    ) -> None:
        """Approve Hermes's AGENTS.md write prompt in a Task or wake run. Every /v1/runs run can
        raise it, but only a chat has a person to answer, through AIDE; elsewhere it waited 300 s
        and failed, and the setup wake writes the lab AGENTS.md. A chat keeps the prompt."""
        if self._require_session(stores, row["session_id"])["kind"] == "chat":
            return
        for control in status.raw["pending_controls"]:
            if control["kind"] == "approval" and control["description"].startswith(
                PROTECTED_INSTRUCTION_PROMPT
            ):
                self.hermes.approve_once(row["run_id"], control, profile=row["profile"])

    def _require_session(self, stores: store_cli.Stores, session_id: str) -> dict:
        session = sessions.read(stores.wstore, session_id)
        if session is None:
            raise Refused(
                HTTPStatus.NOT_FOUND, f"unknown session {session_id}; call /session-open first"
            )
        return session

    def _context_block(
        self, stores: store_cli.Stores, session_id: str, context: object
    ) -> tuple[dict | None, list[str]]:
        """The validated block with the server's stamps, or ``None`` when there is nothing to
        send: no context and no client known for the Session."""
        client = self._clients.get(session_id)
        if client is None:
            opened = stores.wstore.one(
                "SELECT request FROM client_admissions WHERE session_id=? AND kind='open'",
                (session_id,),
            )
            if opened is not None:
                client = json.loads(opened["request"])["client"]
        if context is None and client is None:
            return None, []
        kept, problems = ({}, []) if context is None else contract.validate_context(context)
        block = {**kept, "contract_version": contract.CONTRACT_VERSION}
        if client is not None:
            block["client"] = client
        session = self._require_session(stores, session_id)
        coworld_id = session["coworld_id"]
        declared = session["session_context"] or {}
        for field, expected in (
            ("scope", declared.get("scope", "coworld" if coworld_id else "workspace")),
            ("coworld", coworld_id),
            ("league", declared.get("league")),
        ):
            shown = kept.get(field)
            if shown is not None and shown != expected:
                raise Refused(
                    HTTPStatus.CONFLICT,
                    f"context.{field} differs from this session; open a new session",
                    kind="scope_mismatch",
                )
            if expected is not None and (field != "scope" or "scope" in declared):
                block[field] = expected
        return block, problems

    def _answer(
        self, stores: store_cli.Stores, actor: Actor, session_id: str, payload: dict
    ) -> dict:
        ask_id = _text(payload, "ask_id")
        if "value" not in payload:
            raise Refused(HTTPStatus.BAD_REQUEST, "value is required: the user's answer")
        value = payload["value"]
        conversation = self._ask_conversation(stores.wstore, session_id, ask_id)
        shown = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        key = f"ui-answer:{session_id}:{ask_id}"
        with stores.wstore.tx(actor) as tx:
            current = ui_state.get(stores.wstore, conversation)["doc"]["asks"][ask_id]
            if current["status"] != "open" and current["answer"] != value:
                raise Refused(
                    HTTPStatus.CONFLICT, f"{ask_id} was already answered", answer=current["answer"]
                )
            if current["status"] == "open":
                path = f"/asks/{pointer_token(ask_id)}"
                record = ui_state.patch_in_transaction(
                    tx,
                    conversation,
                    [
                        {"op": "test", "path": f"{path}/status", "value": "open"},
                        {"op": "replace", "path": f"{path}/status", "value": "answered"},
                        {"op": "add", "path": f"{path}/answer", "value": value},
                        {"op": "add", "path": f"{path}/answered_by", "value": str(actor)},
                    ],
                )
            else:
                record = ui_state.get(stores.wstore, conversation)
            op_id = admissions.enqueue_in_transaction(
                tx,
                key=key,
                session_id=session_id,
                profile=self._require_session(stores, session_id)["profile"],
                text=_answer_text(ask_id, current, shown),
                origin="answer",
            )
        result = {"op_id": op_id, "session_id": session_id, "run_id": None, "delivered": "queued"}
        return {
            "accepted": True,
            "conversation": conversation,
            "version": record["version"],
            **result,
        }

    @staticmethod
    def _ask_conversation(wstore: Store, session_id: str, ask_id: str) -> str:
        """Where the ask lives (A3.5): the chat's own conversation, else its Coworld's shared."""
        session = sessions.read(wstore, session_id)
        shared = coworld_conversation(session["coworld_id"] if session else None)
        for conversation in (session_id, shared):
            if ask_id in ui_state.get(wstore, conversation)["doc"].get("asks", {}):
                return conversation
        raise Refused(
            HTTPStatus.NOT_FOUND, f"{ask_id} is not an ask of this session or the workspace"
        )

    def _reaction(
        self, stores: store_cli.Stores, actor: Actor, session_id: str, payload: dict
    ) -> dict:
        """Feedback the agent sees at its next turn, as a UI state change under ``/reactions``.
        It never starts a run (design A.5)."""
        message_id = _text(payload, "message_id")
        if "value" not in payload:
            raise Refused(HTTPStatus.BAD_REQUEST, "value is required: the reaction")
        wstore = stores.wstore
        ops: list[dict[str, Any]] = []
        if "reactions" not in ui_state.get(wstore, session_id)["doc"]:
            ops.append({"op": "add", "path": "/reactions", "value": {}})
        ops.append(
            {
                "op": "add",
                "path": f"/reactions/{pointer_token(message_id)}",
                "value": {"value": payload["value"], "by": str(actor), "at": wstore.now()},
            }
        )
        record = ui_state.patch(wstore, actor, session_id, ops)
        return {
            "accepted": True,
            "conversation": session_id,
            "version": record["version"],
            "delivered": "none",
        }


# --- HTTP -------------------------------------------------------------------------------------


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], seam: SeamServer) -> None:
        super().__init__(address, _Handler)
        self.seam = seam


class _Handler(BaseHTTPRequestHandler):
    server: _HttpServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base signature
        log.debug("%s - %s", self.address_string(), format % args)

    # -- routing --------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        url = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(url.query).items()}
        seam = self.server.seam
        try:
            if url.path in PAGE_PATHS:
                self._html(HTTPStatus.OK, seam.page())
            elif url.path == "/health":
                self._json(HTTPStatus.OK, seam.health())
            elif url.path == "/status":
                self._json(HTTPStatus.OK, seam.status())
            elif url.path == "/command":
                self._json(
                    HTTPStatus.OK, seam.command_status(query["coworld_id"], query["command_id"])
                )
            elif url.path == "/snapshot" and "board" in query:
                self._json(
                    HTTPStatus.OK,
                    seam.board_snapshot(query["coworld"], query["board"], query.get("item")),
                )
            elif url.path == "/snapshot":
                self._json(
                    HTTPStatus.OK,
                    seam.snapshot(
                        query.get("store", "workspace"),
                        query.get("coworld"),
                        session_id=query.get("session_id"),
                        league=query.get("league"),
                        requests=query.get("requests") == "1",
                        request_id=query.get("request_id"),
                    ),
                )
            elif url.path == "/session-pending":
                self._json(HTTPStatus.OK, seam.session_pending(query["session_id"]))
            elif url.path == "/input-response":
                self._json(HTTPStatus.OK, seam.input_response(query["op_id"]))
            elif url.path == "/message-events":
                self._json(HTTPStatus.OK, seam.message_events(query))
            elif url.path == "/stream":
                self._stream(query)
            elif url.path == "/models":
                self._json(HTTPStatus.OK, seam.models())
            elif url.path == "/models/catalog":
                self._json(HTTPStatus.OK, seam.models_catalog())
            elif url.path.startswith("/runs/") and len(url.path) > len("/runs/"):
                self._json(HTTPStatus.OK, seam.run_status(url.path[len("/runs/") :]))
            else:
                raise Refused(HTTPStatus.NOT_FOUND, f"no route {url.path}")
        except Exception as error:  # noqa: BLE001 - every failure becomes a JSON error
            self._error(error)

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        path = urlsplit(self.path).path
        seam = self.server.seam
        routes = {
            "/session-open": seam.session_open,
            "/message": seam.message,
            "/message-cancel": seam.message_cancel,
            "/input-response": seam.input_responded,
            "/message-events": seam.message_event,
            "/messages/context": seam.message_context,
            "/ui-action": seam.ui_action,
            "/store/apply": seam.store_apply,
            "/models": seam.set_model,
        }
        unrecorded = path in RECORDED_ROUTES
        body: dict = {}
        request_span = trace.INVALID_SPAN

        def record(*args: Any, **kwargs: Any) -> None:
            """The request's record names the request's span, which its message's trace holds."""
            with trace.use_span(request_span, end_on_exit=False):
                seam.record_request(path, self.headers, body, *args, **kwargs)

        try:
            handler = routes.get(path)
            if handler is None:
                raise Refused(HTTPStatus.NOT_FOUND, f"no route POST {path}")
            with span("workspace.http", context=propagator.extract(self.headers)) as request_span:
                body = self._body()
                result = handler(body)
            status = (
                HTTPStatus.ACCEPTED
                if path == "/store/apply" and "status" in result and result["status"] == "pending"
                else HTTPStatus.OK
            )
            if unrecorded:
                unrecorded = False  # A reply that cannot be written is not a second outcome.
                record(status, result=result)
            self._json(status, result)
        except Exception as error:  # noqa: BLE001 - every failure becomes a JSON error
            self._error(error, record if unrecorded else None)

    # -- bodies and replies -----------------------------------------------------------------

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError as error:
            raise Refused(HTTPStatus.BAD_REQUEST, f"the body is not JSON: {error}") from None
        if not isinstance(body, dict):
            raise Refused(HTTPStatus.BAD_REQUEST, "the body must be a JSON object")
        return body

    def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _html(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, error: Exception, record: Callable[..., None] | None = None) -> None:
        """Answer with what ``error`` means. ``record`` gets the status and a reason with no free
        text, before the reply goes out."""
        if isinstance(error, RunsError):
            error = _gateway_error(error)
        headers = None
        if isinstance(error, Refused):
            status, body, headers = error.status, error.body, error.headers
        elif isinstance(error, ValueError):
            status = HTTPStatus.BAD_REQUEST
            body = {"ok": False, "error": str(error), "problems": []}
        elif isinstance(error, StoreError):
            status = HTTPStatus.NOT_FOUND if isinstance(error, NotFound) else HTTPStatus.BAD_REQUEST
            body = {"ok": False, "error": str(error), "problems": list(error.problems)}
        else:
            log.exception("request failed: %s %s", self.command, self.path)
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            body = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
                "problems": [],
                "traceback": traceback.format_exc()[-2000:],
            }
        if record is not None:
            phrase = HTTPStatus(status).phrase.lower().replace(" ", "_")
            record(status, reason=body.get("kind") or body.get("code") or phrase)
        self._json(status, body, headers)

    # -- the event stream -------------------------------------------------------------------

    def _stream(self, query: dict[str, str]) -> None:
        """``text/event-stream`` of one store's events after a seq (A.11).

        Frames are ``id: <seq>`` and ``data: <event JSON>``; a ``: keepalive`` comment goes out
        after ten idle seconds. A reconnecting client's ``Last-Event-ID`` header wins over
        ``after``. The loop ends when the client goes away or the server stops.
        """
        seam = self.server.seam
        kind = query.get("store", "workspace")
        after_text = self.headers.get("Last-Event-ID") or query.get("after", "0")
        try:
            cursor = int(after_text)
        except ValueError:
            raise Refused(
                HTTPStatus.BAD_REQUEST, f"after must be an integer seq, not {after_text!r}"
            ) from None
        with seam.stores(query.get("coworld")) as stores:
            store = seam.store_for(stores, kind)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self._write(f": stream {kind} after {cursor}\n\n")
                last_write = time.monotonic()
                while not seam.stopping.is_set():
                    events = store.events_after(cursor, limit=STREAM_BATCH)
                    for event in events:
                        data = json.dumps({"store": kind, **event.to_dict()}, ensure_ascii=False)
                        self._write(f"id: {event.seq}\ndata: {data}\n\n")
                        cursor = event.seq
                    if events:
                        last_write = time.monotonic()
                        continue
                    if time.monotonic() - last_write >= KEEPALIVE_S:
                        self._write(": keepalive\n\n")
                        last_write = time.monotonic()
                    wait_for_change(change_version(), TAIL_INTERVAL_S)
            except OSError:
                return  # the client closed the connection

    def _write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()


# --- the command ------------------------------------------------------------------------------


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Port (default {DEFAULT_PORT}; 0 picks one)"
    )
    parser.add_argument(
        "--profile", default="orchestrator", help="The Hermes profile that runs the chat Sessions"
    )
    parser.add_argument(
        "--wake", action="store_true", help="End a workspace sleep cycle before starting dispatch"
    )
    parser.add_argument(
        "--hermes",
        default=shutil.which("hermes") or "hermes",
        help="The hermes executable that writes a profile's model (default: hermes on PATH)",
    )


def run(args: argparse.Namespace) -> int:
    """Serve the workspace of ``IDE_WORKSPACE`` against the gateway of ``HERMES_HOME/.env``."""
    logging.basicConfig()
    log.setLevel(logging.INFO)
    env = os.environ
    startup_context = process_context(env) if args.wake else None
    if args.wake:
        from research_core.watcher.sleep import wake_from_env

        with span("workspace.application_wake", context=startup_context):
            result = wake_from_env(env)
            print(json.dumps({"ok": True, **result}, default=str))
    with (
        span("workspace.application_ready", context=startup_context) if args.wake else nullcontext()
    ):
        profile_home = Path(env.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
        researcher_profiles = [
            name.strip() for name in env.get("IDE_RESEARCHER_PROFILES", "researcher").split(",")
        ]
        hermes = HermesRuns.from_profile(
            profile_home, researcher_profiles=[name for name in researcher_profiles if name]
        )
        server = SeamServer(
            ws=layout.from_env(env),
            hermes=hermes,
            profile=args.profile,
            host=args.host,
            port=args.port,
            inference_lease_file=Path(env["IDE_INFERENCE_LEASE_FILE"])
            if "IDE_INFERENCE_LEASE_FILE" in env
            else None,
            profiles=profile_models.sandbox_profiles(
                profile_home.parent, args.profile, researcher_profiles
            ),
            hermes_bin=args.hermes,
            env=dict(env),
        )
    print(f"research serve: {server.url} (profile {args.profile}, Hermes {hermes.base_url})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research serve", description=run.__doc__)
    _add_arguments(parser)
    return run(parser.parse_args(argv))


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "serve",
        help="Serve the client seam locally: sessions, messages, UI actions, the event stream",
        description=(
            "The local stand-in for the bridge (design §10, A.11): POST /session-open, /message,"
            " /ui-action and /store/apply; GET /snapshot, /stream, /runs/<id> and /health;"
            " plus GET / (the client page) and GET /status (what watcher status prints)."
        ),
    )
    _add_arguments(parser)
    parser.set_defaults(run=run)
