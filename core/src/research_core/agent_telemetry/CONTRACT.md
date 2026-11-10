# Agent telemetry lines and bodies

This page says what reaches the container's stream, and what the `body` of a content object holds. This repository writes
them and reads them, and `records.py` defines the format: no other code parses them.

`rebuild.py` beside this file is the reader, from Datadog: see `docs/agent-telemetry.md`.

## Lines

Each process writes to `/proc/1/fd/1`, the stream of PID 1, and never its own stdout, because a tool process's last stdout
line is the tool's result. `IDE_TELEMETRY_STREAM` names another path, for tests and local runs. With neither, as on a Mac,
nothing is written.

A line is a newline, one JSON object and a newline, written with one `os.write`. **It is at most 4,096 bytes, measured as the
UTF-8 of the whole line with its JSON escapes and both newlines.** Above that size concurrent lines tear on the way to Datadog.
The first newline ends any line that other output on the stream has left open, so that output cannot join a telemetry line, and
it leaves a blank line when none was open. The Datadog Agent parses each line, so its fields become the log entry's attributes.

Every line has:

| Field | Holds |
| --- | --- |
| `service` | Always `ide-agent-telemetry`. The Restricted Dataset selects by it, so only `datadog.content.read` reads these lines: `docs/agent-telemetry.md`, "Read a Session", says how far that is checked. Search it as `service:ide-agent-telemetry` |
| `workspace_id` | `IDE_WORKSPACE_ID`, or `unknown` outside the image. Pod tags do not identify a workspace's owner |
| `producer`, `image`, `source_instance`, `runtime_id` | Which process wrote it. `source_instance` is new at each process start |
| `redaction_version` | Which scanner read it, or `none` from a writer that takes no free text. Such a writer still masks Softmax tokens in its metadata strings |
| `occurred_at` | UTC with three decimals. Datadog's own timestamp is when it read the line |
| `sequence` | Capture order inside one `source_instance` |
| `family` | `operation`, `context` or `content` |

An `operation` line adds `event_id`, `action`, `stage`, its metadata and `content_refs`:

```json
{"service":"ide-agent-telemetry","workspace_id":"58bda2da-38b3-4e89-9d61-5df046c11134","producer":"gateway","image":"distribution-8e66497bd5a8b175","source_instance":"gateway-4f1c09ab22d7","runtime_id":"6f0e1fd2b7a94c52","redaction_version":"hermes-0.22.6+softmax-1","occurred_at":"2026-11-05T20:31:14.123Z","sequence":42,"family":"operation","event_id":"0199014d-55f8-4b70-a8eb-000000000002","action":"tool","stage":"completed","session_id":"ses_1","tool_call_id":"call_1","tool_name":"read_file","outcome":"success","content_refs":[{"name":"arguments","digest":"<digest>"}]}
```

A `context` line adds `event_id`, `source_type` and where the source sits:

```json
{"service":"ide-agent-telemetry","workspace_id":"58bda2da-38b3-4e89-9d61-5df046c11134","producer":"gateway","image":"distribution-8e66497bd5a8b175","source_instance":"gateway-4f1c09ab22d7","runtime_id":"6f0e1fd2b7a94c52","redaction_version":"hermes-0.22.6+softmax-1","occurred_at":"2026-11-05T20:31:14.123Z","sequence":43,"family":"context","event_id":"0199014d-55f8-4b70-a8eb-000000000003","source_type":"notice","session_id":"ses_1","size_bytes":41,"target_digest":"<digest>","byte_offset":0,"byte_length":41}
```

A `content` line is one chunk of one object:

```json
{"service":"ide-agent-telemetry","workspace_id":"58bda2da-38b3-4e89-9d61-5df046c11134","producer":"gateway","image":"distribution-8e66497bd5a8b175","source_instance":"gateway-4f1c09ab22d7","runtime_id":"6f0e1fd2b7a94c52","redaction_version":"hermes-0.22.6+softmax-1","occurred_at":"2026-11-05T20:31:14.123Z","sequence":41,"family":"content","content_kind":"part","content_digest":"<digest>","size_bytes":5000,"chunk_index":0,"chunk_count":2,"body":"<at most 3,000 bytes as escaped>"}
```

`content_digest` is the SHA-256 of the whole object's UTF-8 bytes, and `size_bytes` their count. Text goes in `body` and never in
`message`, because Datadog trims and parses `message`. An object can be shared, so its lines carry no IDs of a conversation:
find a manifest through its operation line's `content_refs`.

### Chunks

`records.split_body` cuts a body into the longest pieces of at most 3,000 bytes, measured as escaped UTF-8, never inside a code
point. That leaves 1,094 bytes for the rest of a line, and the fields above take about 540.
To read an object, join its chunks by `chunk_index` and check the digest and the size.

