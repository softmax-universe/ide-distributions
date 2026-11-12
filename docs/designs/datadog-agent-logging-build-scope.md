# Datadog agent logging: build scope

What is being built from [the plan](datadog-agent-logging-2026-11-03.html). Where the two disagree, this file wins. The plan
stays useful for evidence, Hermes observation points and Datadog limits, but its outbox and Worker are gone.

James's instruction: "Do not add any gates, testing/deployment flags, extra safety mechanisms, or so on ... the initial
implementation should be tight and minimal." The goal: everything about agent operations reaches Datadog, and a Session can
be rebuilt from Datadog alone. [`docs/agent-telemetry.md`](../agent-telemetry.md) says how to read it and what it cannot show.

## The shape

`observation -> pickled copy -> memory queue -> redaction -> chunked JSON lines -> /proc/1/fd/1 -> Datadog Agent -> Datadog Logs -> rebuild command`

A short command skips the copy and the queue: it masks Softmax tokens and writes each record before the capture returns.

- Orchestrator and Researcher profiles. Capture is always on, with no setting.
- The gateway plugin records context, requests, replies, tools and lifecycle, from Hermes's own hooks and middleware
  (`plugin/capture.py`). Hermes is not patched since #252. `research serve` and every other `research` command record
  metadata. Only the gateway writer takes free text.
- The AIDE backend (`image/aide.py`) is the only chat path and records nothing itself. The image's own bridge
  (`image/application.py`), with its refusal records and request-ID header, was removed in #236.
- Datadog Logs holds the content: service `ide-agent-telemetry`, in the `main` index at 15 days.
- `research telemetry rebuild` reads only Datadog's Logs search API, and prints JSON.

## Decisions

