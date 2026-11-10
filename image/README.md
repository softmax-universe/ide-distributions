# Self-contained workspace image

The workspace coordinator owns native admission and periodic watcher execution. The full plan is
[workspace orchestration](../docs/specs/workspace-orchestration.md).
The existing lifecycle v1 sleep/wake behavior remains enabled. Explicit Stop and idle sleep
use pause, drain, settle, and flush; lifecycle v2 is a separate future change.
Lost native receipts remain execution obligations until the supervisor recycles the container.
The next boot closes those obligations without replaying their inputs. This is automatic recovery,
not a forced platform hold or an operator command.

The image owns profile installation, dependency resolution, internal paths, gateway setup,
watcher activation and the translation from generic lifecycle requests to research commands.

Build from the repository root:

```sh
docker build --platform linux/amd64 -f image/Dockerfile -t ide-distribution:local .
```

The image runs the pinned Hermes unpatched. Agent telemetry uses Hermes's own hooks and middleware
(`core/src/research_core/plugin/capture.py`).

The host supplies a durable `/workspace`, writable `/tmp`, and a read-only
`/run/workspace/bootstrap.json` containing `workspace_id`, `platform_url`, and
`credential`. Run the image entrypoint as its declared numeric user, with a read-only
root filesystem and no Linux capabilities. No source checkout or dependency installation
is needed outside the image.

The hosted image selects its bundled plugins through Hermes's supported
`HERMES_BUNDLED_PLUGINS` setting. It preserves the base image's plugins except
messaging-platform adapters: hosted traffic uses the built-in API transport.
Standalone orchestrator and researcher installations retain their normal plugin selection.