**The pieces depend on the body alone.** So `content_digest` and `chunk_index` name a chunk wherever and whenever it was sent,
and two sendings of one object are interchangeable, chunk by chunk.

An `operation` or `context` line is never split. If its metadata would pass the limit, that is a bug in the record, and the
writer logs a warning and drops that line.

### Duplicates

The same chunk can reach Datadog more than once, because a new process knows nothing of what the last one sent. A line could also be read twice by the Agent, though none was in the pod test. A duplicate has the same `event_id`, or the same `content_digest` and
`chunk_index`, and a reader drops it by those keys. Nothing else identifies a sending.

## Objects

Every structured body is canonical JSON: sorted keys, no spaces, UTF-8, as `records.canonical_bytes` writes it. Every text
in it has been through the gateway writer's redaction. Digests and byte offsets refer to the redacted form.

| `content_kind`     | Holds                                                                            |
| ------------------ | -------------------------------------------------------------------------------- |
| `part`             | A native message, a native tool, the request options, or plain text              |
| `tool_list`        | The tools of one request, in order                                               |
| `prompt_sections`  | Where each source's text sits in one system prompt                               |
| `request_manifest` | One model request, by reference                                                  |

An object with no bytes has no line. Its digest is the SHA-256 of nothing, and a reader treats it as empty.

## Native messages

A message is stored as the JSON object the provider got, after redaction, and nothing else. A field that was omitted is
omitted, an empty one is empty, a null one is null, and a tool call of any shape stays where it was. A base64 `data:image/`
URL is left out of the scan, because a pattern can damage it and cannot read it. Key order is not kept, because JSON gives it
no meaning. Two messages that are the same on the wire are one object, in any Session of the workspace.

## Request manifest

```json
{
  "operation": "append",
  "parent_manifest_digest": "<digest>",
  "message_count": 3,
  "messages": ["<digest of a native message>"],
  "hermes_message_ids": { "2": "1188" },
  "cache_markers": [{ "position": 2, "at": "text", "cache_control": { "type": "ephemeral" } }],
  "prompt_sections_digest": "<digest>",
  "tool_list_digest": "<digest>",
  "options_digest": "<digest of JSON>"
}
```

- `operation` is `full` or `append`. A `full` manifest lists every message: it is the first this process saw of this history,
  as after a restart, or the messages differ from the last ones. An `append` lists only what was added, and
  `parent_manifest_digest` names the manifest before it.
- `message_count` is the length of the whole history after this manifest, its system message included.
- `hermes_message_ids` maps a position in the whole history to the native message ID. It sits beside the list because the
  provider never saw it. It names each ID the manifests before it in the chain did not, so it can name a message listed earlier
  if Hermes gave that message its ID later. Hermes gives an ID only to rows committed since its last prune or restart. A
  reader joins a row by this ID first. A listing with none joins through `tool_call_id`, unless the row's own `tool` / `result`
  has other text, because a call ID can come back on a new row.
- `cache_markers` says where the cache markers of this request go, for the whole history. Unlike the messages it is never
  inherited: only the manifest being rebuilt counts. Hermes moves its markers to the last messages at each request, and a
  marked string becomes a list of one part. A stored message has no marker, so it is the same object at every request and an
  ordinary conversation stays an `append`. `position` counts as in `hermes_message_ids`. To rebuild, set `cache_control`:
  - `at: "message"` on the message.
  - `at: "text"` by turning the message's string `content` into `[{"type": "text", "text": <it>, "cache_control": ...}]`.
  - `at: "part"` on the part of `content` numbered `part`.
- Later in a history, `full` means the messages really differ from the last ones, as after Hermes prunes old tool results. A
  message sent as a list of one text part with a marker is stored as a string, so if a later request sends that part with no
  marker, that request is `full` too. Hermes does not do this with a string it wrapped.
- A system message is listed like any other message. A changed prompt makes the next manifest `full`, which is a list of
  digests: text already sent is not sent again. The hosted route cuts the prompt into parts to place cache markers, and the
  message is stored without them.
- `prompt_sections_digest` is present when the first message is a system message and its sections are known. Its offsets are of
  that message's text: its string, or its text parts joined with nothing between.
- `tool_list_digest` is present when `tools` is a list with something in it.
- `options_digest` names every other field of the request as native JSON, including `model`. An omitted, empty or null
  `tools` is here too, as it came, so those three stay distinct. It is absent when there are no other fields.

## Rebuilding a request

1. Walk `parent_manifest_digest` back to a `full` manifest. Join each manifest's `messages`, oldest first. Check the length
   against `message_count`.
