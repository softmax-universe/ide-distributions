# Agent telemetry

Every operation of an Orchestrator or Researcher profile goes to Datadog Logs, and one command rebuilds
a Session from Datadog alone. Capture is always on, and there is no setting.

- What a line and a body hold, what each process records, and how records join:
  [`core/src/research_core/agent_telemetry/CONTRACT.md`](../core/src/research_core/agent_telemetry/CONTRACT.md).
- Why the build differs from the plan: [`designs/datadog-agent-logging-build-scope.md`](designs/datadog-agent-logging-build-scope.md).

## What is captured

| Source | What |
|---|---|
| Gateway plugin | Each model request and reply, one per outer attempt, with reasoning and token usage (not the billed cost: see "Known holes"). System prompt sections and context parts. Tool calls as proposed, what ran, and each result as the model got it. Every committed row's ID and its text. Approvals, compaction, subagents, lifecycle, and each log line but the access line of a successful seam poll (a 200 for `GET /p/<profile>/v1/runs/<id>` from its urllib client), which comes once a second per live run. A failed poll and the AIDE backend's run read are sent. All text goes through Hermes's secret scanner first. |
| `research serve` | Each POST to a route in `RECORDED_ROUTES`, whatever its outcome: `/session-open`, `/message`, `/message-cancel`, `/ui-action`, `/store/apply`, `/models`. Also committed store events, coordinator passes, attachment outcomes, wakes and successors. Metadata only, with Softmax tokens masked. |
| Other `research` commands | Their committed store events and tool completions. Metadata only, with Softmax tokens masked. |

The AIDE backend (`image/aide.py`) records nothing itself. See "Known holes".

OpenTelemetry spans (`research_core/telemetry.py`, [`image/README.md`](../image/README.md)) are separate: payload-free timing, which each long-lived process writes to stderr from a background
queue, and can also send over OTLP. A one-shot `research tool` process writes none. Rebuild does not read them and capture does not use them, but `plugin/register.py` imports `plugin/messages.py` and `plugin/tracing.py`, which need the SDK, before it registers capture, so a Hermes without the SDK gets no capture either.

Text is sent once per process, by digest. A request is a manifest that lists digests.

## How it reaches Datadog

`observation -> pickled copy -> memory queue -> redaction -> chunked JSON lines -> container stream -> Datadog Agent -> Datadog Logs`

In the gateway and `research serve` a caller only copies what it was given and puts it on a bounded queue, which drops and
counts what does not fit. A background thread redacts, hashes and writes, so a stuck stream holds up no callback and no
commit through this path. That thread also does the reads whose result it records: the `state.db` rows a turn committed,
read after the turn's end, and `SOUL.md`, read when the request that sends it is written. A store event's
row (an admission, a durable command or a Session) is the exception: `observed.py` reads it on the committing thread, just
after the COMMIT, with one `SELECT` by key. A short command, such as a
workspace tool's process, writes each record to the stream before the capture returns, as any process logs to stdout, and
exits with nothing left to write. The container runtime drains the stream into the pod's log file whether or not the
Datadog Agent reads it, so such a write waits only while the runtime itself stops draining; a tool call then waits too. No
process waits for telemetry at exit, as for spans and logs in [`image/README.md`](../image/README.md).
[`CONTRACT.md`](../core/src/research_core/agent_telemetry/CONTRACT.md), "Lines", says how each line is written.

