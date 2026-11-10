"""The AIDE 1.0.0 backend: the platform relay's JSON-RPC methods over the seam and the gateway.

It runs beside the hosting control connection in every workspace. Reads are authoritative and
come from durable workspace state: request records from the seam's admissions and their events,
progress from each request's event log in the workspace store, messages from the gateway's
history. One consumer per open request is that log's only writer: it translates the gateway's
run events, appends each, and only then pushes it, so a push carries the cursor of the read
returning the same event. Consumers start with the backend and resume from their log's last
row, so a restart keeps what the log holds and loses what had not reached it, along with pushes
not yet sent. A frontend is subscribed to a session from its first request naming it until the
relay reports ``frontend.closed``.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from inference import Availability
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_serializer,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from research_core.run_failures import CONTINUATION_PREFIX
from research_core.store.db import DEFAULT_MAX_HOLD_S
from research_core.store.migrate import BUSY_TIMEOUT_MS
from research_core.telemetry import propagator, span, tracer

log = logging.getLogger(__name__)

# A seam write waits out a ``research store quiesce``: at most the default hold plus the usual lock
# wait, and then it has to commit and answer. Timing out first ends the backend for a normal hold.
SEAM_TIMEOUT_S = DEFAULT_MAX_HOLD_S + BUSY_TIMEOUT_MS / 1000 + 5
MAX_BYTES = 1048576
ACTIVE = frozenset({"queued", "running", "waiting"})
TERMINAL = frozenset({"completed", "cancelled", "failed", "interrupted"})
CONTROLS = frozenset({"approval.request", "approval.settled", "clarify.request", "clarify.settled"})
COMMANDS = frozenset(
    {"session.open", "session.page", "chat.send", "chat.cancel", "task.cancel", "input.respond"}
)
RECENT_TERMINAL = 10  # terminal requests a Session embeds beside its open ones
EMBEDDED_ACTIVE = 1000  # open requests a Session embeds at most
ERROR_CHARS = 4000  # RequestError.message
TEXT_CHARS = 65536  # reasoning, interim text and tool arguments in one event
MERGE_SECONDS = 0.25  # consecutive text deltas share one logged event for this long
REASONING_MERGE_SECONDS = 1.0  # and consecutive relayed reasoning chunks this long
DEFAULT_CLIENT = {"id": "aide", "kind": "ide"}
TASK_STATUSES = {
    "proposed": "proposed",
    "unclaimed": "pending",
    "in-progress": "in_progress",
    "complete": "completed",
    "abandoned": "abandoned",
}


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Wire(Closed):
    """An AIDE object: optional fields are absent when unset, required nullable ones stay null."""

    @model_serializer(mode="wrap")
    def sparse(self, handler):
        required = {
            field.serialization_alias or name
            for name, field in type(self).model_fields.items()
            if field.is_required()
        }
        return {k: v for k, v in handler(self).items() if v is not None or k in required}


class Scope(Closed):
    scope: Literal["workspace", "coworld"]
    coworld: str | None
    league: str | None


class Cursor(Closed):
    attempt: int = Field(ge=0)
    seq: int = Field(ge=0)


# --- parameters -------------------------------------------------------------------------------
# ``wake`` belongs to the relay, which decides whether a read may wake the workspace; by the time
# a read reaches this backend the workspace is awake, so it is accepted and has no effect here.


class Client(Closed):
    id: str
    kind: str
    guide: str | None = None


class Open(Closed):
    scope: Scope
    client: Client | None = None


class PageScope(Scope):
    scope: Literal["coworld"]
    coworld: str = Field(min_length=1, max_length=120)
    league: str = Field(min_length=1, max_length=120)


class SessionPage(Closed):
    session_id: str
    scope: Scope
    page: PageScope
    related_uploads: list[str] = Field(max_length=1000)
    target_session_id: str | None = None


class Send(Closed):
    session_id: str
    messages: list[dict[str, JsonValue]]
    scope: Scope
    context: dict[str, JsonValue] | None = None
    instructions: str | None = Field(default=None, max_length=64000)


class History(Closed):
    session_id: str
    cursor: str | None = None
    after_sequence: int | None = Field(default=None, ge=0)
    limit: int = 50
    scope: Scope
    wake: bool = True


class Events(Closed):
    session_id: str
    request_id: UUID
    after: Cursor
    limit: int = Field(default=200, ge=1, le=1000)
    scope: Scope
    wake: bool = True


class ChatCancel(Closed):
    session_id: str
    request_id: UUID
    scope: Scope


class StateList(Closed):
    resource: Literal["sessions", "tasks", "goals", "requests"]
    scope_id: str | None = None
    session_id: str | None = None
    cursor: str | None = None
    limit: int = 50
    scope: Scope | None = None
    wake: bool = True


class StateGet(Closed):
    resource: Literal["sessions", "tasks", "goals", "requests"]
    id: str
    scope: Scope
    wake: bool = True


class TaskCancel(Closed):
    task_id: str
    scope: Scope


class Approval(Closed):
    kind: Literal["approval"]
    choice: Literal["once", "deny"]


class Clarification(Closed):
    kind: Literal["clarification"]
    answer: str


class Respond(Closed):
    session_id: str
    request_id: UUID
    response: Annotated[Approval | Clarification, Field(discriminator="kind")]
    scope: Scope


# --- native replies ---------------------------------------------------------------------------


class Context(BaseModel):
    league: str | None = None


class SessionRow(BaseModel):
    id: str
    kind: str
    parent: str | None
    started: str
    ended: str | None
    coworld_id: str | None
    session_context: Context | None

    def scope(self) -> Scope | None:
        """The AIDE scope, or ``None`` for a Coworld session without a league (not exposed)."""
        league = self.session_context.league if self.session_context else None
        if self.coworld_id is None and league is None:
            return Scope(scope="workspace", coworld=None, league=None)
        if self.coworld_id is not None and league is not None:
            return Scope(scope="coworld", coworld=self.coworld_id, league=league)
        return None


class Run(BaseModel):
    attempt: int
    run_id: str
    steered: bool  # another request's live run, which that request's consumer logs


class Logged(BaseModel):
    """The last row of a request's event log, with the gateway position folded into it."""

    attempt: int
    seq: int
    run_id: str | None
    source_seq: int | None
    event: dict[str, JsonValue]


class Admission(BaseModel):
    """One message admission as ``research_core.store.admissions.requests`` reads it."""

    op_id: str
    session_id: str
    status: str
    input: dict[str, JsonValue] | None
    error: str | None
    error_class: str | None
    resume_after: str | None
    created_at: str
    updated_at: str
    revision: int
    resumes: int
    runs: list[Run]
    logged: Logged | None


class Records(BaseModel):
    sessions: list[SessionRow]
    requests: list[Admission]


class Snapshot(BaseModel):
    seq: int
    records: Records


class Waiting(BaseModel):
    detail: str


class TaskRow(BaseModel):
    id: str
    goal_id: int | None
    title: str
    description: str
    status: str
    outcome: str | None
    result: str | None
    for_human: str
    held_by: str | None
    waiting_on: Waiting | None


class GoalRow(BaseModel):
    id: str
    title: str
    description: str
    end_state: str
    status: str
    where_it_stands: str


class BoardRecords(BaseModel):
    tasks: list[TaskRow] = []
    goals: list[GoalRow] = []


class Board(BaseModel):
    seq: int
    records: BoardRecords


class Control(BaseModel):
    kind: Literal["approval", "clarify"]
    prompt_id: str
    execution_generation: int
    description: str = ""
    command: str = ""
    question: str = ""
    choices: list[str] = []


class RunState(BaseModel):
    # Only session-authority runs carry prompts; other runs have none to answer here.
    pending_controls: list[Control] = []


class NativeMessage(BaseModel):
    id: int
    role: str
    content: JsonValue = None
    tool_calls: list[JsonValue] | None = None
    tool_call_id: str | None = None
    display_kind: str | None = None
    client_context: dict[str, JsonValue] | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None
    timestamp: float | None = None


class NativePage(BaseModel):
    data: list[dict[str, JsonValue]]


