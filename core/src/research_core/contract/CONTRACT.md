# The client contract (version 1)

## Execution authority

Hosted requests carry gateway-verified execution authority outside client context. A person or
cogent admitted under a product grant receives an isolated `restricted` conversation in the same
Hermes gateway. Its immutable identity includes issuer, subject, subject kind, OAuth client, local Player
and product grant. Session metadata and SQL triggers prevent that identity from changing. Existing
stored conversations remain owner conversations.

Each message admission retains its own authorization handle and expiry. Renewing a token can
continue the same granted conversation, but cannot renew queued work, change another grant's
conversation, or steer into an already running restricted admission. The dispatcher checks the
original authorization before every start or recovery. An unavailable authorization service
denies new effects and retains the original receipt and proof for retry. The existing retry
deadline never passes the proof's expiry. Expired proofs, revoked grants and authoritative
inactive responses end work. Restricted background and child admissions are refused; they never
inherit ambient owner authority.

A reconnect may retry the same operation ID with a new authorization handle under the same
immutable grant. Receipt matching compares that identity and the unchanged request content,
excluding the connection's authorization field. It returns the original receipt without changing
the original admission's proof, expiry or execution state.

Restricted conversations accept text and workspace scope only. They cannot enter owner history,
use Coworld state, run tools, or acquire owner credentials. AIDE authorizes reads and pushes;
the native restricted profile enforces model execution. Caller-supplied client names, instructions
and context are presentation data and cannot authorize an operation. The local seam remains a
trusted process boundary; omitted authority there means owner and cannot access a restricted
conversation. Hosted deliveries must carry explicit authority.

Checks: `core/tests/scenarios/test_session_coworld_isolation.py` exercises persisted grant
isolation and revoked work after restart. `sessions_authority_immutable` and
`client_admissions_authority_immutable` protect stored identity. The native restricted-profile
checks cover tool and credential denial separately.

The profiles own one small contract with clients: the IDE, the site, coplay, and any scripted
client. It is published here as two JSON Schema files plus this note. The design calls it
**the seam** (design §10); what is still to build is listed at the end and extends these files
without breaking them.

| File | What it describes |
|---|---|
| `context.schema.json` | The typed context block a client sends with each message (A.3, A.4). |
| `events.schema.json` | One row of a store's `events` table as the stream delivers it (A.11). |

A client reads the current files from the installed profile with `research context-schema
[--name context|events]`; it never hard-codes them.

## The three channels

1. **Context in.** A client sends `session-open` once (its identity, a session context and its
   capabilities), then `{message, context}` for each turn. The bridge validates the context,
   stamps it and keeps it beside the message; the profile's turn hook renders it for the model.
2. **State sync.** The tools and the watcher write records and notices into the store; every
   write is an `events` row. The bridge streams the rows to all attached clients as Server-Sent
   Events, so each client renders what it can and all stay in sync.
   A background wake cannot write into a chat: its `chat_update` is recorded in the store, and
   the coordinator delivers it as a queued `followup` message admission into the user's most
   recent chat for that Coworld, so it reaches the user as an ordinary assistant turn.
3. **Edits back.** User edits go through `store apply`. The agent asks its questions in the
   chat, and the user answers there.

Locally `research serve` is the bridge (design A.11, R3). Its routes are the reference for the
hosted bridge:

| Route | Channel | Does |
|---|---|---|
| `POST /session-open` `{op_id, client: {id, kind, guide?}, session_context?, capabilities?, actor?}` | 1 | Registers a chat Session; returns `{session_id, client, coworld, session_scope, contract_version, problems}`. `op_id` is a UUID the client generates and reuses when it retries the same call. |
| `POST /message` `{op_id, session_id, message: {role: "user", content}, context?, instructions?, actor?}` | 1 | Validates and stamps the block and accepts the message durably; returns `{op_id, session_id, run_id, delivered: "queued", problems, seq}` before any run takes it (see "Runs per Session"). `op_id` as above. |
| `POST /store/apply` `{actor, op, args, store?, coworld?}` | 3 | One op of the `store apply` allow-list as `user:<id>`; returns `{record, seq}`, or HTTP 202 with `{status: "pending", command, ...}` when the op is a transition still running (read it at `GET /command`). A `directive_id` is `W-<n>` (workspace) or `D-<n>` (Goal); a bare number or a contradicting `store` is refused. |
| `GET /snapshot?store=workspace\|coworld&coworld=<id>` | 2 | The store's records, settings and `seq`. `coworld` is required for a Coworld's store. |
| `GET /stream?store=…&coworld=<id>&after=<seq>` | 2 | SSE of the store's events after `seq`. |
| `GET /runs/<run_id>` | 1 | The run's status from the Hermes runs API. |
| `GET /models` | 2 | Each profile's model: `{profiles: [{profile, role, model, provider, fallbacks}]}`. See "Profile models". |
| `GET /models/catalog` | 2 | The models a client may offer: `{source, models: [{id, name, context_length, usd_per_million_input, usd_per_million_output, free}]}`. |
| `POST /models` `{profile, model, provider?, fallbacks?, actor?}` | 3 | Changes one profile's model as the user asked; returns `{profile, previous, changed, verified, seq}`. |
| `GET /health` | — | `{ok, profile, coworld, contract_version}`. |
| `GET /` and `GET /ui` | — | A convenience of `research serve`, not a contract channel: the client page (`research_core/ui/index.html`), which exercises every channel above. |
| `GET /status` | — | A convenience of `research serve`: what `research watcher status` prints (`heartbeat`, `has_work`, `waiting_on`, `paused`, `sleep_started`). |

