# Hosting the research distributions

Softmax hosting runs the `orchestrator` and `researcher` profiles in one sandbox per user. The
profiles own the agents' behaviour, records, tools, skills and the watcher. `image/` owns installing
them, the Hermes gateway, `research serve` and starting sleep (`image/README.md`). Hosting supplies
the durable volume, `/tmp`, `/run/workspace/bootstrap.json` (workspace id, platform URL, credential),
`BUILDKIT_HOST`, snapshots and the relay. This page is the handoff: what the rebuilt distributions
need, and what they guarantee in return. A host running the profiles without the image does
itself what this page gives to the image.
The design is `docs/designs/ide-profile-refactor-2026-10-28-final.html` (Appendix G lists the
metta changes); the build decisions are in `docs/plans/ide-profile-refactor/00-overview.md`.

## What the image must contain

- The release checkout of this repository, read-only to the sandbox user: `core/` (the shared
  package), the root `pyproject.toml` and `uv.lock` (the workspace environment), `orchestrator/`
  and `researcher/`. The plugin imports `research_core` from `core/src` at run time, the
  workspace environment installs it from `core/`, and `prepare.py` finds both beside the profile
  folder it was given with `--source`. A writable checkout would let an agent change the store
  library or the watcher (design Q24); `workspace.doctor` reports whether the core path is
  writable.
- Hermes v0.22.6 or later, the version `image/Dockerfile` and CI pin, with its interpreter at a
  fixed path. Older versions have no `hermes sessions refresh-prompts`, so a release would not reach
  open chats ("Open chats after a release"). The plugin (stdlib and OpenTelemetry, which the
  image build installs there), `prepare.py` and the watcher launcher run under that interpreter.
- `uv`, `git`, and the toolchains Coworld mixins declare (today: none shipped; a mixin may ask
  for another Python, which setup and the watcher install under `$IDE_WORKSPACE/tools/`; any
  other kind, such as Nim, is installed there by hand).

## Install and prepare

```
hermes profile install <checkout>/orchestrator --name orchestrator --yes
hermes profile install <checkout>/researcher --name researcher --yes     # one or several
<hermes python> <profile>/scripts/prepare.py --source <checkout>/<folder> --hermes <hermes> [--check]
```

Run `prepare.py` after every install and every `hermes profile update`; `image/runtime.py` does at
every boot. The installed script takes `HERMES_HOME` from where it is installed and fills in what
the environment does not set from that profile's `.env`. With `IDE_WORKSPACE` unset the workspace
defaults to `$HERMES_REAL_HOME/workspace`, so set `IDE_WORKSPACE` explicitly. It:

1. writes `<profile>/local/core-path.txt` (where the plugin finds `research_core`);
2. creates or migrates the workspace store and every registered Coworld store;
3. re-applies the config keys the machinery relies on (`approvals.mode: off`, no deny list,
   memory off, `tools.tool_search.enabled: off`, the plugin and toolset, `cron.catch_up_missed`,
   the Orchestrator's compaction settings, the tool-result prune trigger, the disabled Hermes
   toolsets), removes retired keys, sets the profile's `skills.external_dirs`,
   `skills.trusted_project_dirs` (every registered Coworld's code) and `terminal.cwd` (the
   workspace root), and applies shipped values only where the owner kept an earlier default; it does not change the
   model (the one exception: an Orchestrator on `moonshotai/kimi-k3` with no recorded model
   change moves once to `moonshotai/kimi-k3.1-pro`, see
   `core/src/research_core/contract/CONTRACT.md`, "Profile models"), but adds the OpenRouter host pins for the Kimi models (`provider_routing.models`,
   Moonshot only) unless the owner set routing for that model or for every model
   (Hermes applies a per-model entry over the rules for every model, so route a Kimi model
   differently with an entry under `provider_routing.models`);
4. retires the Orchestrator's obsolete `watcher` cron job; `research serve` owns scheduling;
5. moves open chats onto this release's tools and instructions when those changed (see "Open
   chats after a release");