| Decision | Why |
|---|---|
| Pod logs, not an outbox, exporter and Worker | The Agent already collects container output. The pod test below lost nothing. |
| Lines are at most 4,096 bytes, with 3,000-byte chunks, written to `/proc/1/fd/1` | `CONTRACT.md`, "Lines", says why. |
| Each line starts with a newline of its own | Anything else on the stream can leave a line open, which would join the next telemetry line. Removing `PYTHONUNBUFFERED=1` would not end that, and would bring back the exit abort #223 removed. `stream.py` says why. |
| Stdout's stream, not stderr | Canary stderr lines arrived with `status:error`. |
| Each line names its workspace, from `bootstrap.json` | Pod tags do not identify the owner. |
| The gateway and `research serve` each have one queued writer: a caller pickles and enqueues, and one thread writes. A short command, such as a workspace tool's process, writes each record inline with a blocking write (`InlineWriter`) | A hook or the seam never waits. A short command has nothing left to write at exit. #258 reverses the earlier choice of one queued writer for every process. That choice was made because a direct writer could stall a tool past its commit on a stuck stream, but the queued writer then waited up to 2 s at exit, lost the records, and held the tool call for that wait. Now a write waits only while the container runtime stops draining the stream, and keeps the records. A non-blocking write was tried and dropped a tool's own records when the pipe was briefly full. |
| No process waits for telemetry at exit. There is no exit flush and no shutdown code | Hooks must never wait. The gateway's exit flush went with the Hermes patches in #252, and `research serve`'s and the commands' in #258. An exit of the gateway or `research serve` loses what its queue still holds: on a real gateway, at most 4 records (`agent-telemetry.md`, "What is knowingly lost"). |
| The gateway's log handler stays on the root logger when `telemetry.configure_logging` moves the others behind its queue | It only enqueues. Behind that queue a full stderr pipe stopped it: 0 of 5 lines arrived, against 5 of 5 on the root logger (measured 2026-11-06). An exit can also lose what is queued. |
| A full queue counts its drops, and the writer's thread logs each new count | Logging on a caller's thread can block on the same full pipe. |
| Writers outside the gateway mask Softmax tokens in metadata strings: every prefix of metta's `CredentialTokenPrefix` but `code_` (`redaction.py`, `SOFTMAX_TOKEN_PREFIXES`; widened from `usr_` and `ply_` in #265) | A client chooses `actor` and `client.kind`. The patterns are plain regexes, so this needs no Hermes. |
| Bodies come from Hermes's own hooks: the `llm_request` middleware, registered last, keeps the whole request, `pre_api_request` records it with its history's row IDs, and `post_api_request` gives the reply. One record per outer attempt | Hermes is not patched (#252). This reverses the earlier choice of a patched `on_model_attempt` callback, which also saw `stream`, `stream_options` and a stream Hermes retries inside one attempt. Those are now lost (`agent-telemetry.md`, "Known holes"). The request is written before the call, so a process killed during a call keeps it once the writer has written it. |
| Messages are native JSON addressed by digest, with cache markers in the manifest | Markers move at every request. In the manifest, 95.5% of manifests stayed `append` over 100 real turns, against 0.5%. |
| Content stays in `main` at 15 days, sent once per process | A separate 30-day index was refused with 403 "Out of contract retentions are not authorized", and Flex is not on this org. James chose `main`. `agent-telemetry.md`, "Retention", says what this promises. |
| Store events are observed after COMMIT, with allowed metadata listed per event | A transaction can roll back, and payloads hold exception text. |
| Admission, command and Session fields come from the row, read after the commit | Cheaper than changing about a dozen event payloads, and no new call sites. |
| aiohttp's access lines are sent, except the seam's own successful `GET /p/<profile>/v1/runs/<id>` | They are the only record of a direct call to the gateway, such as the AIDE backend's, and its status. The poll runs once a second per live run and says nothing the seam's records lack. |
| Rebuild orders by process, then time, then `sequence` | A record carries the time of what it describes, not of its writing: a request its start, a reply its end, and a committed row its own timestamp, read after the turn's end. |
| A row's text comes from its first listing, `rewritten_to` from its last. Join by row ID first, and a listing with none only if the row's own result agrees | Hermes's prune rewrites a row in place and drops row IDs after it. A call ID can come back on another row. |
| A row no request lists takes its own text: from its commit record, or a tool row's from its result. Nothing else stands in | A turn's last rows reach no later request, and Hermes's tool guard writes its text with no model call, so "the last reply" was the wrong row. |
| Records join by OpenTelemetry trace, not by Hermes run: one user message is one trace, and a record written inside a span carries its `trace_id` and `span_id`, and the trace again as `otel_trace_id` | #258 replaced the `run_id` joins. Datadog takes `trace_id` and `span_id` off a log's attributes, so Logs search never returns them, and the rebuild reads `otel_trace_id` (#275). `CONTRACT.md`, "How to join them", says how each record joins. |
| The package is `research_core/agent_telemetry`, not `telemetry` | `research_core/telemetry.py` is #186's OpenTelemetry module. A package of that name would hide it, and git reports no conflict. |

## Measured on James's workspace, 2026-11-05 (raw results are not in the repo)

| Measure | Result |
|---|---|
| One writer, largest line that arrives whole | 900,000 bytes. Above that, text lands unparsed under `hermes-runtime`. |
| 16 concurrent writers, 496 lines per size | 4,096 bytes: all whole. 4,097: 49 unparsed. 16,000: 125 unparsed, 8 valid JSON with the wrong content. |
| Bursts of 1, 10, 60 MB and paced 240 MB over about 15 rotations | No loss, no duplicates |
| Real sleep and default delete, about 0 s wait, one run each | No loss |
| Written to searchable | 12 to 27 s |

The pod test used probe lines. No real captured Session has been rebuilt from real Datadog yet. Overhead, memory, backlog and
cost figures are in `agent-telemetry.md`, "Proof and cost". The benchmarks that took them are deleted.

## Cut: do not build

- **The delivery path:** SQLite outbox, exporter, platform submit route, Worker admission, R2 objects, queue, Logs consumer,
  receipts, delivery retries, a record contract in metta, `delivery_id`, segments, cross-process flush signalling.
- **Controls:** capture setting, per-workspace override, default-off rollout, capture on/off intervals, size caps, upload limits,
  Datadog quota, cap alerts.
- **Health and alerting:** health and gap records, checkpoints, coverage states, monitors, alerts, counters, a daily synthetic
  Session, scheduled rebuild, registry gate in CI, runbook.
- **Elsewhere:** Sensitive Data Scanner, LLM Observability, browser logging, RUM, cron inventory, a sleep handshake, runtime
  tokenizers, a generic final-request patch, an extra invocation ID.

## Rules for builders

- `research_core/agent_telemetry` and `plugin/capture.py` import only the standard library, because they run in Hermes's
  interpreter. Since #186 the rest of the plugin also imports the OpenTelemetry SDK there; capture still does not.
- Synchronous code. Plain words. Tests start their own Sessions.
- Access work stays small (James: "very little"). `agent-telemetry.md`, "Read a Session", says what is checked.

## Future list

Considered and not built. Do not add these or others without asking James: resending content by age for a process that runs
for weeks, pruning the in-memory record of what was sent, a
check that a line reached the Agent, reopening a stream that failed to open, rejecting a metadata line whose caller used the
wrong field name, logging an `observe_commit` failure off the caller's thread, proof that queues are empty at `ready_for_sleep`,
a `status: timeout` flag on rebuild's partial results, the rebuild saying when a Session's earliest surviving record isn't its
start.