Errors are JSON `{ok: false, error, problems}` with an HTTP status: 400 for a malformed or
refused request, 404 for an unknown session or store, 409 for a
block whose scope, Coworld or league differs from the Session's, 503 with `Retry-After: 1` when the gateway cannot take the run now (retry), 502 for any other
gateway failure. The `actor` of a write is `user:<id>` (default `user:local`); a session actor
is refused, because Sessions write through the tools.

## The seam fields and the four stamps

Every field of the context block is optional except `contract_version`. The fields and their
limits are in `context.schema.json`. The block travels in
the durable admission separately from the user message; the profile's turn hook reads that
admission, validates the block again and renders it for the model.

The bridge stamps four items into each turn (design A.4). `research serve` stamps the first
two into the block; the profile's own turn hook (`plugin/turn.py`) adds the last two, so the
bridge never writes them:

| Stamp | Who | Where |
|---|---|---|
| `client` — the client that opened the Session (`id`, `kind`, `guide`) | bridge | the block; it overrides any `client` the message carried |
| `coworld` — the Session's Coworld | bridge | the block; a block naming another Coworld is refused (409) |
| the changes since this conversation's last turn | turn hook | `[changed since your last turn]` |
| the unread notices | turn hook | `[notices since your last turn]` |

`contract_version` is stamped too, so a block always carries it.

## Limits and validation (A.7)

The whole block is at most 16 KB as JSON and `client_data` at most 8 KB; `mode.name` and
`command.name` at most 40 characters, their `instruction` at most 1,000; `on_screen` at most 20
items. Unknown fields are dropped and named in `problems`; an oversize or malformed field is
refused with a message naming the limit, never truncated; the rest of the block is kept. A bad
field never fails a turn: `/message` answers 200 with `problems`, for the client, and the message
goes on without the refused parts. Only a mismatch with the Session is refused (409 above).

## The client guide skill

`client.guide` names the skill the agent loads for that client (for example `client-coplay`).
It replaces free instruction text, which the bridge drops. The agent asks its questions in
chat.

## The stream (A.11)

A client loads `/snapshot` for a store, then opens `/stream?store=…&after=<the snapshot's seq>`;
nothing falls between the two. Each store (the workspace store and each Coworld store) has its
own `seq`, so a client keeps one cursor per store and one connection per store. Frames are
`id: <seq>` and `data: <event JSON>` (the event per `events.schema.json`, plus `store`); a
`: keepalive` comment goes out after ten idle seconds; new rows appear within half a second.
A reconnecting client sends `Last-Event-ID` (or `after`) with the last `seq` it saw. Clients
ignore event kinds and content kinds they do not know.

## Runs per Session

The Hermes gateway does not serialise runs per session, so the bridge does (amendment A6.1). It
accepts a message durably and answers `delivered: "queued"`, usually with `run_id`
null; the client does not retry and follows the admission below. Later, text for a Session with a
live run is steered into that run; otherwise the message waits its turn and starts a new run with
the same session id, which resumes the conversation's history.

## Message admissions

Each queued message is an admission in the workspace snapshot (`records.admissions`): `{op_id,
session_id, message, status, run_id, error, error_class, resume_after, created_at, client_id}`.
`status` is `queued` until its run starts, then `running`, and ends `completed`, `cancelled`,
`failed` or `interrupted`. It ends `cancelled` only after `POST /message-cancel`; a run stopped
without one (a drain, a shutdown) leaves it `interrupted`. A run that fails for a reason that passes on its own leaves the
admission `waiting`: not finished, and not to be retried by the client. The bridge resumes it by
itself once `resume_after` has passed and model work may start again, with a fixed continuation
prompt in the same Session (beginning `Your previous run stopped because`), not the user's text
again; if the resumed run fails the same way, the admission waits again, each time for longer
(the hold doubles per resume, up to 30 minutes). The resumed run keeps the admission's
`instructions` and context. Its continuation prompt is a new user message in the native
transcript; `/messages/context` joins the admission's context to the user's original message,
so the continuation shows `client_context: null`. A `waiting` admission holds its Session: later
messages stay `queued` behind it until it resumes, and a text message among them then joins the
resumed run, as any follow-up joins a live run. A message that had joined the run that failed
ends `failed` with the class rather than waiting: the one resume continues the whole
conversation, that message included. `POST /message-cancel` cancels a `waiting` admission at
once; a cancelled message never waits. It answers `409` for a message that joined another
admission's live run, since stopping that run would end the other admission too.