Agent telemetry goes through the container's own output to the Datadog Agent, and holds content
([`docs/agent-telemetry.md`](../docs/agent-telemetry.md)). The [OpenTelemetry](#opentelemetry) spans
below are separate: payload-free timing on stderr. Their rule that no process exit waits for a
telemetry flush does not cover agent telemetry, which has its own bounded exit flushes
([build scope](../docs/designs/datadog-agent-logging-build-scope.md)).

The lifecycle adapter sends replies over the platform's authenticated outbound workspace
connection. Explicit sleep starts the distribution command asynchronously; status reports
ready for sleep only after its successful exit. The image also initiates sleep after fifteen
minutes without work. Restart calls wake with the retained filesystem.

The release dispatcher passes the distribution commit to Metta's generic build workflow.
That workflow builds `image/Dockerfile`, runs `image/system_tests/start-stop.sh`, and publishes
an immutable image digest. Hosting consumes that digest without inspecting profile internals.

Chat reaches the image only through the [AIDE 1.0.0 backend](#aide-100-backend); the
workspace connection above carries hosting control alone, and refuses any other envelope with
422 `invalid`. Chat messages are confirmed only after the workspace commits them. Each operation ID names
one input; reconnecting with that ID cannot create a second run. Messages wait behind the
previous run in their session, and queued messages survive sleep and restart.

Session opening and message intake perform only local validation and durable writes. They
never acquire the execution lock. One serial dispatcher creates native sessions, prepares
attachments and starts Hermes runs after intake. A newly accepted session has an empty
transcript until its native session is ready.
The open receipt and local session registration commit in one transaction. A temporary native
opening failure stays queued with its original identity; a permanent failure also fails its
queued messages. Neither stops the dispatcher from processing unrelated sessions.

Session intake emits OpenTelemetry `session.open` and `message.admit` spans, correlated
by `op.id`, `session.id`, and `replayed`. Child `intake.transaction` spans mark writer wait,
acquisition, admission write, registration, and commit with SDK-timestamped events. These
boundaries preserve the distinction between database contention and execution preparation.
No message or attachment contents are captured. See [OpenTelemetry](#opentelemetry) below.

The image obtains its spend-capped model credential from
`PUT /v2/workspaces/{workspace_id}/inference`, using the bootstrap credential. It installs
and renews the key internally; hosting never supplies profile paths or model environment
variables. Queued chat dispatch waits for
a valid inference lease, including after restart. Lifecycle readiness remains independent
of the optional model service, so an unavailable budget does not prevent stopping a workspace.

A spent key is not an expired one, so the image also keeps the *inference state*,
`.inference-state` beside the lease file (`core/src/research_core/inference.py`). The
profiles' `api_request_error` hook sets `budget_exhausted` when Hermes classifies a model call
as `billing` (a 402, or OpenRouter's 403 "Key limit exceeded"), except for a 402 that says a
smaller response is still affordable, and within two minutes of a key change, when such errors
come from runs still finishing on the previous key. It sets
`provider_unavailable` for five minutes after three outage errors within three minutes. The
lease loop sets `budget_exhausted` when the platform refuses a lease for that reason, and
renews no later than the refill (the lease's expiry minus the platform's ten-minute grace).
A budget block names the spent key's fingerprint and holds only while the lease file names
that same key, so installing a different key ends it with no second write that could be
lost. A budget block's `until` is the refill time shown to the user; the block itself holds
until the new key arrives, so the gate does not open on the spent key in the seconds after
the refill. An outage block lapses at its `until`. The lease file write is essential and a
failure raises; state writes run off the event loop, and a failed one is logged rather than
stopping the workspace. While the state blocks, chat, wakes and Researchers start no
runs, and AIDE `chat.send` answers the `inference_unavailable` error with
`"reason": "budget_exhausted"` and `"until": "<ISO time>"` (`reason` and `until` are null when
the lease is only missing). Work resumes on its own once the state clears or lapses.

## AIDE 1.0.0 backend

`image/aide.py` also connects every workspace to the platform's AIDE backend socket
(`/v2/workspaces/{id}/aide/1.0.0/backend`, subprotocol `aide.1.0.0`, the bootstrap credential)
and answers the [AIDE 1.0.0](https://softmax.com/docs/aide) JSON-RPC methods. It runs beside the
control connection, gates neither readiness nor sleep, and reconnects forever; no frontend reaches
it until the platform routes one to it. The contract stays in Metta; this image does not
bundle or validate it, the relay does.

Each method maps to seam and gateway calls. The AIDE request id
is the seam `op_id` or gateway `Idempotency-Key`, so retries need no new tables. The gateway
forgets a prompt once it is resolved, so an applied `input.respond` is also recorded at the
seam's `/input-response`, which an identical retry replays. Notifications go to the frontends
that named a session in the last ten minutes: run output (`chat.event`, `chat.chunk`,
`chat.message`, `chat.finished`) and session changes from the seam's event stream
(`state.changed`). Coworld sessions without a league have no AIDE scope and are not listed.

Run progress is durable. One consumer per open request reads the gateway's run stream,
translates each event to an AIDE `ChatEvent` (consecutive text deltas merge for 250 ms), appends
it to the workspace store's `request_events` log through the seam (`POST /message-events`), and
only then pushes it. `chat.events` reads that log alone (`GET /message-events`); the gateway's
in-memory run buffer only bridges a dropped run stream. Consumers start with the backend and
resume after their log's last row. A restart recycles the gateway too, so what had not reached
the log and pushes not yet sent are lost. A run the gateway lost ends `interrupted` once the seam
settles it, and its logged events stay readable for as long as the request record.

An admitted run's stream carries no reasoning, so the gateway plugin relays it
(`core/src/research_core/plugin/reasoning_relay.py`): Hermes hands it each reasoning delta through
`on_stream_delta` (`plugins.stream_reasoning_deltas`), and it sends each one as a JSON line to the
backend at `IDE_REASONING_RELAY` (127.0.0.1:8651). The consumer following the run of that delta's
native Session logs it where it arrives among the run's events, merging consecutive chunks for one
second. A delta for no followed run, such as one that arrives after its run ended, is dropped.
A relay failure never reaches the turn: Hermes isolates the hook. Each follow records a
payload-free `workspace.aide.reasoning` span with the deltas relayed, the events logged and the
deltas lost on the way (`reasoning.lost`, a gap in the plugin's running count).

A session the gateway does not have reads as having no messages, so its requests still close. A
consumer follows its run stream again when the stream drops or a read for a push fails, since
neither moves its place in the log. Any other failure in a consumer, or a seam error answer to the
event watcher's `recover()` or `changed()`, ends the backend, which recycles the whole runtime.
That includes an append with no answer, such as a timeout: it may have committed, and the consumer
has already moved past that event, so going on would leave a gap. The next backend resumes from
the log, so an append that committed before the timeout is not pushed again and only `chat.events`
returns it. Nothing retries the failed call, so a failure that lasts across boots can crash-loop
the workspace.

A request is different: the failure is answered to that request alone. One the handler did not
expect is logged with its traceback and answered `unavailable`, with `outcome: unknown` for a
command, since it may have taken effect. The backend keeps serving.

A frontend subscribes to a session's pushes through any request naming that session, once the
backend has read the session and its scope is that session's own. A wrong scope is refused
(`forbidden`) to that request alone and is not recorded, because the event watcher reads with the
recorded scope and a refusal there would end the backend.

The backend's seam calls time out after 40 s (`SEAM_TIMEOUT_S`), which a write waiting out a default
`research store quiesce` stays inside. A write that had already begun waiting when the hold began
keeps its usual five seconds, and its `database is locked` answer recycles the runtime as above: 1 of
25 holds in a test at about 60 writes a second. A `--max-hold` above the default 30 s has no margin
either.

The normal image smoke checks drive the packaged AIDE backend against its running seam. Real model
chat remains a target-environment acceptance check with an authorized inference lease.

Run `bash image/system_tests/start-stop.sh ide-distribution:local` after building. The smoke
uses a fresh volume, then restarts with the same volume under the hosting filesystem
restrictions. It checks AIDE intake, explicit clean-sleep refusal, retained sessions and control transport failures.
It validates eight completed startup spans on each boot: profile install/update, preparation,
gateway readiness, application wake/readiness and total startup. Missing phases, reused boot
IDs, broken parent relationships and unexpected startup attributes fail the check.

## Repeatable startup measurements

Use the same built image, machine and resource limits for before/after comparisons:

```sh
STARTUP_CYCLES=6 STARTUP_ARTIFACTS=/tmp/ide-startup-baseline \
  bash image/system_tests/start-stop.sh ide-distribution:local
```

The artifact directory must not already exist. It retains the image ID/architecture, checkout
revision, per-boot logs and per-stage median/minimum/maximum durations. The first boot uses an
empty volume; later boots retain state and run the same intake, sleep and persistence checks.
`workspace.state` on the startup span distinguishes fresh from previously prepared workspaces.
This is workspace state, not whether the Kubernetes host or image cache was cold.

The readiness wait has a four-minute wall-clock budget between Docker probes. A stalled Docker
request can exceed that budget; Docker must be healthy before interpreting a benchmark.
Container timing ends at application health. Platform control readiness and browser usability
remain separate gates; the smoke's deliberately unavailable platform does not prove either.

The startup comparison workflow builds the PR base and candidate from their own source and
lockfile on one Linux runner. Each image runs one fresh boot and five retained boots with the same smoke command and
the hosted workspace limit of two CPU cores and 2 GiB memory. This compares the complete
proposed runtime change. Both image identities, import profiles, bytecode counts, and all per-boot
timing reports are retained as artifacts.

On container stop, the supervisor cancels ongoing startup/control work, terminates its owned
application and gateway processes, and waits for them to exit. A child that ignores termination
is killed after ten seconds. The smoke requires a zero container exit code and retains gateway
file logs alongside startup spans when STARTUP_ARTIFACTS is set. These fixture logs contain no
real model credentials or user conversations.

## Startup tracing

`workspace.startup` spans join on resource attributes `workspace.id` and
`service.instance.id` (the boot ID). `workspace.state` is `fresh` or `retained`.
The root ends at application health. Child spans cover profile installation/preparation,
gateway readiness, and application wake/readiness. Concurrent `workspace.inference_ready`
and `workspace.relay_connected` spans end at first credential installation and first
websocket handshake respectively. Cancelled readiness is an error observation, never a
successful zero-duration milestone. Missing completion can also mean process termination.
Renewal and reconnection do not duplicate these first-readiness spans.

These milestones do not claim a completed model turn or browser delivery.

The container starts the gateway through Hermes's supported `python -m gateway.run` entrypoint.
The general CLI remains installed for tools and profile operations; its full dispatcher is not
needed to run the container-owned gateway. Packaged restart and transport checks exercise this
entrypoint, including gateway interruption and recovery.

Hermes's base image disables runtime bytecode writes, and hosting mounts its root filesystem
read-only. The image compiles Hermes's application modules and installed dependencies at build
time so each gateway restart can load cached bytecode. Tests and optional skill examples are
excluded from source compilation. The comparison artifact also contains gateway import timings
and bytecode counts for both images; compilation benefit must be established by the boot results.

The hosted local speech-model path is applied by profile preparation through Hermes's existing
configuration API. It is included in `profile_prepare` timing. Startup does not launch a separate
`hermes config set` process per profile. `IDE_LOCAL_STT_MODEL` selects the bundled model;
standalone preparation without that setting preserves its existing speech configuration.

## Container policy builds

The image packages `buildctl` alongside the locked Coworld CLI. Hosting supplies a private
BuildKit endpoint in `BUILDKIT_HOST`; profile preparation passes it to terminal commands.
A mixin builds with `coworld build-policy <context> --output <archive>` and declares
`artifact.kind: image`. Keep the archive outside the Docker build context. `version.create`
retains the archive by its byte digest; `upload` saves the image config digest separately
for the platform's upload lookup. Recovery needs those saved inputs, not the archive.

`image/system_tests/check-build-upload.py` is an opt-in proof inside the built image with a
real builder and platform Player credential. Pass `SOFTMAX_PLAYER_CREDENTIAL` and
`SOFTMAX_PLAYER_ID` as JSON on stdin, and provide writable `/workspace` and `BUILDKIT_HOST`.
It creates one uniquely named policy with two versions, verifies an ordinary upload and
recovery after a deliberately lost response and deleted archive, and never submits or evaluates.

## OpenTelemetry

All timing/tracing uses the OpenTelemetry Python SDK. The locked workspace package and
native build requirements select version 1.45.1, already shipped in the pinned Hermes image.
No packages install at runtime. Standalone profile installations need the same native SDK.

Long-lived workspace processes use standard `BatchSpanProcessor` instances for both the
SDK's `ConsoleSpanExporter` and the optional OTLP/HTTP exporter. Each has its own bounded
queue and background worker. A slow console or collector cannot hold up span completion;
full queues drop telemetry. Console output remains standard span JSON, one object per line.
Span `start_time` and `end_time` replace custom elapsed fields.

The native `execute_tool` span covers the complete tool invocation, including subprocess
startup, handler work and process exit. One-shot `research tool` commands set the standard
`OTEL_SDK_DISABLED=true`: they do not initialize exporters, write console spans or flush at
exit. There is no separate distribution-handler span for a workspace subprocess. In-process
handlers retain their nested distribution span. A tool of an action's own still reports its
grouped tool/action, such as `claim_write` as `claim` / `write`.

Application logs and SDK self-diagnostics use Python's standard bounded `QueueHandler` and
`QueueListener`. Existing root handlers run on the listener thread, except agent telemetry's,
which only enqueues. Console handlers write one JSON object per record (`telemetry.JsonLines`):
`status` is the level, which Datadog takes as the log's status instead of reading every stderr
line as an error, and a traceback stays inside its record's `message`. The gateway's own stderr
handler switches when the plugin registers and runs its text through Hermes's scanner; other
processes mask Softmax tokens. Production logging
failure handling (`logging.raiseExceptions=false`) drops records if that queue is full.
The image sets `PYTHONUNBUFFERED=1` for both interpreters, so a blocked background write
cannot hold the standard stream's buffered-writer lock during interpreter exit.

No request, tool, readiness check, sleep operation or process exit waits for a telemetry
flush. The provider disables automatic shutdown flushing; Python logging's automatic
handler-flush callback is also unregistered. Pending tail spans/logs can be lost on exit.
This is best-effort diagnostics, not a durable audit trail.

To export through an existing collector, configure `OTEL_EXPORTER_OTLP_ENDPOINT` (base URL)
or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (full trace URL). Standard exporter timeout, header,
TLS and batch settings apply to background delivery. Invalid SDK configuration disables
tracing; invalid remote-exporter configuration preserves console tracing and records
`telemetry.exporter.disabled`. Resource metadata comes from the standard environment
resource detector plus explicit workspace/service identity; automatic resource-discovery
plugins do not run on the application path.

Audit of workspace instrumentation:

| Boundary | Policy |
| --- | --- |
| Application logs and SDK diagnostics | Standard bounded logging queue; no handler delivery on functional threads |
| Native turns, model attempts, tools, HTTP intake and coordinator observations | Standard SDK spans, background batch export, no exit flush |
| Short-lived tool commands | No SDK export; complete call observed by the native parent |
| Turn cleanup | In-memory session/size bounds; no native lease database query |
| Admission correlation | Reuses the required native-message binding read; no tracing-only connection or query; optional annotation cannot fail binding |
| Relay instrumentation | Separate prompt/response exporter stays disabled |

The public-interface system checks exercise real HTTP admissions, durable receipt replay,
real OTLP delivery, stalled/rejected collectors, invalid configuration, full export queues,
a full unread stderr pipe, tool results and process exit. The existing installed-Hermes
flow verifies turn/model IDs and admission links. These follow the standard
[OpenTelemetry batching/error policy](https://opentelemetry.io/docs/specs/otel/trace/sdk/)
and [Python logging queue recipe](https://docs.python.org/3.12/howto/logging-cookbook.html#dealing-with-handlers-that-block).

Hosting must configure an accessible, authorized endpoint before claiming remote trace delivery.
The distribution neither provisions a collector nor assumes a Datadog endpoint. Changing
backend routing requires exporter/collector configuration, not instrumentation changes.
The pinned Relay plugin configuration explicitly disables its separate prompt/response
exporter, so configuring an OTLP endpoint does not enable content capture.

Spans and boundaries:

| Span | Meaning |
| --- | --- |
| `session.open`, `message.admit` | Durable intake, with child transaction events |
| `hermes.session.create` | Native session-create HTTP exchange, including replay |
| `invoke_agent` | Observed pre-LLM callback to session-end callback |
| `chat` | One observed physical model attempt; retries have separate spans |
| `execute_tool`, `tool.boundary=native` | Native pre/post-tool callbacks, including built-ins |
| `execute_tool`, `tool.boundary=distribution` | In-process distribution validation, execution, rendering and scrubbing |
| `workspace.run.observed` | Durable acceptance to coordinator terminal observation, including polling delay |

Native model/tool spans are children of the turn. Hermes isolates tool workers, so distribution
tool handlers explicitly bind the unique active turn for their native session. Subprocesses
carry W3C trace context through `TRACEPARENT`/`TRACESTATE`; AIDE backend HTTP requests use the same
propagator through normal trace headers. Durable asynchronous intake and native execution
continue the platform W3C context carried in relay metadata. Durable message admissions retain
that original context across retries, queued dispatch and restarts. Native model/tool turns reuse
the native-message binding hook and link their asynchronous turn spans to the admitted request
using standard OpenTelemetry span links. Direct native sessions have their own trace. No latest-run
heuristic or process-global session context is used.

Distribution outcomes preserve `validation_rejected`, `tool_rejected`, `store_rejected`,
`internal_error`, `failed`, `succeeded` and `unknown_tool`. Unknown tool/action inputs are
not logged. Native semantic tool errors and failed model attempts set error status. No
arguments, results, messages, prompts, credential values or exception text are captured.

Both tool boundaries end through `telemetry.finish_tool_call`, so every `execute_tool` span carries
`gen_ai.agent.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `session.id`, `turn.id`, `outcome`
and, on a distribution call with one, `tool.action`. A failed call also has `error.type`, a short
class and never text from the call: the distribution's outcome, a refusal's `kind` such as
`transition_refused`, or an exception's class; natively Hermes's own class (`tool_timeout`,
`invalid_tool_arguments`), else one read from the result's structured fields (a distribution
result's `kind`, `timeout` or `degraded` status, `exit_code_nonzero`), else `tool_error`. The same
call logs one line on `research_core.tool_call` with those fields and its duration, INFO when it
succeeded and WARNING when it failed; it replaces Hermes's `Tool X returned error` line, which held
the output and which a logger filter in `plugin/capture.py` drops. A failed `chat` span has `error.type`, Hermes's classification
(`rate_limit`, `server_error`, `unknown`...), and `exception.type`. A stream that fails after an HTTP
200, such as OpenRouter's "Provider returned error" event, has no status code. At session end
`invoke_agent` counts its model attempts, native tool calls and failed ones in
`gen_ai.turn.model_calls`, `gen_ai.turn.tool_calls` and `gen_ai.turn.tool_failures`.
Token usage uses `gen_ai.usage.*`; positive native cache buckets are evidence of reported
usage, while absent/zero buckets do not prove a cache miss. The first-chunk event does not
claim first visible text or first-token latency.

Do not sum overlapping span durations. Waterfalls expose concurrency and gaps; exact busy
unions and unaccounted-time summaries can be calculated from exported intervals. The runtime
no longer maintains a second interval-accounting system. Native and distribution tool spans
measure different boundaries and must not be added together as independent tool counts.

The Hermes adapter retains only live callback correlation. Read-only authoritative leases
reclaim orphaned observations without evicting active calls or backoff. An observed orphan or
missing callback ends with `observation.complete=false` and a reason. Its end is an observation
boundary, not an invented native completion timestamp. Capacity rejection emits a diagnostic
span. Session restoration, isolated retry backoff, and frontend delivery are not inferred.

## Retained-wake fast path

After a profile's install/update and preparation both succeed, the supervisor atomically records
`/opt/distribution/image-id` in that profile's `.softmax-distribution-image`. On another boot of
the same image it skips the Hermes profile-update process. Missing markers and different images
run the normal update, so existing volumes migrate on their first boot of this release. Interrupted
updates cannot record success. Preparation, protected configuration and per-boot credentials still
run every time. Profile-install spans include `profile.updated` to distinguish reconciliation from
reuse. The image ID includes the distribution, runtime and dependencies, not a mutable image tag.

Distribution-owned payload files are release content. The fast path no longer repairs arbitrary
manual edits to those files on every wake; invalidate the marker to request reconciliation. Owner
configuration, conversations and workspace data retain their normal update semantics.

Gateway and application readiness use per-process Unix datagram notifications on systemd's
`NOTIFY_SOCKET`. Each supervisor binds a fresh private socket before launch; old boots cannot
satisfy a new wait. Process exit and the startup deadline fail the wait. The gateway sends Hermes's
own `READY=1` once its adapters, cron and housekeeping run, which needs its systemd watchdog on
(`gateway.systemd_watchdog_seconds` in the host `config.yaml` and `WATCHDOG_USEC`, both one hour). The
application announces its bound listener after store initialization, ownership acquisition and
dispatcher start.
Each notification is followed by one HTTP contract check. There is no periodic startup probe.

See [the polling audit](../docs/reports/workspace-polling-audit-2026-11-09.md) for remaining work.