class RunEvent(BaseModel):
    """One gateway run event (``/v1/runs/{id}/events``); ``seq`` is absent on stream markers."""

    event: str
    seq: int | None = None
    timestamp: float = 0
    delta: str | None = None
    text: str | None = None
    already_streamed: bool = False
    tool: str | None = None
    preview: str | None = None
    tool_call_id: str | None = None


class RunSession(BaseModel):
    """The native Session a gateway run executes in: the live one compaction last moved to."""

    session_id: str


class Thought(BaseModel):
    """One reasoning delta the gateway plugin relays (``plugin/reasoning_relay.py``).

    ``sessions`` is the agent's Session id, then each id compaction moved it from; ``n`` counts the
    plugin's lines since its gateway started, so a gap is lines lost on the way.
    """

    sessions: list[str]
    n: int
    text: str


class SeamEvent(BaseModel):
    seq: int
    kind: str
    subject: str
    payload: dict[str, JsonValue]


class Opened(BaseModel):
    session_id: str


class Opening(BaseModel):
    pending: bool


class Resolution(BaseModel):
    status: str


class Applied(BaseModel):
    """The ``input.respond`` params the seam recorded as applied under one request id."""

    request: dict[str, JsonValue] | None


# --- AIDE objects -----------------------------------------------------------------------------


class ApprovalInput(Wire):
    request_id: str
    prompt: str
    kind: Literal["approval"] = "approval"
    choices: list[Literal["once", "deny"]]
    description: str | None = None
    command: str | None = None


class ClarificationInput(Wire):
    request_id: str
    prompt: str
    kind: Literal["clarification"] = "clarification"


Pending = ApprovalInput | ClarificationInput


class RequestError(Wire):
    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True)

    error_class: str | None = Field(alias="class")
    code: str
    message: str


class RequestWaiting(Wire):
    reason: str | None
    until: str | None


class ChatRequest(Wire):
    request_id: str
    session_id: str
    scope: Scope
    origin: Literal["user", "runtime"]
    status: str
    created_at: str
    updated_at: str
    revision: int
    attempt: int
    in_history: bool
    error: RequestError | None
    waiting: RequestWaiting | None
    messages: list[dict[str, JsonValue]] | None = None
    message_omitted: Literal[True] | None = None


class Session(Wire):
    id: str
    parent_session_id: str | None
    started_at: str
    ended_at: str | None
    active_request_ids: list[str]
    pending: list[Pending]
    scope: Scope
    requests: list[ChatRequest]


class Record(Wire):
    id: str
    session_id: str
    request_id: str | None = None
    sequence: int
    message: dict[str, JsonValue]
    kind: Literal["message", "continuation", "failed_turn"]
    reasoning: str | None = None
    created_at: str | None = None


class Goal(Wire):
    id: str
    scope_id: str
    title: str
    description: str
    end_state: str
    status: str
    summary: str


class Task(Wire):
    id: str
    scope_id: str
    goal_id: str | None
    session_ids: list[str]
    title: str
    description: str
    summary: str
    status: str
    outcome: str | None
    waiting_on: Waiting | None


class Event(Wire):
    attempt: int
    seq: int
    at: str


class TextDelta(Event):
    kind: Literal["text.delta"] = "text.delta"
    completion_id: str
    delta: str


class TextInterim(Event):
    kind: Literal["text.interim"] = "text.interim"
    text: str
    already_streamed: bool
    truncated: Literal[True] | None = None


class Reasoning(Event):
    kind: Literal["reasoning"] = "reasoning"
    text: str | None = None
    truncated: Literal[True] | None = None


class ToolStarted(Event):
    kind: Literal["tool.started"] = "tool.started"
    call_id: str
    name: str
    arguments: str
    truncated: Literal[True] | None = None


class ToolCompleted(Event):
    kind: Literal["tool.completed"] = "tool.completed"
    call_id: str
    name: str


class Finished(Event):
    kind: Literal["finished"] = "finished"
    status: Literal["completed", "cancelled", "failed", "interrupted"]
    error: RequestError | None = None


ChatEvent = TextDelta | TextInterim | Reasoning | ToolStarted | ToolCompleted | Finished


class Log(BaseModel):
    """A page of one request's event log; ``closed`` once it holds ``finished``."""

    events: list[Annotated[ChatEvent, Field(discriminator="kind")]]
    more: bool
    closed: bool


class Binding(BaseModel):
    backend_id: UUID
    version: Literal["1.0.0"]
    max_message_bytes: Literal[1048576]


class Bound(BaseModel):
    binding: Binding


class Frame(BaseModel):
    frontend_id: UUID
    message: dict[str, JsonValue]
    trace_context: dict[str, str] = Field(default_factory=dict)


class Refusal(Exception):
    """An error the contract names: ``invalid`` is malformed parameters (-32602), every other
    code an application error; ``unknown`` marks an unresolved command."""

    def __init__(self, code: str, message: str, *, unknown: bool = False, **data: JsonValue):
        super().__init__(message)
        self.code, self.unknown, self.data = code, unknown, data

    def error(self) -> dict[str, JsonValue]:
        if self.code == "invalid":
            return {"code": -32602, "message": str(self)}
        data = {"code": self.code} | ({"outcome": "unknown"} if self.unknown else {}) | self.data
        return {"code": -32000, "message": str(self), "data": data}


async def events(response: httpx.Response) -> AsyncIterator[str]:
    """Each server-sent event's data; comments and keepalives carry none."""
    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            data.append(line[6:])
        elif not line and data:
            yield "\n".join(data)
            data = []


def cursor_number(part: str) -> int:
    """A number from a client's cursor. Anything this backend never issued is a typed refusal.

    An uncaught ``ValueError`` would end the backend, and ``str.isdigit`` alone lets through
    characters ``int`` rejects, such as a superscript. Twenty digits holds any issued number and
    stays under Python's limit on converting long strings.
    """
    if not (part.isascii() and part.isdigit() and len(part) <= 20):
        raise Refusal("snapshot_expired", "Cursor is not one this backend issued")
    return int(part)


def fitting(items: list[BaseModel]) -> int:
    """How many leading items fit in one 1 MiB reply beside its envelope and cursor."""
    total = 1024
    for count, item in enumerate(items):
        total += len(item.model_dump_json().encode()) + 1
        if total > MAX_BYTES:
            if count == 0:
                raise Refusal("too_large", "One record exceeds the 1 MiB message limit")
            return count
    return len(items)