2. Read each digest as a message, and put back the cache markers.
3. Start from the JSON at `options_digest`, or `{}`. Set `messages`. If `tool_list_digest` is present, set `tools` from each
   digest it lists, in order.

The result equals the request the provider got, after redaction. A missing object blocks that request. A lost manifest also
blocks the requests that walk back through it, until the next `full` manifest. A lost message object stays lost for every later
request of that process that lists it, because a `full` manifest lists the same digest and does not send it again. Report it by
digest instead of guessing. `docs/agent-telemetry.md`, "Retention", says how long a request can be rebuilt.

## Tool list

```json
{ "tools": ["<digest of a native tool>"] }
```

## Prompt sections

```json
{ "sections": [{ "name": "soul", "byte_offset": 0, "byte_length": 22 }] }
```

Offsets are UTF-8 bytes of the redacted system text, which is in the request's system message. A reader counts bytes that no
section covers as unattributed. A section is placed as it was sent: when request middleware drops text from it, it covers what
is left, and the dropped text is in no section. A section that cannot be found in the sent text is left out.

## Context records

A context record's `target_digest` is the SHA-256 of a message's text after redaction: its content string, or its text parts
joined with nothing between when its content is a list. No object has this digest. Find the message by hashing the text of each
message of the request. `byte_offset` and `byte_length` are UTF-8 bytes of that text. A reader counts bytes no record covers as
unattributed. A part that could not be found has `source_type: unattributed` and no place.

## Operations the writer adds

`capture_request` writes an operation with `action: model_attempt`, `stage: request` and a `content_refs` entry named
`request_manifest`. `capture_response` writes one with `stage: response`, an entry named `message` for the native assistant
message, and `partial`, which is true when a stream failed and the message is what arrived before that. A reply that is not a
message, such as a 200 of plain text, is stored as `{"unparsed_reply": <what came back>}`. Each carries the fields its caller
passed. All other operations name their own action and stage.

## What the gateway records

`research_core/plugin/capture.py` writes these. `session_id` is the ID a Session started with, and `hermes_session_id` is
its live ID, which compaction can change. A process that restarts after a compaction no longer knows the first ID, so a
reader follows `parent_session_id` on the `compaction` records.

| `action` / `stage`                                    | Content refs                 | Says                                                                  |
| ----------------------------------------------------- | ---------------------------- | --------------------------------------------------------------------- |
| `model_request` / `started`                           |                              | A call is on its way (`pre_api_request`)                              |
| `model_attempt` / `request`, `response`               | `request_manifest`, `message` | One outer attempt, written before the call. `model_attempt_id` is `<api_request_id>#<n>`. A stream Hermes retries inside one attempt is one attempt. `native_event_type` is `request` or the auxiliary task. The request is the middleware's view: `stream` and `stream_options` are added after it. A broken stream's `response` is `partial`, with its shown text and any streamed reasoning (`on_stream_end`). When Hermes retries the stream inside the attempt, the attempt has both a `partial` and a whole `response` |
| `model_attempt` / `failed`                            | `error`, if it has text      | Hermes's verdict on a failed attempt (`api_request_error`): `reason`, `duration_ms` and any status. It joins the attempt by `api_request_id`. A call that failed before any attempt of it was recorded, such as one whose request could not be built, has no `model_attempt_id` |
| `model_attempt` / `unread`                            |                              | A streamed auxiliary call, whose reply Hermes hands back unread       |
| `tool` / `proposed`                                   | `arguments`                  | One call the model asked for, from the reply Hermes assembled         |
| `tool` / `completed`, `refused`                       | `arguments`                  | What ran, with `outcome`. The result is on the next one               |
| `tool` / `result`                                     | `result`                     | The result as the model got it, from the first request that sends it, with its `hermes_message_id` |
| `message` / `committed`                               | `message`                    | A row of `state.db`, read at the end of each turn (`on_session_end`), with `hermes_message_id`. `occurred_at` is the row's own timestamp. `message` holds the row's `role`, `content`, `tool_calls` and `reasoning_content`, because a turn's last rows reach no later request |
| `approval` / `requested`, `decided`                   | `command`, `description`     | A human approval                                                      |
| `system_prompt` / `built`, `compaction` / `committed` |                              | The prompt was assembled; the history was rewritten                   |
| `session` / `started`, `turn` / `ended`, `subagent` / `started`, `stopped` | | Lifecycle |
| `diagnostic` / `<log level>`                          | `message`                    | One log record, other than the access line of a successful seam poll (a 200 for `GET /p/<profile>/v1/runs/<id>` from its urllib client). `native_event_type` is the logger |

`tool_call_id` is the provider's on `proposed`. When a provider reuses an ID, as Kimi does across turns, Hermes renames the
repeat to `<id>_d<n>` after the attempt ends, so every later record has the new one. Join those through `api_request_id`.