The bridge also admits follow-up turns into a chat on its own (origin `followup`): the results of
an `xp.request` the chat made, and a background wake's `chat_update` updates. A follow-up fails, waits
and resumes like a message. Its prompt is a user message in the native transcript that the user
did not type, so `/messages/context` marks it `runtime_input: true`, and the AIDE backend shows it
as a continuation, not as the user's message.

`run_id` is the admission's current run: `null` while it is `waiting`, and a new run on each
resume. A client follows the admission's `status` until it is `completed`,
`cancelled`, `failed` or `interrupted`, and tracks whichever run `run_id` names at the time, not
the first run it saw.

`GET /status`'s `has_work` counts `queued` and `running` admissions, each named in `waiting_on`
as `admission:<op id>:<status>`, but not `waiting` ones, a start the gateway refused that waits
for its `retry_at`, an admission that needs resolution, or the messages queued behind a waiting
or refused one in the same Session, on purpose: a budget wait lasts until the daily refill, and
must not keep the workspace awake that long. The workspace may sleep; a waiting admission resumes once the workspace is awake again
(for example, when the user's next message wakes it).

`error_class` names why a `waiting` or `failed` admission's run failed, when that is known
(`null` otherwise); `error` keeps Hermes's own text. A resuming admission is `queued` again and
keeps the class of the run that failed until its new run starts, when the class is cleared:

| `error_class` | Status | What the client can say |
|---|---|---|
| `budget_exhausted` | `waiting` | Out of budget until `resume_after` (the refill time) |
| `rate_limited` | `waiting` | Busy, retrying |
| `provider_unavailable` | `waiting` | Model provider down, retrying |
| `too_large` | `failed` | Conversation too long; start a new one |
| `misconfigured` | `failed` | Workspace misconfigured |

A message whose cancel was requested never waits: with any of the first three classes it ends
`failed`, keeping the class.

`resume_after` is the earliest time a `waiting` admission resumes (ISO time; `null` for any
other status). A budget wait can last past it, until the workspace's new key arrives.

## Profile models

Each profile's model belongs to the profile, not to the host: a new install starts with what the
profile's shipped `config.yaml` names (each profile: `moonshotai/kimi-k3.1-pro`, falling back to
`z-ai/glm-5.3`, both through OpenRouter), and only the user changes it, through the bridge.
`prepare.py` does not change it, with one exception for a profile on OpenRouter with no
`profile.model_updated` event yet: an Orchestrator on `moonshotai/kimi-k3`, or a Researcher on
`recursive/flint-v1.0:free`, moves once to `moonshotai/kimi-k3.1-pro` (the Researcher also to the
`z-ai/glm-5.3` fallback), recorded as that event with the actor `harness`. `moonshotai/kimi-k3.1-pro` and `moonshotai/kimi-k3` go only to
Moonshot's own OpenRouter endpoint: `provider_routing.models` pins them, unless the owner routed
that model. Another model the user picks gets no pin.

- `GET /models` lists every profile of the sandbox (the Orchestrator, then each Researcher
  profile) with its `role` (`orchestrator` or `researcher`), `model`, `provider` and
  `fallbacks` (`[{provider, model}]`, tried in order when the primary fails).
- `GET /models/catalog` is the list a model picker offers: OpenRouter's models that take tool
  calls (the profiles need them), with context length and prices in US dollars per million
  tokens. It is cached for ten minutes; 503 with `Retry-After: 30` when it cannot be read.
- `POST /models` `{profile, model, provider?, fallbacks?, actor?}` changes one profile.
  `model` is an id such as `moonshotai/kimi-k3`; `provider` defaults to the profile's current
  one; `fallbacks`, when present, replaces the list (each entry a model id or `{model,
  provider?}`, at most four; an absent `fallbacks` keeps the list). Only a `user:<id>` actor
  may change a model. An OpenRouter model not on the catalog is refused (400, named in
  `problems`); when the catalog cannot be read and no earlier list is held, the change is made
  and `verified` is false.
  `changed` lists the config keys written (empty, and no event, when nothing differed); an
  unknown profile is 404.
- A change is a `profile.model_updated` event in the workspace store (subject: the profile;
  payload: the new `{profile, role, model, provider, fallbacks}` with `previous` and
  `changed`), so every attached client sees it on its stream.
- A change applies to the next run of every conversation of that profile: Hermes reads the
  profile's config when a run starts. A run already going finishes on the model it started
  with. The bridge must not name a `model` in its run requests, or the request would override
  the profile's choice.

## Versioning

`contract_version` is an integer in every schema file and in every block. It increments on any
breaking change: a removed or renamed field, a tightened limit, a changed meaning. Adding an
optional field or a new event kind is not breaking: clients ignore what they do not know, and
the backend drops what it does not know. A client sends the version it was built for; the
backend accepts the current one and refuses others in `problems`.

## Deferred to the contract layer

Not part of the seam, and not built here: `session-update`, relaying live chat
tokens and client-supplied guides by reference.