def moment(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def capped(text: str) -> dict:
    """``text`` cut to fit one event, with ``truncated`` when it was cut."""
    if len(text) <= TEXT_CHARS:
        return {"text": text}
    return {"text": text[:TEXT_CHARS], "truncated": True}


def record(message: NativeMessage, session_id: str) -> Record | None:
    """A retained native row as a message record; hidden scaffolding has no record."""
    if message.display_kind == "hidden" or message.role not in {
        "system",
        "user",
        "assistant",
        "tool",
    }:
        return None
    fields = message.model_dump(include={"role", "content", "tool_calls", "tool_call_id"})
    body = {key: value for key, value in fields.items() if key == "content" or value is not None}
    kind = "message"
    if message.display_kind == "failed_turn":
        kind = "failed_turn"
    # The runtime writes its resume prompt itself, so no admission's context joins it; a message
    # the user typed with the same words carries its own admission's context.
    elif (
        message.role == "user"
        and message.client_context is None
        and isinstance(message.content, str)
        and message.content.startswith(CONTINUATION_PREFIX)
    ):
        kind = "continuation"
    return Record(
        id=str(message.id),
        session_id=session_id,
        sequence=message.id,
        message=body,
        kind=kind,
        reasoning=message.reasoning_content or message.reasoning or None,
        created_at=None if message.timestamp is None else moment(message.timestamp),
    )


def request_error(row: Admission) -> RequestError | None:
    """Why a request failed; only ``failed`` and ``interrupted`` requests have an error."""
    if row.status not in {"failed", "interrupted"}:
        return None
    return RequestError(
        error_class=row.error_class,
        code=row.error_class or row.status,
        message=(row.error or row.status)[:ERROR_CHARS],
    )


def chat_request(row: Admission, scope: Scope) -> ChatRequest:
    """A request record from its admission; ``waiting`` and ``error`` follow its status."""
    return ChatRequest(
        request_id=row.op_id,
        session_id=row.session_id,
        scope=scope,
        origin="runtime" if row.input is None else "user",
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        revision=row.revision,
        attempt=row.resumes,
        in_history=bool(row.runs),
        error=request_error(row),
        waiting=RequestWaiting(reason=row.error_class, until=row.resume_after)
        if row.status == "waiting"
        else None,
    )


class Progress:
    """One attempt's log position as its consumer places the gateway events of its runs.

    Each placed event takes the next ``seq``, so seqs are contiguous within the attempt. Text
    deltas between boundaries (a tool call, interim text) share one completion, and consecutive
    deltas merge into one event until another kind arrives or ``MERGE_SECONDS`` pass, which
    bounds the log's writes. Relayed reasoning merges the same way for ``REASONING_MERGE_SECONDS``
    and takes its place in the log where it arrives among the run's events. ``consumed`` is the
    last gateway seq read from each run, where a reconnected stream resumes; a pending merge holds
    the gateway seq it has folded in.
    """

    def __init__(self, attempt: int, seq: int = 0, completion: str | None = None):
        self.attempt, self.seq, self.completion = attempt, seq, completion
        self.consumed: dict[str, int] = {}  # run id -> the last gateway seq read from it
        self.pending: tuple[TextDelta | Reasoning, int] | None = None
        self.deadline = 0.0

    @classmethod
    def resumed(cls, logged: Logged) -> "Progress":
        """The attempt's position after its last logged event, as a restarted consumer resumes."""
        completion = logged.event["completion_id"] if logged.event["kind"] == "text.delta" else None
        progress = cls(logged.attempt, logged.seq, completion)
        progress.consumed[logged.run_id] = logged.source_seq
        return progress

    def patience(self) -> float | None:
        """Seconds until a pending merge is due, or ``None`` with nothing pending."""
        return None if self.pending is None else max(0.0, self.deadline - time.monotonic())

    def flush(self) -> list[tuple[ChatEvent, int]]:
        pending, self.pending = self.pending, None
        return [] if pending is None else [pending]

    def place(self, run_id: str, event: RunEvent) -> list[tuple[ChatEvent, int]]:
        """The events due for the log after ``event``, each with its last gateway seq."""
        if event.event == "message.delta" and event.delta:
            if self.pending is not None and isinstance(self.pending[0], TextDelta):
                self.pending[0].delta += event.delta
                self.pending = (self.pending[0], event.seq)
                return []
            flushed = self.flush()
            if self.completion is None:
                self.completion = f"{run_id}:{self.seq + 1}"
            self.seq += 1
            delta = TextDelta(
                attempt=self.attempt,
                seq=self.seq,
                at=moment(event.timestamp),
                completion_id=self.completion,
                delta=event.delta,
            )
            self.pending, self.deadline = (delta, event.seq), time.monotonic() + MERGE_SECONDS
            return flushed
        at = {"attempt": self.attempt, "seq": self.seq + 1, "at": moment(event.timestamp)}
        placed: ChatEvent | None = None
        if event.event == "message.interim" and event.text:
            placed = TextInterim(
                **at, **capped(event.text), already_streamed=event.already_streamed
            )
        elif event.event == "reasoning.available":
            placed = Reasoning(**at, **(capped(event.text) if event.text else {}))
        elif event.event in {"tool.started", "tool.completed"}:
            call = {"call_id": event.tool_call_id or event.tool, "name": event.tool}
            # The gateway streams a display preview of the call, never its JSON arguments;
            # the complete call is in chat.history.
            placed = (
                ToolStarted(
                    **at, **call, arguments=(event.preview or "")[:TEXT_CHARS], truncated=True
                )
                if event.event == "tool.started"
                else ToolCompleted(**at, **call)
            )
        if placed is None:
            return []
        flushed = self.flush()
        self.completion = None
        self.seq += 1
        return flushed + [(placed, event.seq)]

    def think(self, text: str, source_seq: int) -> list[tuple[ChatEvent, int]]:
        """The events due for the log after a relayed reasoning chunk, read at gateway
        ``source_seq``: a reasoning event pending its merge, which ends the text's completion."""
        if (
            self.pending is not None
            and isinstance(self.pending[0], Reasoning)
            and len(self.pending[0].text) + len(text) <= TEXT_CHARS
        ):
            self.pending[0].text += text
            self.pending = (self.pending[0], source_seq)
            return []
        flushed = self.flush()
        self.completion = None
        self.seq += 1
        thought = Reasoning(
            attempt=self.attempt, seq=self.seq, at=moment(time.time()), **capped(text)
        )
        self.pending = (thought, source_seq)
        self.deadline = time.monotonic() + REASONING_MERGE_SECONDS
        return flushed


class Subscription:
    def __init__(self, scope: Scope):
        self.scope = scope
        self.frontends: set[UUID] = set()


class BoardFeed:
    """One Coworld event stream, with an independent cursor per frontend and resource."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.readers: dict[tuple[UUID, str], int | None] = {}
        self.watcher: asyncio.Task[None] | None = None


class Backend:
    def __init__(
        self,
        seam: httpx.AsyncClient,
        gateway: httpx.AsyncClient,
        inference: Availability,
        work: asyncio.TaskGroup,
    ):
        self.seam, self.gateway, self.inference, self.work = seam, gateway, inference, work
        self.epoch, self.revision = str(uuid4()), 0
        self.socket: ClientConnection | None = None
        self.subscribers: dict[str, Subscription] = {}
        self.boards: dict[str, BoardFeed] = {}
        self.requests: dict[str, str] = {}  # admission op id -> session id
        self.wakes: dict[str, asyncio.Event] = {}
        self.consumers: dict[str, asyncio.Task[None]] = {}
        self.finished: set[str] = set()  # requests whose log this backend closed
        # Native Session id -> the relayed reasoning (text, lines lost before it) its follower logs.
        self.thinking: dict[str, asyncio.Queue[tuple[str, int]]] = {}
        self.relayed = 0  # the newest relay line's ``n``
        self.start: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    # --- native calls -----------------------------------------------------------------------

    async def call(
        self, client: httpx.AsyncClient, method: str, path: str, command: bool = False, **kwargs
    ) -> bytes:
        """A native reply's body, or the refusal its status means."""
        headers = dict(kwargs.pop("headers", {}))
        propagator.inject(headers)
        response = await client.request(
            method, path, timeout=SEAM_TIMEOUT_S, headers=headers, **kwargs
        )
        if response.is_success:
            return response.content
        if "op_id was already used for different input" in response.text:
            raise Refusal("id_reused", "This request id was used for different input")
        if response.status_code >= 500:
            raise Refusal("unavailable", response.text, unknown=command)
        if response.status_code == 409:
            # The seam's only scope check answers 409 scope_mismatch.
            code = "forbidden" if "scope_mismatch" in response.text else "conflict"
            raise Refusal(code, response.text)
        code = {400: "invalid", 404: "not_found"}.get(response.status_code, "unsupported")
        raise Refusal(code, response.text)

    async def snapshot(self, **params: str) -> Snapshot:
        """Top-level chat Sessions with their requests, as one consistent seam read."""
        snapshot = Snapshot.model_validate_json(
            await self.call(
                self.seam,
                "GET",
                "/snapshot",
                params={"store": "workspace", "requests": "1", **params},
            )
        )
        if not self.start.done():
            self.start.set_result(snapshot.seq)
        for request in snapshot.records.requests:
            self.requests[request.op_id] = request.session_id
        return snapshot

    async def scoped(self, scope: Scope, **selection: str) -> Snapshot:
        """One session's rows; the seam refuses a scope that differs from the stored one."""
        expected = scope.model_dump(include={"coworld", "league"}, exclude_none=True)
        return await self.snapshot(**selection, **expected)

    async def request(self, session_id: str, scope: Scope, request_id: str) -> Admission:
        snapshot = await self.scoped(scope, session_id=session_id)
        found = [r for r in snapshot.records.requests if r.op_id == request_id]
        if not found:
            raise Refusal("not_found", "No such chat request in this session")
        return found[0]

    async def pending(self, session_id: str, requests: list[Admission]):
        """The prompts waiting on this session's running requests, with their native ids."""
        found = []
        running = sorted(
            {
                r.runs[-1].run_id
                for r in requests
                if r.session_id == session_id and r.status == "running"
            }
        )
        for run_id in running:
            response = await self.gateway.get(f"v1/runs/{quote(run_id, safe='')}", timeout=30)
            if response.status_code == 404:
                continue  # a retired run has no prompts left
            if not response.is_success:
                raise Refusal("unavailable", response.text)
            for control in RunState.model_validate_json(response.content).pending_controls:
                identity = f"aide-input:{session_id}:{run_id}:{control.execution_generation}"
                request_id = str(uuid5(NAMESPACE_URL, f"{identity}:{control.prompt_id}"))
                prompt = control.description or control.command or control.question
                pending = (
                    ApprovalInput(
                        request_id=request_id,
                        prompt=prompt,
                        choices=[c for c in control.choices if c in {"once", "deny"}],
                        description=control.description or None,
                        command=control.command or None,
                    )
                    if control.kind == "approval"
                    else ClarificationInput(request_id=request_id, prompt=prompt)
                )
                found.append((pending, run_id, control))
        return found

    async def messages(self, session_id: str, limit: int, offset: int) -> list[NativeMessage]:
        """Native messages newest first, ``offset`` counted from the newest, with their admissions'
        context; empty until the session's native opening completes, and for a session the gateway
        does not have, whose store record and requests still stand. The gateway pages from the
        newest but returns each page oldest first."""
        opening = await self.call(
            self.seam, "GET", "/session-pending", params={"session_id": session_id}
        )
        if Opening.model_validate_json(opening).pending:
            return []
        headers: dict[str, str] = {}
        propagator.inject(headers)
        response = await self.gateway.get(
            f"api/sessions/{quote(session_id, safe='')}/messages",
            params={"order": "latest", "limit": limit, "offset": offset},
            headers=headers,
            timeout=30,
        )
        if response.status_code == 404:
            return []
        if not response.is_success:
            raise Refusal("unavailable", response.text)
        native = NativePage.model_validate_json(response.content).data[::-1]
        if not native:
            return []
        annotated = await self.call(
            self.seam, "POST", "/messages/context", json={"session_id": session_id, "data": native}
        )
        return [
            NativeMessage.model_validate(m) for m in NativePage.model_validate_json(annotated).data
        ]

    async def project(self, row: SessionRow, requests: list[Admission]) -> Session:
        """A session from one snapshot, so its requests and active ids always agree."""
        scope = row.scope()
        mine = [r for r in requests if r.session_id == row.id]
        active = [r.op_id for r in mine if r.status in ACTIVE]
        recent = sorted((r for r in mine if r.status in TERMINAL), key=lambda r: r.updated_at)
        embedded = set(active[:EMBEDDED_ACTIVE]) | {r.op_id for r in recent[-RECENT_TERMINAL:]}
        return Session(
            id=row.id,
            parent_session_id=row.parent,
            started_at=row.started,
            ended_at=row.ended,
            active_request_ids=active,
            pending=[pending for pending, _, _ in await self.pending(row.id, mine)],
            scope=scope,
            requests=[chat_request(r, scope) for r in mine if r.op_id in embedded],
        )

    async def board(
        self, coworld: str, resource: str, item_id: str | None = None
    ) -> tuple[int, list[BaseModel]]:
        params = {"store": "coworld", "coworld": coworld, "board": resource}
        if item_id is not None:
            params["item"] = item_id
        board = Board.model_validate_json(
            await self.call(self.seam, "GET", "/snapshot", params=params)
        )
        if resource == "goals":
            return board.seq, [
                Goal(
                    id=f"{coworld}:{row.id}",
                    scope_id=coworld,
                    title=row.title,
                    description=row.description,
                    end_state=row.end_state,
                    status=row.status,
                    summary=row.where_it_stands,
                )
                for row in board.records.goals
            ]
        return board.seq, [
            Task(
                id=f"{coworld}:{row.id}",
                scope_id=coworld,
                goal_id=None if row.goal_id is None else f"{coworld}:G-{row.goal_id}",
                session_ids=[row.held_by] if row.held_by else [],
                title=row.title,
                description=row.description,
                summary=row.result or row.for_human,
                status=TASK_STATUSES[row.status],
                outcome=row.outcome,
                waiting_on=row.waiting_on,
            )
            for row in board.records.tasks
        ]

    async def subscribe_board(
        self, frontend_id: UUID, coworld: str, resource: str
    ) -> tuple[int, list[BaseModel]]:
        """Install the list cursor before the feed can pass it."""
        feed = self.boards.setdefault(coworld, BoardFeed())
        reader = (frontend_id, resource)
        feed.readers.setdefault(reader, None)
        try:
            async with feed.lock:
                revision, rows = await self.board(coworld, resource)
                if reader in feed.readers:  # the frontend can close during the snapshot
                    feed.readers[reader] = revision
                    if feed.watcher is None:
                        feed.watcher = self.work.create_task(
                            self.watch_board(coworld, feed, revision)
                        )
                return revision, rows
        finally:
            if reader in feed.readers and feed.readers[reader] is None:
                del feed.readers[reader]
                if not feed.readers:
                    del self.boards[coworld]

    async def watch_board(self, coworld: str, feed: BoardFeed, after: int) -> None:
        """Follow durable board events while at least one frontend has a scoped inventory."""
        while True:
            try:
                async with self.seam.stream(
                    "GET",
                    "/stream",
                    params={"store": "coworld", "coworld": coworld, "after": after},
                    timeout=httpx.Timeout(10, read=None),
                ) as response:
                    if response.status_code < 500:
                        response.raise_for_status()
                        async for data in events(response):
                            event = SeamEvent.model_validate_json(data)
                            await self.publish_board(coworld, feed, event)
                            after = event.seq
            except (httpx.TransportError, Refusal) as error:
                if isinstance(error, Refusal) and error.code != "unavailable":
                    raise
                log.info("Coworld board stream dropped; resuming after %s", after)
            await asyncio.sleep(1)

    async def publish_board(self, coworld: str, feed: BoardFeed, event: SeamEvent) -> None:
        kind = event.kind.partition(".")[0]
        prefix = {"task": "T-", "goal": "G-"}.get(kind)
        if prefix is None or not event.subject.startswith(prefix):
            return
        resource = kind + "s"
        async with feed.lock:
            readers = {
                reader: revision
                for reader, revision in feed.readers.items()
                if reader[1] == resource and revision is not None and revision < event.seq
            }
            if not readers:
                return
            _, items = await self.board(coworld, resource, event.subject)
            socket = self.socket
            if socket is None:
                return
            # A frontend may close during the projection. Do not send to it or restore its cursor.
            readers = {
                reader: previous for reader, previous in readers.items() if reader in feed.readers
            }
            for reader in readers:
                feed.readers[reader] = event.seq

        change = (
            {"change": "upsert", "item": items[0].model_dump(mode="json")}
            if items
            else {"change": "delete", "id": f"{coworld}:{event.subject}"}
        )
        for (frontend_id, _), previous in readers.items():
            await self.send(
                socket,
                frontend_id,
                {
                    "jsonrpc": "2.0",
                    "method": "state.changed",
                    "params": {
                        "resource": resource,
                        "epoch": "coworld",
                        "revision": event.seq,
                        "previous_revision": previous,
                        **change,
                    },
                },
            )

    # --- notifications ----------------------------------------------------------------------

    async def touch(self, session_id: str, scope: Scope, frontend_id: UUID) -> None:
        """Subscribe a frontend to a session, once this is the session's own scope.

        ``changed`` reads with the recorded scope, and a failure there ends the backend, so a
        wrong scope is refused to its request and leaves nothing behind. Every caller reads what
        it returns after this, so its reply holds any change made before the subscription began.
        """
        subscription = self.subscribers.get(session_id)
        if subscription is None or subscription.scope != scope:
            (row,) = (await self.scoped(scope, session_id=session_id)).records.sessions
            # The seam compares only Coworld and league; a Coworld session with no league has no
            # scope, and projecting one fails validation.
            if row.scope() != scope:
                raise Refusal("forbidden", "This session does not have that scope")
        self.subscribers.setdefault(session_id, Subscription(scope)).frontends.add(frontend_id)

    async def send(self, socket: ClientConnection, frontend_id: UUID, message: dict) -> None:
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode()) > MAX_BYTES:
            if "id" not in message:
                log.warning("AIDE notification over 1 MiB dropped: %s", message["method"])
                return
            refusal = Refusal("too_large", "The result exceeds the 1 MiB message limit")
            body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": refusal.error()})
        try:
            await socket.send(f'{{"frontend_id":"{frontend_id}","message":{body}}}')
        except ConnectionClosed:  # the relay connection ended; it reports the outcome as unknown
            log.info("AIDE delivery lost with its connection")

    async def notify(self, session_id: str, method: str, params: BaseModel | dict) -> None:
        if self.socket is None or session_id not in self.subscribers:
            return
        if isinstance(params, BaseModel):
            params = params.model_dump(mode="json")
        for frontend_id in list(self.subscribers[session_id].frontends):
            message = {"jsonrpc": "2.0", "method": method, "params": params}
            await self.send(self.socket, frontend_id, message)

    async def changed(self, session_id: str) -> None:
        if session_id not in self.subscribers or not self.subscribers[session_id].frontends:
            return
        snapshot = await self.scoped(self.subscribers[session_id].scope, session_id=session_id)
        (row,) = snapshot.records.sessions
        session = await self.project(row, snapshot.records.requests)
        self.revision += 1
        change = {
            "resource": "sessions",
            "epoch": self.epoch,
            "previous_revision": self.revision - 1,
            "revision": self.revision,
            "change": "upsert",
            "item": session.model_dump(mode="json"),
        }
        await self.notify(session_id, "state.changed", change)

    async def watch(self) -> None:
        """Recover every open event log's consumer, then follow the workspace store's events from
        the first snapshot this backend served; a new message admission gets its consumer."""
        after: int | None = None
        while True:
            try:
                if after is None:  # the runtime starts this backend once the seam is healthy
                    await self.recover()
                    after = await self.start
                async with self.seam.stream(
                    "GET",
                    "/stream",
                    params={"store": "workspace", "after": after},
                    timeout=httpx.Timeout(10, read=None),
                ) as response:
                    if response.status_code >= 500:  # the seam is restarting; read again
                        log.info("Seam event stream refused (%s)", response.status_code)
                    else:
                        response.raise_for_status()
                        async for data in events(response):
                            event = SeamEvent.model_validate_json(data)
                            after = event.seq
                            if event.kind == "client.accepted":
                                self.requests[event.subject] = str(event.payload["session_id"])
                            if (
                                event.kind == "client.accepted"
                                and event.payload["kind"] == "message"
                            ) or event.kind == "execution.queued":
                                await self.recover()
                            if event.subject in self.wakes:
                                self.wakes[event.subject].set()
                            if event.kind.startswith("session."):
                                await self.changed(event.subject)
                            elif (
                                event.kind.startswith("client.") and event.subject in self.requests
                            ):
                                await self.changed(self.requests[event.subject])
            except httpx.TransportError:  # the seam stream dropped; resume after the last event
                log.info("Seam event stream dropped; resuming after %s", after)
            await asyncio.sleep(1)

    async def publish(self, request_id: str, session_id: str, after: int) -> int:
        """Announce native messages newer than ``after``; return the newest announced id."""
        fresh: list[NativeMessage] = []
        offset = 0
        while True:
            page = await self.messages(session_id, 100, offset)
            newer = [m for m in page if m.id > after]
            fresh = list(reversed(newer)) + fresh
            if len(newer) < 100:
                break
            offset += 100
        for native in fresh:
            visible = record(native, session_id)
            if visible is not None:
                visible.request_id = request_id
                await self.notify(session_id, "chat.message", visible)
            after = native.id
        return after

    async def record(
        self, request: Admission, run_id: str | None, placed: list[tuple[ChatEvent, int | None]]
    ) -> None:
        """Append each event to the request's log, then push it at the cursor it was stored at."""
        note = {"session_id": request.session_id, "request_id": request.op_id}
        for event, source_seq in placed:
            body = {
                "op_id": request.op_id,
                "event": event.model_dump(mode="json"),
                "run_id": run_id,
                "source_seq": source_seq,
                "actor": "user:aide",
            }
            try:
                await self.call(self.seam, "POST", "/message-events", json=body)
            except httpx.TransportError as error:
                # No answer says nothing about the commit, and ``place`` has already moved past
                # this event, so following on would append after a gap. This fails like a 5xx:
                # the backend ends and the next one resumes from what the log holds.
                raise Refusal("unavailable", type(error).__name__, unknown=True) from error
            await self.notify(request.session_id, "chat.event", note | {"event": body["event"]})
            if isinstance(event, TextDelta):
                chunk = {
                    "id": event.completion_id,
                    "object": "chat.completion.chunk",
                    "created": int(datetime.fromisoformat(event.at).timestamp()),
                    "model": "",
                    "choices": [{"index": 0, "delta": {"content": event.delta}}],
                }
                # A completion's deltas are consecutive events and its id names the first one's
                # seq, so this is the chunk's place in its completion, counted from one.
                first = int(event.completion_id.rsplit(":", 1)[1])
                params = note | {
                    "completion_id": event.completion_id,
                    "seq": event.seq - first + 1,
                    "cursor": {"attempt": event.attempt, "seq": event.seq},
                    "chunk": chunk,
                }
                await self.notify(request.session_id, "chat.chunk", params)

    async def relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """The gateway plugin's reasoning lines, each to the follower of the run that serves its
        Session. A Session runs one request at a time, so this is that request's. A line for no
        followed run (a subagent's, a wake's, or one that arrived after its run ended) is dropped.
        """
        try:
            async for line in reader:
                thought = Thought.model_validate_json(line)
                # ``n`` starts again from one with a new gateway, which loses nothing here.
                lost = max(0, thought.n - self.relayed - 1)
                self.relayed = thought.n
                for session in thought.sessions:
                    if session in self.thinking:
                        self.thinking[session].put_nowait((thought.text, lost))
                        break
        finally:
            writer.close()

    async def follow(
        self, request: Admission, run: Run, progress: Progress, after: int
    ) -> tuple[bool, int]:
        """Log and push one run's events after those already consumed, until its stream ends.

        Returns whether the run is done here (terminal, or gone from the gateway) and the newest
        announced message id. A pending merge is logged when it is due and whenever the stream
        ends; a failure that ends the consumer loses it. Reasoning the plugin relays for the run's
        Session is logged where it arrives; each follow records how much as a
        ``workspace.aide.reasoning`` span, without the text.
        """
        session_id, request_id = request.session_id, request.op_id
        last_seq = progress.consumed.setdefault(run.run_id, -1)
        thoughts: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        native: str | None = None  # the run's Session, once the gateway named it
        relayed = lost = reasoned = 0

        async def record(placed: list[tuple[ChatEvent, int]]) -> None:
            nonlocal reasoned
            reasoned += sum(isinstance(event, Reasoning) for event, _ in placed)
            await self.record(request, run.run_id, placed)

        try:
            while True:
                try:
                    state = await self.gateway.get(
                        f"v1/runs/{quote(run.run_id, safe='')}", timeout=30
                    )
                    if state.status_code == 404:
                        # Gone with its gateway: the seam settles the request as interrupted.
                        await record(progress.flush())
                        return True, after
                    state.raise_for_status()
                    native = RunSession.model_validate_json(state.content).session_id
                    self.thinking[native] = thoughts
                    async with self.gateway.stream(
                        "GET",
                        f"v1/runs/{quote(run.run_id, safe='')}/events",
                        params={"last_seq": last_seq},
                        timeout=httpx.Timeout(30, read=None),
                    ) as response:
                        if response.status_code == 404:
                            await record(progress.flush())
                            return True, after
                        response.raise_for_status()
                        progressed = False
                        lines = events(response)
                        following: asyncio.Task[str | None] | None = None
                        thought: asyncio.Task[tuple[str, int]] | None = None
                        try:
                            while True:
                                if following is None:
                                    following = asyncio.ensure_future(anext(lines, None))
                                if thought is None:
                                    thought = asyncio.ensure_future(thoughts.get())
                                ready, _ = await asyncio.wait(
                                    {following, thought},
                                    timeout=progress.patience(),
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                if not ready:  # a merged event is due
                                    await record(progress.flush())
                                    continue
                                if thought in ready:
                                    (text, gap), thought = thought.result(), None
                                    relayed, lost = relayed + 1, lost + gap
                                    await record(progress.think(text, last_seq))
                                if following not in ready:
                                    continue
                                data, following = following.result(), None
                                if data is None:
                                    break
                                event = RunEvent.model_validate_json(data)
                                if event.event == "replay.truncated":
                                    # The consumer fell behind the gateway's buffer: those events
                                    # were never logged, and the log continues after them.
                                    log.warning("Run %s events lost before logging", run.run_id)
                                    continue
                                if event.seq is None or event.seq <= last_seq:
                                    continue
                                last_seq, progressed = event.seq, True
                                progress.consumed[run.run_id] = last_seq
                                await record(progress.place(run.run_id, event))
                                if event.event == "tool.completed":
                                    after = await self.publish(request_id, session_id, after)
                                elif event.event in CONTROLS:
                                    await self.changed(session_id)
                                elif event.event.removeprefix("run.") in TERMINAL:
                                    await record(progress.flush())
                                    return True, after
                        finally:  # the pending reads end before their stream closes
                            pending = {task for task in (following, thought) if task is not None}
                            for task in pending:
                                task.cancel()
                            if pending:
                                await asyncio.wait(pending)
                        await record(progress.flush())
                        if not progressed:
                            return False, after  # a quiet closed stream; the consumer follows again
                # The run stream dropped, or a read for a push failed; ``record`` lets no failed
                # append reach here. Resume after last_seq.
                except httpx.TransportError:
                    await asyncio.sleep(1)
        finally:
            # Reasoning relayed after this, the run's end among it, has no place in the log.
            if native is not None and self.thinking.get(native) is thoughts:
                del self.thinking[native]
            attributes = {
                "run.id": run.run_id,
                "reasoning.relayed": relayed,
                "reasoning.lost": lost,
                "reasoning.events": reasoned,
            }
            tracer().start_span("workspace.aide.reasoning", attributes=attributes).end()

    async def consume(self, request_id: str, session_id: str, scope: Scope) -> None:
        """The only writer of one request's event log: its runs' events in order, then its
        outcome as the log's last event once the seam settles it.

        It resumes after the log's last row, so a restarted backend neither repeats nor skips
        an event the log holds. The gateway restarts with the backend, so its buffer does not
        cover the gap. Any failure here but a dropped run stream or a failed read for a push ends
        the backend's task group. A steered request's run is another request's, whose log holds
        the stream: this log holds only the outcome.
        """
        wake = self.wakes[request_id]
        request = await self.request(session_id, scope, request_id)
        attempts: dict[int, Progress] = {}
        done: set[str] = set()
        if request.logged is not None:
            attempts[request.logged.attempt] = Progress.resumed(request.logged)
            ran = [run.run_id for run in request.runs]
            done.update(ran[: ran.index(request.logged.run_id)])
        newest = await self.messages(session_id, 1, 0)
        after = newest[0].id if newest else 0
        try:
            while True:
                wake.clear()
                request = await self.request(session_id, scope, request_id)
                runs = [r for r in request.runs if r.run_id not in done and not r.steered]
                if runs:
                    run = runs[0]
                    progress = attempts.setdefault(run.attempt, Progress(run.attempt))
                    finished, after = await self.follow(request, run, progress, after)
                    if finished:
                        done.add(run.run_id)
                    elif request.status in ACTIVE:
                        await asyncio.sleep(1)
                    continue
                if request.status in TERMINAL:
                    # A steered request's messages are its host run's, which that consumer
                    # announces; read from this watermark they would repeat under this id.
                    if not any(r.steered for r in request.runs):
                        after = await self.publish(request_id, session_id, after)
                    final = attempts.get(request.resumes, Progress(request.resumes))
                    outcome = Finished(
                        attempt=request.resumes,
                        seq=final.seq + 1,
                        at=moment(time.time()),
                        status=request.status,
                        error=request_error(request),
                    )
                    await self.record(request, None, [(outcome, None)])
                    self.finished.add(request_id)
                    finished = {
                        "session_id": session_id,
                        "request_id": request_id,
                        "status": outcome.status,
                        "cursor": {"attempt": outcome.attempt, "seq": outcome.seq},
                        "revision": request.revision,
                    }
                    if outcome.error is not None:
                        finished["error"] = outcome.error.model_dump(mode="json")
                    await self.notify(session_id, "chat.finished", finished)
                    return
                await wake.wait()
        finally:
            del self.wakes[request_id]

    def ensure_consumer(self, request_id: str, session_id: str, scope: Scope) -> None:
        if request_id in self.consumers or request_id in self.finished:
            return
        self.requests[request_id] = session_id
        self.wakes[request_id] = asyncio.Event()
        consumer = self.work.create_task(self.consume(request_id, session_id, scope))
        self.consumers[request_id] = consumer
        consumer.add_done_callback(lambda _: self.consumers.pop(request_id))

    async def recover(self) -> None:
        """Consume every request whose event log is not closed yet, open or newly terminal."""
        snapshot = await self.snapshot()
        scopes = {row.id: row.scope() for row in snapshot.records.sessions}
        for request in snapshot.records.requests:
            scope = scopes[request.session_id]
            closed = request.logged is not None and request.logged.event["kind"] == "finished"
            if not closed and scope is not None:
                self.ensure_consumer(request.op_id, request.session_id, scope)

    # --- methods ------------------------------------------------------------------------------

    async def open(self, frontend_id: UUID, request_id: str, params: Open) -> dict:
        if params.scope.scope == "coworld" and params.scope.league is None:
            # The seam would store this as a session with no AIDE scope, and start its opening.
            raise Refusal("forbidden", "A Coworld session needs a league")
        client = (
            DEFAULT_CLIENT if params.client is None else params.client.model_dump(exclude_none=True)
        )
        body = {
            "op_id": request_id,
            "client": client,
            "actor": "user:aide",
            "session_context": {"contract_version": 1, **params.scope.model_dump()},
        }
        opened = Opened.model_validate_json(
            await self.call(self.seam, "POST", "/session-open", command=True, json=body)
        )
        await self.touch(opened.session_id, params.scope, frontend_id)
        snapshot = await self.scoped(params.scope, session_id=opened.session_id)
        (row,) = snapshot.records.sessions
        session = await self.project(row, snapshot.records.requests)
        return {"session": session.model_dump(mode="json")}

    async def session_page(self, frontend_id: UUID, request_id: str, params: SessionPage) -> dict:
        await self.touch(params.session_id, params.scope, frontend_id)
        # Related uploads are page context for grouping. Every session keeps its own checked scope.
        uploads = {params.page.coworld, *params.related_uploads}
        if params.scope.coworld is None or (
            params.scope.coworld in uploads and params.scope.league == params.page.league
        ):
            return {"kind": "stay"}
        confirmed = "target_session_id" in params.model_fields_set
        if confirmed and params.target_session_id is None:
            # Confirmations for an empty target converge on one creation, even after a lost reply.
            scope = Scope.model_validate(params.page.model_dump())
            open_id = str(uuid5(NAMESPACE_URL, f"aide:session.page:{scope.model_dump_json()}"))
            opened = await self.open(
                frontend_id, open_id, Open(scope=scope, client=Client(id="softmax", kind="web"))
            )
            return {"kind": "switched", **opened}
        snapshot = await self.snapshot()
        candidates = [
            row
            for row in snapshot.records.sessions
            if row.parent is None
            and row.coworld_id in uploads
            and (scope := row.scope()) is not None
            and scope.league == params.page.league
        ]
        if confirmed:
            target = next((row for row in candidates if row.id == params.target_session_id), None)
            if target is None:
                raise Refusal("not_found", "No such session for the viewed Coworld")
            session = await self.project(target, snapshot.records.requests)
            return {"kind": "switched", "session": session.model_dump(mode="json")}

        activity = {}
        for request in snapshot.records.requests:
            if request.input is not None:
                activity[request.session_id] = max(
                    activity.get(request.session_id, ""), request.created_at
                )
        target = max(
            candidates,
            key=lambda row: (max(row.started, activity.get(row.id, "")), row.started, row.id),
            default=None,
        )
        return {"kind": "offer", "target_session_id": target.id if target else None}

    async def send_chat(self, frontend_id: UUID, request_id: str, params: Send) -> dict:
        await self.touch(params.session_id, params.scope, frontend_id)
        if len(params.messages) != 1 or params.messages[0]["role"] != "user":
            raise Refusal("unsupported", "Send exactly one user message")
        snapshot = await self.scoped(params.scope, session_id=params.session_id)
        replay = any(r.op_id == request_id for r in snapshot.records.requests)
        if not replay and not self.inference.available():
            blocking = self.inference.blocking()
            reason = blocking.blocked if blocking else None
            until = blocking.until if blocking else None
            raise Refusal(
                "inference_unavailable",
                "Model work cannot start now",
                reason=reason,
                until=None if until is None else until.isoformat(),
            )
        body = {
            "op_id": request_id,
            "session_id": params.session_id,
            "message": params.messages[0],
            "actor": "user:aide",
        } | params.model_dump(mode="json", include={"context", "instructions"}, exclude_none=True)
        await self.call(self.seam, "POST", "/message", command=True, json=body)
        if not replay:
            self.ensure_consumer(request_id, params.session_id, params.scope)
        return {"accepted": True, "session_id": params.session_id, "request_id": request_id}

    async def read_events(self, frontend_id: UUID, request_id: str, params: Events) -> dict:
        await self.touch(params.session_id, params.scope, frontend_id)
        await self.request(params.session_id, params.scope, str(params.request_id))
        query = {
            "op_id": str(params.request_id),
            "attempt": params.after.attempt,
            "seq": params.after.seq,
            "limit": params.limit,
        }
        found = Log.model_validate_json(
            await self.call(self.seam, "GET", "/message-events", params=query)
        )
        page = found.events[: fitting(found.events)]
        last = page[-1] if page else None
        return {
            "session_id": params.session_id,
            "request_id": str(params.request_id),
            "events": [event.model_dump(mode="json") for event in page],
            "next": params.after.model_dump()
            if last is None
            else {"attempt": last.attempt, "seq": last.seq},
            "done": found.closed and not found.more and len(page) == len(found.events),
        }

    async def history(self, frontend_id: UUID, request_id: str, params: History) -> dict:
        """Backwards from the newest record, or forward after ``after_sequence``.

        A backwards cursor is ``<offset>:<sequence>``: the records older than ``sequence``, whose
        newest sat at native ``offset`` when it was issued. Newer messages only push it further
        down, so the offset is a starting hint and the sequence the boundary.
        """
        await self.touch(params.session_id, params.scope, frontend_id)
        await self.scoped(params.scope, session_id=params.session_id)
        if params.cursor is not None and params.after_sequence is not None:
            raise Refusal("invalid", "cursor and after_sequence exclude each other")
        offset, boundary = 0, None
        if params.cursor is not None:
            parts = params.cursor.split(":")
            if len(parts) != 2:
                raise Refusal("snapshot_expired", "History cursor is not one this backend issued")
            offset, boundary = cursor_number(parts[0]), cursor_number(parts[1])
        floor = params.after_sequence
        # (record, native offset after it), newest first.
        items: list[tuple[Record, int]] = []
        exhausted = False
        while not exhausted and (floor is not None or len(items) < params.limit):
            page = await self.messages(params.session_id, 100, offset)
            exhausted = len(page) < 100
            for message in page:
                offset += 1
                if floor is not None and message.id <= floor:
                    exhausted = True
                    break
                visible = record(message, params.session_id)
                if visible is not None and (boundary is None or message.id < boundary):
                    items.append((visible, offset))
        if floor is not None:
            forward = [item for item, _ in reversed(items)][: params.limit]
            page = forward[: fitting(forward)]
            return {"items": [item.model_dump(mode="json") for item in page], "next_cursor": None}
        newest = items[: params.limit]
        kept = newest[: fitting([item for item, _ in newest])]
        more = len(kept) < len(items) or not exhausted
        following = f"{kept[-1][1]}:{kept[-1][0].sequence}" if kept and more else None
        return {
            "items": [item.model_dump(mode="json") for item, _ in reversed(kept)],
            "next_cursor": following,
        }

    async def cancel_chat(self, frontend_id: UUID, request_id: str, params: ChatCancel) -> dict:
        await self.touch(params.session_id, params.scope, frontend_id)
        target = await self.request(params.session_id, params.scope, str(params.request_id))
        body = {"op_id": str(params.request_id), "actor": "user:aide"}
        await self.call(self.seam, "POST", "/message-cancel", command=True, json=body)
        return {"requested": target.status in ACTIVE}

    async def list_state(self, frontend_id: UUID, request_id: str, params: StateList) -> dict:
        epoch = {"sessions": self.epoch, "requests": "workspace"}.get(params.resource, "coworld")
        if params.resource == "requests":
            if params.session_id is None or params.scope is None:
                raise Refusal("invalid", "Requests are listed per session and scope")
            await self.touch(params.session_id, params.scope, frontend_id)
            snapshot = await self.scoped(params.scope, session_id=params.session_id)
            revision = snapshot.seq
            rows = [chat_request(r, params.scope) for r in reversed(snapshot.records.requests)]
        elif params.session_id is not None:
            raise Refusal("invalid", "session_id selects requests only")
        elif params.resource == "sessions":
            snapshot = await self.snapshot()
            revision = self.revision
            rows = [
                row
                for row in snapshot.records.sessions
                if (scope := row.scope()) is not None
                and (params.scope is None or scope == params.scope)
                and (params.scope_id is None or scope.coworld == params.scope_id)
            ]
        else:
            coworld = params.scope.coworld if params.scope else params.scope_id
            if coworld is None:
                raise Refusal("unsupported", "Tasks and goals are listed per Coworld")
            if params.cursor is None:
                revision, rows = await self.subscribe_board(frontend_id, coworld, params.resource)
            else:
                revision, rows = await self.board(coworld, params.resource)
        selection = params.model_dump_json(include={"resource", "scope_id", "scope", "session_id"})
        key = hashlib.sha256(selection.encode()).hexdigest()[:16]
        offset = 0
        if params.cursor is not None:
            parts = params.cursor.split(":")
            if len(parts) != 3 or parts[0] != str(revision) or parts[2] != key:
                raise Refusal("snapshot_expired", "The listing changed; read it again")
            offset = cursor_number(parts[1])
        page = rows[offset : offset + params.limit]
        if params.resource == "sessions":
            page = [await self.project(row, snapshot.records.requests) for row in page]
        count = fitting(page)
        following = offset + count
        return {
            "resource": params.resource,
            "epoch": epoch,
            "revision": revision,
            "items": [item.model_dump(mode="json") for item in page[:count]],
            "next_cursor": f"{revision}:{following}:{key}" if following < len(rows) else None,
        }

    async def get_state(self, frontend_id: UUID, request_id: str, params: StateGet) -> dict:
        epoch = {"sessions": self.epoch, "requests": "workspace"}.get(params.resource, "coworld")
        if params.resource == "sessions":
            await self.touch(params.id, params.scope, frontend_id)
            snapshot = await self.scoped(params.scope, session_id=params.id)
            (row,) = snapshot.records.sessions
            item, revision = await self.project(row, snapshot.records.requests), self.revision
        elif params.resource == "requests":
            found = await self.scoped(params.scope, request_id=params.id)
            (session,) = found.records.sessions
            await self.touch(session.id, params.scope, frontend_id)
            snapshot = await self.scoped(params.scope, request_id=params.id)
            (row,) = [r for r in snapshot.records.requests if r.op_id == params.id]
            item = chat_request(row, params.scope)
            if row.input is not None and not item.in_history:
                item.messages = [row.input]
                if len(item.model_dump_json().encode()) > MAX_BYTES - 1024:
                    item.messages, item.message_omitted = None, True
            revision = row.revision
        else:
            if params.scope.coworld is None:
                raise Refusal("unsupported", "Tasks and goals belong to a Coworld")
            revision, items = await self.board(params.scope.coworld, params.resource)
            found = [item for item in items if item.id == params.id]
            if not found:
                raise Refusal("not_found", f"No such {params.resource[:-1]}")
            (item,) = found
        result = {"resource": params.resource, "epoch": epoch, "revision": revision}
        return result | {"item": item.model_dump(mode="json")}

    async def cancel_task(self, frontend_id: UUID, request_id: str, params: TaskCancel) -> dict:
        coworld = params.scope.coworld
        if coworld is None or not params.task_id.startswith(f"{coworld}:"):
            raise Refusal("forbidden", "The task belongs to another scope")
        _, tasks = await self.board(coworld, "tasks")
        found = [task for task in tasks if task.id == params.task_id]
        if not found:
            raise Refusal("not_found", "No such task")
        if found[0].status in {"completed", "abandoned"}:
            return {"requested": False}
        body = {
            "actor": "user:aide",
            "op": "tasks.abandon",
            "coworld": coworld,
            "store": "coworld",
            "args": {
                "task_id": params.task_id.removeprefix(f"{coworld}:"),
                "failure": {"what_failed": "Cancelled by user", "next_step": "None requested"},
            },
        }
        await self.call(self.seam, "POST", "/store/apply", command=True, json=body)
        return {"requested": True}

    async def respond(self, frontend_id: UUID, request_id: str, params: Respond) -> dict:
        await self.touch(params.session_id, params.scope, frontend_id)
        # The gateway forgets a prompt once it is resolved, so the seam keeps the replay record.
        request = params.model_dump(mode="json")
        applied = Applied.model_validate_json(
            await self.call(self.seam, "GET", "/input-response", params={"op_id": request_id})
        )
        if applied.request is not None:
            if applied.request != request:
                raise Refusal("id_reused", "This request id was used for different input")
            return {"applied": True}
        snapshot = await self.scoped(params.scope, session_id=params.session_id)
        found = [
            (run_id, control)
            for pending, run_id, control in await self.pending(
                params.session_id, snapshot.records.requests
            )
            if pending.request_id == str(params.request_id) and pending.kind == params.response.kind
        ]
        if not found:
            raise Refusal("conflict", "That input is no longer pending")
        ((run_id, control),) = found
        answer = params.response.model_dump(exclude={"kind"})
        action = "approval" if control.kind == "approval" else "clarify"
        headers = {"Idempotency-Key": request_id}
        propagator.inject(headers)
        response = await self.gateway.post(
            f"v1/runs/{quote(run_id, safe='')}/{action}",
            json={
                "request_id": control.prompt_id,
                "execution_generation": control.execution_generation,
                **answer,
            },
            headers=headers,
            timeout=30,
        )
        if response.status_code == 400:
            raise Refusal("invalid", response.text)
        if response.status_code in {404, 409} or (
            response.is_success
            and Resolution.model_validate_json(response.content).status != "resolved"
        ):
            raise Refusal("conflict", "That input is no longer pending")
        if not response.is_success:
            raise Refusal("unavailable", response.text, unknown=True)
        record = {"op_id": request_id, "request": request, "actor": "user:aide"}
        await self.call(self.seam, "POST", "/input-response", command=True, json=record)
        return {"applied": True}

    # --- the connection -----------------------------------------------------------------------

    async def answer(self, socket: ClientConnection, frame: Frame) -> None:
        with span("workspace.aide.request", context=propagator.extract(frame.trace_context)):
            message = frame.message
            request_id, method = str(message["id"]), str(message["method"])
            handlers = {
                "session.open": (Open, self.open),
                "session.page": (SessionPage, self.session_page),
                "chat.send": (Send, self.send_chat),
                "chat.history": (History, self.history),
                "chat.events": (Events, self.read_events),
                "chat.cancel": (ChatCancel, self.cancel_chat),
                "state.list": (StateList, self.list_state),
                "state.get": (StateGet, self.get_state),
                "task.cancel": (TaskCancel, self.cancel_task),
                "input.respond": (Respond, self.respond),
            }
            reply: dict[str, JsonValue] = {"jsonrpc": "2.0", "id": request_id}
            if method not in handlers:
                reply["error"] = Refusal("unsupported", f"{method} is not supported").error()
                await self.send(socket, frame.frontend_id, reply)
                return
            model, handler = handlers[method]
            try:
                params = model.model_validate(message["params"])
            except ValidationError as error:  # the relay checked the contract; these are our limits
                reply["error"] = {"code": -32602, "message": str(error)}
                await self.send(socket, frame.frontend_id, reply)
                return
            try:
                reply["result"] = await handler(frame.frontend_id, request_id, params)
            except Refusal as refusal:  # the contract's typed application errors
                reply["error"] = refusal.error()
            except httpx.TransportError as error:  # the seam or gateway dropped mid-request
                unknown = method in COMMANDS
                refusal = Refusal("unavailable", type(error).__name__, unknown=unknown)
                reply["error"] = refusal.error()
            except Exception as error:  # one request's bug must not end every other client's chat
                log.exception("%s failed unexpectedly", method)
                unknown = method in COMMANDS
                refusal = Refusal("unavailable", type(error).__name__, unknown=unknown)
                reply["error"] = refusal.error()
            await self.send(socket, frame.frontend_id, reply)

    def closed(self, frontend_id: UUID) -> None:
        """The relay reports a frontend connection ended: its subscriptions end with it."""
        for subscription in self.subscribers.values():
            subscription.frontends.discard(frontend_id)
        for coworld, feed in list(self.boards.items()):
            for reader in list(feed.readers):
                if reader[0] == frontend_id:
                    del feed.readers[reader]
            if not feed.readers:
                if feed.watcher is not None:
                    feed.watcher.cancel()
                del self.boards[coworld]

    async def connect(self, url: str, credential: str) -> None:
        async with connect(
            url,
            additional_headers={"Authorization": f"Bearer {credential}"},
            subprotocols=["aide.1.0.0"],
            max_size=2 * MAX_BYTES,
        ) as socket:
            Bound.model_validate_json(await socket.recv())
            # Frontends subscribe through one binding; after a rebind they subscribe again by
            # reading their sessions, so the last binding's frontends are stale.
            self.subscribers.clear()
            self.socket = socket
            try:
                async with asyncio.TaskGroup() as replies:
                    try:
                        async for raw in socket:
                            frame = Frame.model_validate_json(raw)
                            # This backend sends no reverse requests, so no replies arrive.
                            if frame.message.get("method") == "frontend.closed":
                                self.closed(frame.frontend_id)
                            elif "method" in frame.message and "id" in frame.message:
                                replies.create_task(self.answer(socket, frame))
                    except ConnectionClosed:  # superseded or expired; in-flight commands finish
                        log.info("AIDE relay closed the connection")
            finally:
                self.socket = None
                for feed in self.boards.values():
                    if feed.watcher is not None:
                        feed.watcher.cancel()
                self.boards.clear()


async def serve(
    *,
    platform_url: str,
    workspace_id: str,
    credential: str,
    env: dict[str, str],
    inference: Availability,
) -> None:
    """Hold the platform's AIDE backend socket forever: a lost connection only delays reconnection.
    A failed consumer or event watcher ends this, and ``runtime.py`` then recycles the runtime."""
    url = f"{platform_url.rstrip('/')}/v2/workspaces/{workspace_id}/aide/1.0.0/backend"
    url = "ws" + url.removeprefix("http")
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8650") as seam,
        httpx.AsyncClient(
            base_url="http://127.0.0.1:8642/p/orchestrator/",
            headers={"Authorization": f"Bearer {env['API_SERVER_KEY']}"},
        ) as gateway,
        asyncio.TaskGroup() as work,
    ):
        backend = Backend(seam, gateway, inference, work)
        host, port = env["IDE_REASONING_RELAY"].rsplit(":", 1)
        # A relay connection's failure ends that connection alone; the plugin reconnects.
        relay = await asyncio.start_server(backend.relay, host, int(port), limit=MAX_BYTES)
        work.create_task(relay.serve_forever())
        work.create_task(backend.watch())
        delay = 1
        while True:
            started = time.monotonic()
            try:
                await backend.connect(url, credential)
            except (ConnectionClosed, InvalidHandshake, OSError) as error:  # retry forever
                log.info("AIDE relay connection ended: %s", type(error).__name__)
            delay = 1 if time.monotonic() - started > 60 else min(delay * 2, 30)
            await asyncio.sleep(delay)