A request's `user_text` and plugin context parts are context records for the current user message of the first attempt
that sends it.

## What the seam, the coordinator and short commands record

`research_core/agent_telemetry/observed.py` holds one writer per process: `research-serve` (the seam and its coordinator loops),
the gateway's, and `research-command` for every other `research` command, including a workspace tool's subprocess.
All of this is metadata. Text reaches Datadog only through the gateway
writer.

| `action` / `stage`                        | Written by             | Says                                                                                                   |
| ----------------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------ |
| `admission` / `accepted`, `refused`       | `research serve`       | One POST to a route in `RECORDED_ROUTES` (`serve.py`). Reads such as `GET /snapshot` have none. `native_event_type` names the route. `details.status` is the status. On a refusal, `session_id` is what the client sent, unchecked |
| `admission` / `bound`                     | gateway                | An admission's native row: `admission_id`, `run_id` from Hermes, `session_id`, `hermes_message_id`     |
| `store_event` / `committed`               | whichever process commits | One committed `events` row. `native_event_type` is its kind and `details.store_seq` its `seq`       |
| `workspace_tool` / `completed`            | the process running the tool | One `run_tool` completion, with `run_tool`'s outcome names, `reason` its span's `error.type` and `turn_id` |
| `attachment` / `prepared`, `failed`       | `research serve`       | One file's type (`native_event_type`), original `size_bytes` and change (`outcome`), or why it failed  |
| `coordinator` / `report`                  | `research serve`       | One workflow pass as numbers. Sent only when it differs from the last                                  |
| `coordinator` / `failed`, `runtime_recovered` | `research serve`   | A pass raised (`reason` is the exception's type), or a new runtime retired an old one's executions    |
| `wake` / `started`                        | `research serve`       | A wake or batch Session's admission, with `batch_size`, `first_seq` and `last_seq` of its events       |
| `execution` / `successor`                 | `research serve`       | `admission_id` handed over to `execution_attempt_id`. `reason` is `new_session`, `rerouted` or `delivery_retry` |

The prompts the coordinator writes have no record of their own: the model request holds that text, after redaction.

A store event sends its type, `seq`, actor, a subject that is a display ID (`task_id`, `goal_id`, else
`details.record_number`), and what `_ALLOWED` in `observed.py` lists for its kind. An event whose kind starts with `client`,
`execution`, `transition` or `session` adds the row behind it, read just after the commit: for an admission `session_id`,
`profile`, `initiator` (its origin), `owner_instance` and `details.resumes`; for a durable command `tool_name`, `session_id`,
`lease_epoch`, `owner_instance`, `execution_attempt_id` and `details.attempts`; for a Session `kind`, `session_scope`,
`coworld_id`, `league_id`, `task_id`, `goal_id`, `parent_session_id` and `initiator` (the event that caused it). A rollback
records nothing.

### How to join them

- **A request to its admission:** `admission_id` is the `op_id` of the store row. For a request from the AIDE backend
  (`image/aide.py`) it is the JSON-RPC request ID, and a retry keeps it.
- **A retry:** another `accepted` record with the same `admission_id`, and no second `client.accepted`. Count acceptance from
  that event.
- **An admission to its runs:** the `store_event` `client.started`, by `admission_id`. There can be several: a held chat has
  `client.waiting` naming the run that failed (`run_id`, `reason`, `details.resume_after_s`), then `client.resumed`, then
  `client.started` with the new run, all under one admission. `details.steered` means the input joined a run already going.
- **A run to its gateway records:** `run_id`, the `request_id` of the Session's started admission in Hermes's `state.db`
  (`plugin/runs.py`), or its parent Session's for delegated work. A gateway record can come before the seam's
  `client.started`. Order across processes only by these links, never by `sequence`, which compares only within one
  `source_instance`. Never take "the Session's latest run": one Session can hold several runs.
- **Delegated work:** a subagent's records hold the `run_id` of the run that delegated it. `subagent` / `started` names the
  child Session and its `parent_session_id`.
- **A worker's successor:** `execution` / `successor`, from `admission_id` to `execution_attempt_id`. A new Session also has
  `parent_session_id` on its `session.started`.
- **A tool call to what it did:** `tool_call_id`, on `workspace_tool` / `completed` and on the store events of its process or
  its gateway thread. A durable command's later records have no `tool_call_id`, so follow `command_id` from
  `transition.requested`. `task.assigned` holds the first attempt's admission as both `assignment_id` and `admission_id`.
- **Counting tool calls:** count `tool` / `completed` and `refused`. `workspace_tool` / `completed` is the same call seen from
  inside a distribution tool, with a finer outcome, so it joins by `tool_call_id` and is never added. A subprocess that times
  out or is killed writes none.
