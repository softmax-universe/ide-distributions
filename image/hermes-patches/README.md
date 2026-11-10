# Hermes patches

Our own small changes to Hermes for telemetry hooks. They apply to the Hermes in the image that `image/Dockerfile` pins (v0.22.6, upstream commit
`f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab`). Each one is to be proposed upstream and deleted once Hermes has it; a new one needs the `hermes-patch-approved` label (`.github/workflows/hermes-patch-guard.yml`). A Hermes update means
reapplying them, rebuilding the image and rerunning `core/tests/hermes/test_hermes_patches.py`.

`image/Dockerfile` applies them with `git apply`, in file-name order. The `core` job in
`.github/workflows/ci.yml` applies them the same way to the same pinned image. `tools/local_stack.py`
uses the unpatched image.

| Patch | What it is for |
|---|---|
| `0001-prompt-sources.patch` | Say which source (SOUL, project rules, memory, skills and so on) produced each stretch of the system prompt, at every build and rebuild. |
| `0002-model-attempts.patch` | Report each real call to the provider: retries inside a stream, the partial text and tool arguments of a failed stream, and whole auxiliary requests and replies. |
| `0003-tool-results-and-commits.patch` | Report a tool result in the final form the model receives, and every row at the moment it becomes durable, with its database ID. |
| `0004-run-binding.patch` | Tell a callback which run it belongs to, so that it never has to look up the session's latest run. |
| `0005-gateway-exit-flush.patch` | Give plugins one call before the gateway's `os._exit`, which skips every `atexit` step. |

`core/src/research_core/plugin/capture.py` is what uses the hooks.

## How the hooks behave

Register with `ctx.register_hook(name, callback)`. A plugin that registers none of them sees no
change: Hermes checks `has_hook` before building a payload. A callback that names only some fields
receives only those. A callback with `**kwargs` receives them all, including the new fields on
existing hooks.

All the new hooks run on Hermes's bounded hook path (`plugins.hook_callback_timeout`, 30 seconds by
default). A callback that raises is logged and skipped, and one that hangs is abandoned. The caller
waits for the callback, so keep it short: copy what you need and return. Return values are ignored.

Values are live objects that Hermes keeps using. Copy them, and never change them. The content is
not capped and has no secrets removed.

Every hook also receives `telemetry_schema_version` and `run_id`.

## When each hook fires

`plugin/capture.py` is the only consumer and shows each hook's arguments.

- **`on_system_prompt_built`:** each `build_system_prompt`, a Session's first and every rebuild.
  `sections` gives `{"source", "start", "end"}` in characters, and only `"\n\n"` separates neighbours.
  It does not fire when a continuing Session reuses its stored prompt, when a detached compaction
  agent keeps its seeded prompt, or when a fallback rewrites the `Model:` and `Provider:` lines in place.
  `ephemeral_system_prompt` joins the system message at request time and is never part of this prompt.
- **`on_model_attempt`:** when one physical call ends, on the thread that made it, before Hermes
  decides whether to retry. A stream retried twice fires three times. `stream_parts` is the only place
  the partial text of a failed stream survives, and holds tool arguments before any repair. Streaming
  Bedrock does not fire it, and `stream_parts` is `None` on the Anthropic stream. Hosted profiles use
  neither, and no test has run Codex or non-streaming Bedrock.
- **`pre_auxiliary_call`, `post_auxiliary_call`:** existing hooks, with new `request_body` and
  `raw_response` fields that are not capped. `raw_response` is `None` on an error and on a streamed
  auxiliary call, whose reply Hermes hands back unread.
- **`on_tool_results_final`:** once per batch of tool calls, after transforms, spill, the turn budget
  and any `/steer` text. Error rows for invalid tool names are outside the batch.
- **`on_messages_committed`:** after each database transaction that writes a turn's new rows, with the
  Session's persistence lock held. It does not fire for the turn-budget rewrite of a row already written.
- **`run_id`:** added to every hook call by the dispatcher, or `None` outside `/v1/runs`. It is bound
  where execution starts, worker threads inherit it, and a run recovered after a restart keeps it.
  Middleware reads `hermes_cli.middleware.current_run_id` instead.
- **`on_gateway_exit`:** once per profile on the graceful exit path, just before `os._exit`, without
  its profile's scope. Its bound is longer than the ten seconds `image/processes.py` gives the gateway,
  so a callback needs its own shorter limit. It does not fire on the shutdown watchdog's exit, a
  downgraded-checkout exit or SIGKILL.

## Changing a patch

Each patch is a plain `git diff` against the previous one, so a change to an earlier patch can
require regenerating the later ones. Extract `/opt/hermes` from the pinned image, commit it to a
scratch repository, apply the patches as one commit each, edit, and regenerate with
`git diff HEAD~1 HEAD`. The image holds no Hermes tests. They are in the public repository at the
commit above.