A record written inside an OpenTelemetry span carries that span's `trace_id` and `span_id`, read from memory, under the
names Datadog joins logs to traces by ([Datadog: correlate OpenTelemetry traces and
logs](https://docs.datadoghq.com/tracing/other_telemetry/connect_logs_and_traces/opentelemetry/)): the spans also reach
APM over OTLP, as service `ide-distributions` by default (`OTEL_SERVICE_NAME`). Datadog takes those two names off a log's attributes, so Logs search
never returns them, and the record carries the trace again as `otel_trace_id`, which the rebuild reads. One user message is one
trace: its admission, the seam's dispatch and every Hermes turn that works on it, including one that resumes it
(`plugin/tracing.py`). Records join on these IDs, not on Hermes's runs.

`research_core/telemetry.py` moves the root logger's handlers behind its own queue, which drops and counts a record that does
not fit rather than wait, at a process's first span, and the gateway at plugin registration. The gateway's
log handler stays on the root logger, because it only enqueues.

Other output shares this stream and can leave a line open at any moment, so each telemetry line starts with a newline of its own, and that output cannot join it (`test_real_prints_beside_telemetry_lines_on_one_pipe_lose_no_line`). The price is a blank line on the stream for each telemetry line: see "Proof and cost". [`stream.py`](../core/src/research_core/agent_telemetry/stream.py) says why buffering would not do.

The Agent reads the pod's log files, so a line written just before a pod goes away still arrives: real sleep and default delete
each lost nothing with no wait, one run each. A line is searchable 12 to 27 seconds after it is written.

### What is knowingly lost

| Loss | Why it is accepted |
|---|---|
| Nothing confirms delivery. A line that never arrives is not noticed, and a stream that cannot be opened stays closed for the life of that process | There is no sender to tell. Rebuild reports what a surviving record refers to |
| Any exit of the gateway or `research serve`, graceful or not, loses what the memory queue still holds: across 20 turns of a real gateway with tool results of 40,000 characters, 629 captures found at most 4 records queued (median 1), at most 945 KB. A short command has no queue. A request is written before its call, so a process killed during a call keeps the request, and with it the user's message, once the writer has written it, and rebuild lists the attempt in `unended_attempts` (`test_a_kill_during_a_call_keeps_its_request`, `test_a_kill_during_the_first_call_keeps_the_users_message`). The turn's committed rows are lost, because they are read at the turn's end. A kill while the request is still queued can leave `model_request` / `started` alone, and rebuild names that call `call_with_no_attempt` | The queue empties as records arrive, and nothing on the agent's path may wait for it |
| The Agent falling behind by more than the retained log files would lose lines. Untested | 240 MB over about 15 rotations lost nothing with the Agent keeping up |
| A full queue drops records, which a stream that has stopped soon causes. The writer's thread logs the count each time it changes. A capture over the 64 MiB byte limit goes in only when nothing is queued, so a request that large captured just after another record, such as its own context parts, is dropped, and rebuild shows `call_with_no_attempt` and, for its reply, `reply_with_no_request` | A caller must never wait |
| Any process in the workspace can write a line that looks like ours | James: restriction work matters very little here |

## Read a Session

The content sits in service `ide-agent-telemetry`, in Datadog's `main` index, and a Restricted Dataset
selects it. On 2026-11-05 at 17:28 Pacific a freshly written test line, sent straight to Datadog's intake
rather than through a pod's stream, was read by `datadog.content.read` (1 line) and not by `datadog.read`
(0 lines). Metta #30918 removed the separate index, and the `datadog` stack reports no changes. A line from
a real pod's stream has not yet been checked; the first real capture does that. In a checkout of this
repository:

```bash
python3 ~/coding/metta/scripts/token_broker_client.py exec --scope datadog.content.read \
  --reason "why you are reading this Session" -- \
  uv run research telemetry rebuild --session <session id>
```

Add `--workspace <id>` if the command says several workspaces hold that ID. `--session` takes any ID of
the Session, first or after compaction. The command needs only `DD_API_KEY` and `DD_APP_KEY`: no
state.db, volume or workspace API. It exits 0 when nothing was missing, 1 when something was, and 2 when
it could not start, which includes Datadog refusing the search, not answering, or answering that a search did not
finish (`meta.status` other than `done`, such as `timeout`) or that warns (any `meta.warnings`, even with
status `done`), because such an answer can hold part of the results.

It prints one JSON object. Each entry is the record as written, with the text of what it refers to under
each reference's name:

- `requests`: each model request in wire order, whole, with `response`, `failed` or `unread` (the record
  of how the attempt ended), `partial_responses` (what each broken stream of the attempt had shown),
  `tools` (its tool activity, including each `result`), and `tokens_by_source`.
- `unended_attempts`: the `model_attempt_id` of each request with no reply, failure or `unread` record (a partial reply is no end, because Hermes can retry the stream): a process killed during the call, a cancel (Hermes's interrupt fires no hook), or a call still running. It is not a loss, so it does not change the exit code.
- `rows`: each committed native row, with `message` as it was first sent and `rewritten_to` if a prune
  later changed it. `message` means "first sent" only when `missing` is empty: if a row's original was
  lost, it shows the first form that survived. A row no request lists never reached a model, such as a
  turn's last reply or the text Hermes's tool guard ends a turn with, or the request that listed it was
  lost. It shows the text its commit recorded, except a tool row, which shows the result recorded for
  it, as Hermes passed it on, and its commit's text only when there is no such result.
- `events`: admissions, model calls started, a call that failed before any attempt of it was recorded, store events, approvals, lifecycle, compaction, log lines and everything else. Each
  holds its own IDs, and `CONTRACT.md`, "How to join them", says how they join.
- `context`: where each piece of a user message came from.
- `missing`: see below.

### Token counts by source

There is no tokenizer. The command counts each source's characters in the rebuilt request and shares the
provider's prompt tokens out by those shares, so `estimated_tokens` is an estimate. Text no source claims
counts as `system_unattributed` or `user_unattributed`. A request whose reply has no provider usage shows
characters only.

### What "Missing" means

| `kind` | Meaning |
|---|---|
| `no_records` | Datadog has nothing under this Session ID |
| `no_content` | A reference names a digest with no log entries |
| `chunk_gap` | Entries exist, but no sending of the object supplied these chunks: `missing_chunks` names them |
| `digest_failure` | The joined chunks do not match the digest or size |
| `row_without_text` | A committed row that no request lists and whose own text is unknown: its commit text or a tool row's result was lost, or two results with no row ID share its call ID |
| `call_with_no_attempt` | A call started and no attempt was written: its request record was dropped or lost |
| `reply_with_no_request` | A reply, failure or partial reply names an attempt whose request record is lost. One dropped oversized request shows as this and `call_with_no_attempt`: one loss, not two |
| `manifest_mismatch` | A request's manifest chain lists `messages_listed` messages, not the `message_count` its manifest says, so the request is not rebuilt |
| `no_request_manifest` | A request record names no manifest, so the request is not rebuilt |

A missing object blocks each request that needs it, and that request shows `blocked_by`. Nothing is guessed.

A line becomes searchable 12 to 27 seconds after it is written, not always in order, so a rebuild within about 30 seconds of a Session's last record can show a `call_with_no_attempt` or `reply_with_no_request` that clears when it is run again.

**An empty list is not proof that a Session is complete.** It says only that every reference found could
be followed. A record that never reached Datadog leaves no reference to follow. A Session older than 15
days loses its early operation lines, and the report can then show `missing: []`, starting partway
through, with exit 0.

### Retention

**A request can be rebuilt for up to 15 days from when its content was sent.** Promise no more than this.
Datadog keeps a line for 15 days, and a process sends each object once. Every request lists the system
prompt and tools, so past day 15 no request from a long-running process rebuilds until that process
restarts and sends everything its next request lists again. The rebuild reports each as `no_content` and
`blocked_by` (`test_content_past_retention_is_reported_missing`). A workspace sleeps after 15 minutes with
no work (`image/control.py`, `Lifecycle.status`), so this takes work with no such gap for 15 days, or stuck
work that holds the workspace awake.

## Known holes

Each was checked against the pinned Hermes, which carries no telemetry patch since 2026-11-09.

| Hole | Evidence |
|---|---|
| `redaction_version` does not tell the Softmax token patterns apart: #265 (merged 2026-11-10 22:37 Pacific) widened them from `usr_` and `ply_` to every credential prefix but `code_` and kept `_SOFTMAX_RULES` at `softmax-1`. A gateway line says `hermes-<version>+softmax-1` either way, and a line from another writer says `none`, so a line cannot show whether a `cga_` or other new-prefix token in it would have been masked. | `agent_telemetry/redaction.py`, `_SOFTMAX_RULES` |
| `ready_for_sleep` does not prove the queues are empty. `research workspace sleep` is its own process and cannot reach them; the queues empty as records arrive, and an exit loses what they still hold. | `agent_telemetry/writer.py`; `test_the_queue_empties_with_no_flush` |
| Hermes's scanner has gaps: a short value under a secret-looking key inside a JSON string, so `tool` / `proposed` arguments and manifests can keep what `tool` / `completed` masks; a password in a URL; and the prefix and suffix it keeps of Bearer and AWS tokens. Writers outside the gateway have no scanner, only the Softmax token patterns. | `agent_telemetry/redaction.py` |
| An operation or context line whose metadata would pass 4,096 bytes is dropped with a warning. The model can choose a tool name or call ID, and a client the IDs of a refused request, so a long one costs its record. | `agent_telemetry/encoder.py`, `_emit` |
| What was sent is known only per process, so each restart sends everything the next request lists again. That costs bytes and loses nothing. | `test_a_restart_sends_a_full_manifest_and_everything_it_lists` |
| The seam records no read and no other POST: `GET /snapshot`, which the AIDE backend also uses to select a Session, leaves no record of its own, and neither do `/message-events`, `/input-response` and `/messages/context`. | `serve.py`, `RECORDED_ROUTES` |
| A request the AIDE backend refuses itself leaves no record: an unknown method, invalid or multi-message params, a scope mismatch, a Coworld session with no league, inference unavailable, a stale input response, a cursor it never issued. A handler's unexpected failure answers `unavailable` and leaves only a log line. What it sends on to a route in `RECORDED_ROUTES` is recorded by the seam. A failed consumer or event watcher ends the backend and recycles the runtime, and no record here shows that it happened or why: the gateway's lines after it only carry a new `source_instance`. The backend is the only chat path (`image/README.md`). | `image/aide.py`, `Backend.answer`, `Backend.touch`, `Backend.open`, `Backend.send_chat`; `image/runtime.py`, `run` |
| A streamed auxiliary reply is not captured, because Hermes hands it back unread: `model_attempt` / `unread`. | `plugin/capture.py`, `post_auxiliary_call` |
| Capture uses Hermes's own hooks only, so it sees one record per outer attempt: a stream Hermes retries inside one attempt (`HERMES_STREAM_RETRIES`) leaves no record of the retry. | `agent/chat_completion_helpers.py`, `_StreamingCall`; `plugin/capture.py` |
| A request is the `llm_request` middleware's view: `stream` and `stream_options` are added after it. | `agent/chat_completion_helpers.py`, `_call_chat_completions` |
| A broken stream keeps the text it showed and its streamed reasoning, not the partial arguments of a tool call it was writing. | `on_stream_end`; `plugin/capture.py` |
| The provider-billed `cost` is not captured: `post_api_request` passes a token summary without the provider's raw usage. | `agent/api_request_hooks.py`, `_usage_summary_for_api_request_hook` |
| Auxiliary requests and replies are Hermes's capped hook payloads (`HERMES_PLUGIN_PAYLOAD_MAX_CHARS`, 50,000 characters, 8,000 per string). | `agent/auxiliary_hooks.py` |
| Prompt sections name SOUL and this plugin's own sections, found by their text. Hermes's guidance, the project's rules and the skill index are unattributed. | `plugin/capture.py`, `_sections` |
| Committed rows are read from `state.db` on the writer's thread after each turn's end. Rows of a turn that a killed gateway never ended are not recorded (they stay in `state.db`), and a new process records only rows written after it started. Any gateway exit, graceful or not, also loses what the writer still had queued. | `plugin/capture.py`, `_capture_commits` |
| A compaction partway through a turn, done in place as the pinned Hermes's `compression.in_place: true` default does, may leave the rows that turn committed before it uncaptured (Hermes archives them as `compacted`, which the turn's read skips) and capture the kept tail twice, as the fresh copies Hermes inserts. Unverified: read from the code only. | `plugin/capture.py`, `_capture_commits`; Hermes `agent/conversation_compression.py` |
| `extra_headers` and `extra_query` are not captured, because they are not part of the request body. | `plugin/capture.py`, `wire_request` |
| After a restart the first Session ID is lost too. Rebuild follows `parent_session_id` on `compaction` records. | `test_compaction_is_followed_in_both_directions_after_a_restart` |
| Hermes gives a row ID only to rows committed since its last prune or restart, so a `full` manifest after one names few rows. Text is not lost: rebuild joins by row ID first, and a listing with none through its tool call ID, unless the row's own result has other text. | `test_each_committed_row_equals_state_db` |
| A listing with no row ID joins the row that last held its tool call ID, so when a later, different row reuses that ID (Kimi does across turns) and is listed with no row ID, the older row can show a false `rewritten_to`. | `agent_telemetry/rebuild.py`, `_rows` |
| The row Hermes makes to keep a pruned original has no commit record, and neither does a `session_meta` row. The original's text is the first form of the pruned row. | `test_each_committed_row_equals_state_db` |
| A Session's report does not name its subagents' Sessions, because their records have another Session's ID. Search `service:ide-agent-telemetry @parent_session_id:<id>`, then rebuild each. | `agent_telemetry/rebuild.py`, `_Finder.owned` |
| An auxiliary call outside a turn has session ID `no-session`, so a Session's report does not show it. A log line with no Session tag has no session ID, so the report shows it only when it was written inside one of the Session's traces, where it carries `otel_trace_id`. | `plugin/capture.py`, `DiagnosticsHandler.emit` |
| Hermes sends a tool call's arguments as compact JSON and its row keeps the model's text. A request shows the first, and `tool` / `proposed` keeps the second. | `_comparable` in `test_rebuild_real.py` |
| Across a restart, requests are ordered by each process's earliest time, because `sequence` compares only inside one process. This assumes one gateway process at a time and a clock that did not go back. | `agent_telemetry/rebuild.py`, `_order` |

## Proof and cost

`core/tests/hermes/test_rebuild_real.py` runs a real gateway through a real `research serve`, with tool calls, a prune, a restart and a tool-guard halt, feeds every line to a fake Logs search, and compares the rebuild with what a stub model received and each row of `state.db`. No real agent content has yet been rebuilt from real Datadog.

On 2026-11-06, capture added about 4 to 5 ms to a tool cycle at the median, but that was measured on the earlier capture through the former Hermes telemetry patches, and the current unpatched capture has not been measured. On 2026-11-05 a gateway held 386 MiB with capture and 387 without after 100 turns, and an 8 MB request cleared the writer in 1.1 s. On 2026-11-06 a 66 MiB request through the writer alone, with a stand-in for Hermes's scanner, took a process from 88 MiB to a peak of 419 MiB and took 3.9 s to clear. There is no exit flush, so an exit while a request is still clearing loses what is left of it.

A 100-turn Session, about 200 model calls, writes 5,358 lines and 5.2 MB with 5,000-character tool results, and 6,496 lines and 8.9 MB with 20,000. A user row's commit text adds almost nothing, because the same message from a request is already stored under its digest. At 1,105 model calls a day (the highest day in the plan's Appendix I), that is an estimated $2.30 and $2.82 a month, at a third-party $0.10 per GB and $2.50 per million events for 30-day retention while content is kept for 15, so check them against the contract. Each line also leaves a blank line on the stream. By reading the Agent's source (`pkg/logs/internal/decoder/line_parser.go`) a blank line is forwarded, and whether Datadog then keeps it as an empty event under `service:hermes-runtime` is not measured. If it does, the event count doubles and the estimates become $4.51 and $5.51, and those empty events show among that service's logs.