6. builds the workspace environment with `uv sync --frozen --no-dev --project <checkout>` into
   `$IDE_WORKSPACE/tools/venv`, or verifies a prebuilt one named by `IDE_WORKSPACE_ENV` against
   the lock's sha256.

`prepare.py` activates no Coworld: a Session that works on one names it when it opens.

`--check` prints what it would change and writes nothing. A first `uv sync` takes minutes, so
it belongs at image build: `image/Dockerfile` does that and sets `IDE_WORKSPACE_ENV`.

### Open chats after a release

Hermes resends what a session first sent, so the provider's prompt cache stays valid: the tool
definitions (`sessions.tool_names`) and the system prompt (`sessions.system_prompt_hash`). It
replaces stored tool definitions only when its own build changes, so a release of this
repository does not reach an open chat by itself, and compaction does not fix that: a real
compaction by the gateway left both stale. A chat could then keep a schema the deployed validators
refuse.

`prepare.py` closes this (`core/src/research_core/frozen_sessions.py`). It hashes what the release
freezes into a session: the role's tool schemas, the profile's installed `SOUL.md`, the plugin code behind the two prompt
sections and, for each shipped `SKILL.md`, its path and frontmatter (the prompt's skill index reads nothing
else, so editing a skill's body does not count). The hash is kept in the profile's `state.db` (`state_meta`, key
`ide_distribution.agent_surface`). When the hash differs, it runs `hermes sessions refresh-prompts`
and sets `tool_names` to NULL on every session; each session's next turn sends and stores the
current tools and a rebuilt prompt, and keeps its conversation. Each chat pays one prompt-cache
miss, on that turn. When the hash is the same, including after a commit that changes nothing
frozen, no session is touched and the cache stays valid.

- It needs no manual step and no release coordination. Every boot runs `prepare.py` for each
  profile before the gateway starts, so each profile's own `state.db` is covered, nothing from
  the old release is still in memory, and a workspace that slept through several releases catches up
  at its next wake.
- The first boot that sees sessions without a recorded hash refreshes them once. That heals
  chats opened before this existed, and also happens on the second boot of a new workspace, because
  a new profile has no `state.db` when `prepare.py` first runs.
- A failed refresh does not stop the boot. `prepare.py`'s summary line says what failed, and the
  next boot tries again. Conversations and stored tools stay as they were; a prompt rebuild may
  already be requested, because the Hermes command commits on its own.
- Run `prepare.py` with the gateway stopped, as the image and `tools/local_stack.py` do. On a
  gateway still running the old release, a chat it holds in memory keeps the old tools until the
  gateway restarts. A chat it has not loaded stores the old tools as its new pin on its next
  turn, and since the hash is already recorded, it stays on them until a later release changes
  the hash. Both were measured with the pinned Hermes.

Between releases the prompt stays frozen too, including its briefing and session context, so
agents are told the briefing is a snapshot and when it was written. A compaction inside a live
agent's turn renders those sections again; the gateway's compaction between turns keeps the stored
text (`_retain_seeded_system_prompt` in Hermes). So a Researcher already running does not see a new
directive: a Task started later gets it in its briefing, and `directive.add` and `directive.update`
name the session Coworld's Tasks in progress so the Orchestrator can tell them with `task.message`.

The Hermes behaviour this depends on is listed in the module's docstring and exercised by
`core/tests/hermes/test_frozen_sessions_real.py`, which CI runs against the pinned image.

## Environment per profile (`.env`)

| Variable | Who sets it | Meaning |
|---|---|---|
| `IDE_WORKSPACE` | the image (`image/Dockerfile`) | The one workspace root of the sandbox, identical for every profile. It holds the workspace environment, the stores and one folder per Coworld; it must live on the durable, backed-up volume. |
| `IDE_WORKSPACE_ENV` | the image (`image/Dockerfile`) | A prebuilt workspace environment. |
| `IDE_RESEARCHER_PROFILES` | `image/runtime.py` | Comma-separated Researcher profile names (`researcher`, or `researcher-1,researcher-2`). |
| `SOFTMAX_PLAYER_CREDENTIAL` | `image/runtime.py`, from `bootstrap.json` | The credential of the Player the workspace acts as, selected at workspace creation. Both profiles read it. |
| `SOFTMAX_PLAYER_ID` | optional; `image/runtime.py` does not set it | That Player's id. Without it, the tools ask the platform (`GET /whoami`), and the watcher's login check stores the answer for the briefing and the environment guide. |
| `SOFTMAX_USER_CREDENTIAL` | nothing yet | The user's User identity token (design B.16), for the Orchestrator only. `bootstrap.json` has no such field and `image/runtime.py` would not write it to `.env`, so hosted, User-identity reads and writes are reported unavailable. It is deliberately not passed to terminal commands. |
| `COGAMES_API_URL` | `image/runtime.py`, from `bootstrap.json` | Softmax API server. |
| `API_SERVER_KEY`, `API_SERVER_HOST`, `API_SERVER_PORT` | `image/runtime.py` | The Hermes API server. The watcher reads them from the Orchestrator profile's `.env`. **Every profile must carry the same `API_SERVER_KEY`**: Hermes authenticates `/p/<profile>/v1/runs` with that profile's key. |
| `OPENROUTER_API_KEY` | the image (`image/inference.py`) | The OpenRouter key both profiles use. The image leases a spend-capped key from the platform (`PUT /v2/workspaces/{id}/inference`) and writes it into each profile's `.env`; outside the image, whoever runs the profiles supplies it. The model itself is not the host's concern: each profile's `config.yaml` names it, and the user changes it through `research serve` (`core/src/research_core/contract/CONTRACT.md`, "Profile models"). |
| `IDE_INFERENCE_LEASE_FILE` | the image (`image/runtime.py`) | The lease file: the installed key's expiry, fingerprint and when it changed. Beside it, `.inference-state` records why model work is blocked and until when (`budget_exhausted` until the lease file names a new key after the daily refill, or `provider_unavailable` for a few minutes after repeated outages), plus the last failed model call. Every run start (chat, wakes, Researchers) waits while the lease is missing or the state blocks; see `core/src/research_core/inference.py`. A chat message whose run fails mid-way on the budget, a rate limit or a provider outage waits (admission status `waiting`) and resumes with a continuation prompt when the gate opens (`core/src/research_core/run_failures.py`, `core/src/research_core/contract/CONTRACT.md` "Message admissions"). Unset outside the image: always available. |
| `IDE_WORKSPACE_ID` | the image (`image/runtime.py`) | Names the workspace on every telemetry line, from `/run/workspace/bootstrap.json`. `unknown` outside the image. |
| `IDE_TELEMETRY_STREAM` | tests and local runs only | A file or pipe to write telemetry lines to, instead of `/proc/1/fd/1`. |
| `IDE_IMAGE` | the image (`image/runtime.py`) | Names the image on every telemetry record: `distribution-` and a hash of the sources it was built from, which the Dockerfile writes to `/opt/distribution/image-id`. `unknown` outside the image. |
| `IDE_PLATFORM`, `IDE_FAKE_FAULTS` | tests only | `fake` selects the platform test double. |

## Gateway, sleep and drain

- The gateway must multiplex profiles (`/p/<profile>/…`): the watcher starts Researcher runs and
  wake Sessions through `POST /p/<profile>/v1/runs` and reads `GET /v1/runs/{id}`.
- Turns must be serialised per Session, not per profile (Appendix G). The watcher never starts a
  second run on a Session that is live: it steers instead.
- Automatic sleep belongs to the image: its lifecycle adapter (`image/control.py`) reads
  `has_work` from `research serve`'s `/status` and starts `research workspace sleep` after fifteen
  minutes without work. *How* it goes to sleep and wakes is the cycle that follows.
- Container health stays as it is: the watcher reports through its heartbeat and
  `workspace.doctor`, never through container liveness, so a broken tool cannot crash-loop. A tool
  module that does not import takes its profile's plugin down, not the gateway: chats still run,
  without the research tools and hooks, and the gateway log says `Failed to load plugin`. CI fails
  on it first (`tools/build_prompts.py --check`, the tests).

### Coordinator and pause

`research serve` is the sole native admission owner. Its periodic loop runs watcher observations;
no cron job is involved. `task.workers` and `task.assign` let the
Orchestrator choose a worker and commit context and rationale before execution; the coordinator
records the task revision the session last saw.
The workspace admission ledger records every chat, worker, continuation, and event-wake start.
UI answers and their delivery inputs commit together; retrying the same answer returns its receipt.

`research workspace sleep` preserves lifecycle v1: pause admission, drain recorded workers and
tracked processes, settle chat/wake runs, then checkpoint and flush the retained volume.
Exit 0 authorizes the host to stop compute. Explicit Stop and fifteen-minute automatic sleep
remain enabled. Every start after a sleep is a full boot (`image/runtime.py`): `hermes profile
update`, `prepare.py` for each profile, the gateway, then `research workspace wake`, which clears a
sleep-owned pause so the coordinator resumes drained work. A new image therefore takes effect at
the next wake.

A deliberate pause (the `paused` setting set by the user or the Orchestrator) is different: wake does
not clear it, and it holds new chat messages as well as background work. Anything with access to the
workspace lifts it with `research store apply --actor user:<id> --store workspace workspace.resume '{"cites": "<message ref>"}'`.

Steering records its target run before transport. Explicit rejection can retry safely; an ambiguous
reply closes delivery without replay while that known run remains tracked. It does not recycle
the workspace. Unknown native run status or an expired/scope-invalid start receipt ends delivery as interrupted
without blind replay. Its execution obligation continues to block sleep. The image supervisor
then exits and the normal container restart policy replaces that runtime; a fresh boot ID
clears the obligation before HTTP admission opens. Later chat inputs can then proceed. Release requests stop the recorded worker, then clear ownership and reservation;
exhausted worker recovery records a failed Task for normal Orchestrator review.
The stronger lifecycle v2 and descendant/delivery guarantees remain a separate future design.
Merging changes here triggers `.github/workflows/release-hermes.yml` and the hosted image release.

Recovery frequency is available as `runtime_recovery_count` in `/status` and `research watcher status`.
The image emits a typed `workspace_runtime_recovery` event before recycling, with workspace and boot IDs.
The packaged fault test records recovery elapsed seconds, container restart count, and validates the event.
When it fails, the fault container's log is in the startup evidence as `recovery-container.log`.
Other recycles, such as a failed AIDE consumer (`image/README.md`) or a failed `/status` read, emit
no such event and are not counted.

Standalone local `research serve` does not own the gateway's lifecycle. After stopping that gateway
and its workers, use `research workspace recover --runtime-stopped` to acknowledge interruption.
This preserves the store and never replays uncertain input. It refuses managed-runtime obligations;
hosted workspaces continue to recover automatically through their supervisor.

## The bridge (the seam)

`research serve` is the seam. In the image it runs beside the gateway, and clients reach it through
the AIDE 1.0.0 backend (`image/aide.py`) over the platform's relay, which does transport only. It stores each user
message with its context block, the client's typed context validated against `research
context-schema` and stamped with the client's identity, the Session's Coworld and `contract_version:
1`. A run's request keeps them apart (`{"input": [message], "context": block, "instructions"}`), and
the block reaches the model through the `pre_llm_call` hook (`plugin/turn.py`). If the Session has a live run, new inputs wait in the durable inbox. It streams each store's `events` table to clients as Server-Sent Events
(`research store events --after <seq>` and `--follow` show the rows; one cursor per store), and
applies user edits through `research store apply --actor user:<id> <op> <json>`. Notices
(`events` rows of kind `notice`) must reach every client.

The model routes of `core/src/research_core/contract/CONTRACT.md` ("Profile models") are served by `research serve`
itself, which the workspace image runs: it reads each profile's `config.yaml` (`model`,
`fallback_providers`) and changes them with `hermes config set` under each profile's home. The
IDE reaches `research serve` only through the AIDE 1.0.0 methods, which have no model operations,
so the IDE cannot show or change a model until AIDE adds them.
Nothing that starts a run may put a `model` in the run request.

## Agent telemetry

Capture is always on in the image, with no setting. Every process
writes JSON lines to `/proc/1/fd/1`, and the Datadog Agent collects them. Nothing about telemetry is on the volume. Under
`tools/local_stack.py` no Agent collects the lines, and outside a container nothing is written unless
`IDE_TELEMETRY_STREAM` names a file or pipe. See [`agent-telemetry.md`](agent-telemetry.md).

## Storage and backup

The durable state is `$IDE_WORKSPACE` (stores, code repositories, worktrees, documents) and each
profile's Hermes `state.db`. SQLite runs in WAL mode with `synchronous=FULL` on every writable
connection, so a committed record survives a crash of the process or the machine; the bridge may
read the stores with a read-only connection (check S3: WAL reads under gVisor from a second
process). A store's writer waits up to 5 s for each lock another connection holds, including during
a store's first switch to WAL, where SQLite itself would not wait; a lock held longer than that
fails the call as `database is locked`. A quiesce changes that wait, as described below. Nothing on
the volume survives losing the volume, so backups are part of the contract:

- **Hourly encrypted snapshots of the volume are the required minimum**, keeping the daily
  history as it is (a longer retention, not a shorter one). This bounds the loss at one hour of
  records, chat and action receipts. The design's accepted daily bound (R6) is replaced by this.
- `research store backup <dest-dir>` is a database-aware copy hosting may schedule more often
  than the snapshots, into separate storage. It copies the workspace store, every registered
  Coworld store and, with `HERMES_HOME` set, that profile's Hermes `state.db` with SQLite's
  backup API (never a raw copy of a live database and its WAL) into `<dest-dir>/<timestamp>/`
  with a `manifest.json` (sources, sizes, sha256, the workspace store's `last_seq`). Run it once
  per profile whose transcripts should be covered, or copy the other profiles' `state.db` files
  the same way.
- `research store restore-check <backup-dir>` is the restore test: it opens every copy
  read-only, runs `PRAGMA integrity_check`, compares the manifest's checksums and checks each
  store's schema against the running release, and prints a JSON verdict (exit 1 on any
  failure). Run it against a stored backup, not the one just written, as part of the hosting
  checks.
- A backup must cover the workspace store, every Coworld store, the Hermes transcript databases
  of every profile, and the volume's repositories, worktrees and documents (the volume snapshot
  covers the files; the records refer to them by path).
- Monitor the age of the latest restorable backup (the newest folder whose `restore-check`
  passed) and alert when it exceeds the loss window.

Live transcript rows are not always what a model first received. Once a request reaches
`compression.proactive_prune_tokens`, Hermes rewrites old large tool results to one-line summaries
under the same row id, so AIDE `chat.history` returns the summary. The original stays in `state.db`
as an archived row (`active = 0`, `compacted = 1`).

A snapshot taken after `research workspace sleep` exited 0 needs nothing more. For a snapshot
of a workspace that stays awake, run `research store quiesce --then <snapshot command>`, or
run `research store quiesce` in the background and initiate the snapshot when its first JSON
line appears. It checkpoints each database's WAL into its file, takes and holds the write lock
on every Coworld store, the workspace store and (with `HERMES_HOME` set) that profile's Hermes
`state.db`, in that order (a transition nests its workspace write inside its Coworld write, so the
reverse would let the two wait on each other), fsyncs each file and its directory, and holds for at
most 30 seconds (`--max-hold`; a `--then` command still running is killed). Without `--then` it holds for all of that time.
Do not raise `--max-hold` above the default on a workspace running chats: the AIDE backend's seam
calls outlast the default hold by a few seconds, and a longer one can time out an append, which
recycles the runtime (`image/README.md`).
Readers continue and database writers wait, so the databases it locked stop at one moment: a
transaction spanning stores has either finished or not begun. That recovery point has limits:

- A WAL that readers or constant writers kept from emptying stays, reported as `wal_bytes` above 0
  in the first line. The lock is still held and the WAL is fsynced, so a volume snapshot, which
  takes every file, still has every committed write. To copy databases rather than snapshot the
  volume, use `research store backup`.
- The list of Coworlds is read before the first lock, so a Coworld registered after that is not
  locked or checkpointed: it keeps taking writes, and is in the snapshot as a crash would leave it.
- Only the `state.db` of the `HERMES_HOME` profile is locked. Other profiles' transcripts are also
  as a crash would leave them.
- Work done as several transactions in a row can be caught between them, as it can by a crash.
- Nothing stops writes to ordinary files, so a document or worktree may be mid-write from a tool
  process.

Keep the hold short: a store connection opened before the hold began waits only its usual five
seconds before reporting "database is locked", so initiate the snapshot within that time. Stores
opened during the hold wait for the whole of it, including a restored copy not yet in WAL mode,
which makes its switch to WAL after the release. Hermes retries its own writes to `state.db`, for 20 s by
default. For a
repository, the risk is bounded: git writes objects and refs through temporary files and renames, so such a snapshot
still restores to a readable repository, at worst with a stale `.lock` file or an unfinished
temporary object to remove.

The watcher's schema repair never replaces the store file: it rewrites the schema inside the
file in one transaction and first saves a copy next to it (`store.before-repair-<timestamp>.db`),
named in the notice. A store written by a newer release than the running image is left
untouched and the workspace is paused until the matching release runs again.

## Releases

A push to `main` touching the paths in `.github/workflows/release-hermes.yml` (what the image holds)
dispatches Metta's image release, unless the last nightly read-only platform check (`research
platform check --read-only`) failed. Metta's `deploy-hermes-agent.yml` builds `image/Dockerfile`,
runs `image/system_tests/start-stop.sh`, publishes the digest and dispatches its qualification job.
None of these sends a model request. The acceptance scenario ("submit the starter as my champion" on
a new workspace ends in a submission) runs scripted, with no model, as a pull request check
(`core/tests/scenarios/test_seam_end_to_end.py`); its real-gateway form needs a model key, so it
runs by hand (`docs/local-end-to-end.md`). The daily lock refresh keeps `coworld` and `softmax-cli`
current.

## Checks hosting should run once (design H "Validation")

- S1: several Researcher runs in one profile stay isolated; a Session resumes after a gateway
  restart (`core/tests/hermes/test_spike_s1.py`, needs a model key).
- S3: the bridge reads a store with WAL under gVisor.

## Model requests

Both research profiles use Hermes's `llm_request` middleware to cap OpenRouter output at
16,384 tokens, preserving smaller explicit caps. This limits the up-front credit reservation
without changing the context window. A 402 reporting a positive affordable output count does
not prove exhausted credit: it records `output_limit` and fails that run without locking other
model work or scheduling budget-refill continuations. A zero affordable count and other billing
failures retain the budget gate. The platform owns daily allowance and owner team membership.

The same middleware point sends only the latest of identical refused calls in each Chat Completions
request; `plugin/repeats.py` lists what stays regardless. The stored history keeps them all, so a
Session's transcript shows more calls than the model was last sent.

It also removes the `<mandatory_tool_use>` block of Hermes's Execution discipline section from the
system message of each request (`plugin/hermes_prompt.py`). The stored prompt keeps the block, so a
Session's stored system prompt differs from what the model is sent by that block.
