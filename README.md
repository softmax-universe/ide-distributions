# ide-distributions

Hermes Agent profile distributions for the Softmax IDE: two research profiles that share one
package.

| Folder | What it is |
|---|---|
| [`core/`](core/) | `research-core`, the shared package: the record store, the record and platform tools, the Hermes plugin wiring, the watcher, the platform client (real and fake), the `research` CLI, the prompt parts, the skills and the agent telemetry library. |
| [`orchestrator/`](orchestrator/README.md) | The research lead: the profile the user talks to. Plans Goals and Tasks, answers the Researchers, makes and submits policy Versions. |
| [`researcher/`](researcher/README.md) | A research worker. Each Session does one Task in the background and speaks only to the Orchestrator. |

The research profiles were built from the
[IDE profile refactor design](docs/designs/ide-profile-refactor-2026-10-28-final.html) (open it
in a browser; its status note lists where the code has since moved on). The implementation
plan, one file per stack entry, is in
[`docs/plans/ide-profile-refactor/`](docs/plans/ide-profile-refactor/00-overview.md); it is
historical, and its [`STATUS.md`](docs/plans/ide-profile-refactor/STATUS.md) says what landed and
what is still open.

The rebuild merged on October 29, 2026 (Pacific). What hosting needs from these profiles, and
what they guarantee, is in [`docs/hosting.md`](docs/hosting.md); the retired profiles' notes
under `docs/` carry a banner saying so.

## How the pieces fit

- Each profile folder is a thin Hermes distribution: a manifest, a config, a generated `SOUL.md`,
  a plugin shim, scripts and profile configuration. Hermes copies the folder on
  install; the shim imports `research_core` from the release checkout, whose path `prepare.py`
  records.
- Two Python environments exist: Hermes's own interpreter runs the plugin (a base of `research_core`
  that needs only the stdlib and OpenTelemetry), and one *workspace environment* under `$IDE_WORKSPACE/tools/venv` runs
  the `research` CLI, the watcher and every tool that needs the Softmax client. The repository
  root `pyproject.toml` and `uv.lock` define that environment; `prepare.py` builds it.
- Every profile of one sandbox shares one workspace (`IDE_WORKSPACE`): a workspace store, and one
  folder per Coworld with the policy repository, worktrees, documents and the Coworld's store.
- `research_core/agent_telemetry/` captures agent operations for Datadog, and
  `research telemetry rebuild --session <id>` rebuilds a Session from Datadog alone. See
  [`docs/agent-telemetry.md`](docs/agent-telemetry.md).

## Install

```bash
git clone https://github.com/softmax-universe/ide-distributions.git ~/ide-distributions
hermes profile install ~/ide-distributions/orchestrator --name orchestrator --yes
hermes profile install ~/ide-distributions/researcher --name researcher --yes
# write each profile's .env (distribution.yaml lists the variables; IDE_WORKSPACE must be the
# same path for both), then prepare each profile with Hermes's Python. The installed script
# finds its profile and reads that .env:
<hermes python> ~/.hermes/profiles/orchestrator/scripts/prepare.py \
  --source ~/ide-distributions/orchestrator --hermes $(command -v hermes)
<hermes python> ~/.hermes/profiles/researcher/scripts/prepare.py \
  --source ~/ide-distributions/researcher --hermes $(command -v hermes)
```

Run `prepare.py` again after every `hermes profile update` (which keeps `config.yaml`; prepare
re-applies the keys the machinery relies on and removes retired ones).

## Develop and test

```bash
uv sync                 # the workspace environment plus pytest and ruff (Python 3.12)
uv run ruff check .
uv run pytest -q        # unit and scenario tests; the fake platform needs no network
HERMES_BIN=/path/to/hermes uv run pytest -q -m hermes core/tests/hermes   # against a real Hermes
```

`core/tests/hermes/test_telemetry_redaction.py` also needs `HERMES_PYTHON` or `IDE_HERMES_SRC`, to
reach Hermes's secret scanner. `core/tests/hermes/test_rebuild_real.py` holds the comparison the telemetry rests on: a Session rebuilt from a fake Datadog against the bytes a
stub model received and `state.db`.

CI runs the same inside the unpatched hosted Hermes image. `core/tests/hermes/test_native_capture.py`
holds capture to Hermes's own hooks. A
daily workflow
refreshes the `coworld` and `softmax-cli` pins in `uv.lock`.

The model tool-call tests in `core/tests/model` call models through OpenRouter and spend real
money, so they run only on request and never in CI. An explicit `-m` replaces the default
`-m 'not hermes and not model'` rather than adding to it, so `uv run pytest -m "not hermes"` with
`OPENROUTER_API_KEY` exported runs these tests too and spends money.

Nothing in these tests caps spending, so set a limit on the key. The costs below are what past runs
cost on Kimi K3.1 Pro, not a forecast: a longer tool list or prompt, or a model that retries, costs
more.

**Direct calls** (`test_write_actions.py`). `IDE_HERMES_SRC` names the pinned Hermes's source
(`/opt/hermes` in the image), whose schema sanitizers shape the tool list as Hermes sends it. Narrow
a run to one model and host: the default is four models on two hosts each. One such column is 165
cases, noted at about $2 on Kimi K3.1 Pro on November 4. The captured context below costs more: a
far longer prompt, and up to 8 requests per case instead of 3.

```bash
OPENROUTER_API_KEY=... IDE_HERMES_SRC=/path/to/hermes-agent \
  IDE_TOOL_CALL_MODELS=moonshotai/kimi-k3.1-pro IDE_TOOL_CALL_HOSTS=shipped \
  uv run pytest -m model core/tests/model/test_write_actions.py
```

It sends one write action with required fields per case (claims, Goals, Tasks, experiments,
directives, settings, notices), for both roles, offering the role's whole tool list as a Hermes
session does, plus cases that start after one and after three refused calls. Each case runs 5
times by default, and the closing table gives every rate its sample size and 95% Wilson interval:
intervals that do not overlap show a real difference, and overlapping ones decide nothing either
way. It also lists calls whose reasoning names
a field the arguments omit. `core/tests/model/write_actions.py` says which write actions are
covered and how a call is judged. `IDE_TOOL_CALL_MODELS`, `IDE_TOOL_CALL_HOSTS`,
`IDE_TOOL_CALL_CASES`, `IDE_TOOL_CALL_REPEATS`, `IDE_TOOL_CALL_PARALLEL`, `IDE_TOOL_CALL_SCHEMAS`
and `IDE_TOOL_CALL_CONTEXT` narrow or change a run (see `test_write_actions.py`).

**Through a real Session** (`test_real_session.py`). A real Hermes gateway serves both profiles
with the fake platform, so the system prompt, tool list, hooks and tools are the real ones. It ends
with `test_the_agreed_rate`: at least 95% of calls valid on the first try, after one refusal and
after three. This is the check to pass before calling the tools reliable. Run it in the pinned
image, from the repository root (at the default 10 repeats, one run on November 4 cost $3.92 and
took 19 minutes):

```bash
out=$(mktemp -d) && chmod 777 "$out"  # the container's user writes here
OPENROUTER_API_KEY=... docker run --rm --user 10000:10000 -e OPENROUTER_API_KEY \
  -e IDE_TOOL_CALL_CONTEXT=/out/context.json -v "$PWD:/src:ro" -v "$out:/out" \
  --entrypoint bash <the digest in image/Dockerfile> \
  /src/core/tests/model/in_image.sh -m model core/tests/model/test_real_session.py
```

`IDE_TOOL_CALL_CONTEXT` is optional and writes the real system prompt and Hermes tool list to that
path. Give the same variable to the direct calls, pointing at `$out/context.json`, to send
them in place of the short stand-in prompt.

Docker passes on only the variables named with `-e`. An exported `IDE_TOOL_CALL_CASES` alone does
nothing and the full run goes ahead, so narrow a run with, for example,
`-e IDE_TOOL_CALL_CASES=orchestrator-task.create-recovery1 -e IDE_TOOL_CALL_REPEATS=2`.

**Refused calls inside one turn** (`test_mid_turn_recovery.py`). The runs API can only continue a
stored history with a new user message, and a model recovers far more often after one, so
`test_real_session.py` does not measure this. Here a local proxy (`forcing_proxy.py`) answers a
conversation's first requests with a broken call, so the refusals, hooks and loop guard are real
and the model's next call is judged. It runs like the command above, with this file in place of
`test_real_session.py` and `--add-host proxy.openrouter.ai:127.0.0.1` added to `docker run`: Hermes
shapes a request by its base URL, and that name makes the proxy count as OpenRouter. A full run is
81 Sessions. Forced runs recently cost $1.02 per 100 on a control and $2.00 per 100 with a changed
tool list.

The rate counts that next call alone. After a bad one, the turn runs on until the model sends a
valid call or three more calls to the tool that are not reads, the turn ends, or 600 seconds pass
(which counts the run as `ERR`). Other calls along the way have no limit of their own. The line
`invalid, valid later` counts the runs that reached a valid call. Hermes's loop guard halts a turn
at 5 failures of one tool, so after three forced refusals only one more bad call is possible. Only
this test and the ordinary creation test below run on, so in any other test's table that line reads
`-`. A turn that hangs after a bad call still counts as invalid.

Each request keeps only the latest of identical refused calls (`plugin/repeats.py`,
`without_repeated_refusals`). A batch of calls stays, and so does a result carrying more than loop
guard notes, such as a user's mid-turn message. Rates of the next call on Kimi K3.1 Pro. The first
row is this test, 40 runs per cell (November 5, 2026); the others are direct calls with the real
context, 60 per cell (November 4):

| Refused calls in the history | Sent in full | Latest of each only |
|---|---|---|
| 3 identical, real Hermes | 21/40, 52% [37-67] | 40/40, 100% [91-100] |
| 3 identical | 30/60, 50% [38-62] | 59/60, 98% [91-100] |
| 4 identical | 20/60, 33% [23-46] | 56/60, 93% [84-97] |
| 1 of one, 2 of another | 37/60, 62% [49-73] | 55/60, 92% [82-96] |
| 3 identical, other calls between | 17/60, 28% [19-41] | 59/60, 98% [91-100] |
| Refused, a reply, a new request, refused again | 25/60, 42% [30-54] | 51/60, 85% [74-92] |

The last two rows are why copies are dropped across the whole request, not only next to each other
or within a turn.

What this does to the prompt cache, measured on November 4 (Moonshot charges $2 per million input
tokens, $0.20 when cached; cost excludes the first, uncached request). Scripted sessions on real
usage, with Hermes's cache markers, about 57,000 prompt tokens and 5 to 8 runs per row:

| Session | Sent in full | Latest only |
|---|---|---|
| 3 refusals, then 5 later turns | 98.6%, $0.201 | 98.6%, $0.199 |
| Same, and the call is refused again 3 turns later | 98.6%, $0.217 | 98.2%, $0.221 |
| Refused early, refused again 35,000 tokens later | 98.4%, $0.115 | 91.2%, $0.177 |
| Refused 3 times in one turn, 6,000 tokens apart | 98.3%, $0.290 | 97.3%, $0.309 |

So repeats next to each other cost nothing, and a refusal that comes back costs one cache miss on
what was sent since the earlier copy (35,600 tokens against 500 in the third row, about $0.06).
Requests after it hit the cache again. Six real Hermes sessions per arm agree: 97.7% cached
without a refusal coming back (97.6% with the middleware unregistered), 97.8% with one (97.9%).

Each request also leaves out the `<mandatory_tool_use>` block of Hermes's Execution discipline
section (`plugin/hermes_prompt.py`), and the Orchestrator's SOUL says a stated length is approximate.
A Researcher has no chat to go quiet and its final reply is not read, so its SOUL does not. Kimi K3.1 Pro
read the block's "arithmetic" as covering word counts, so it wrote prose into a tool call to count
it, and the user saw nothing until the call was complete. Runs whose first reply put the text in a
tool call, on direct calls with the Orchestrator's prompt, 8 per cell (November 6, 2026):

| Request | As Hermes sends it | Without the block | With the SOUL sentence | With both |
|---|---|---|---|---|
| A 2000-word essay | 3/8 | 0/8 | 0/8 | 0/8 |
| A 200-word paragraph | 8/8 | 6/8 | 1/8 | 0/8 |

With both, a request for exactly 150 words with its count checked still drafted in `execute_code` in
4 of 4 runs. `What time is it?` still went to `terminal` in 4 of 4 and a question about the last
commit in 3 of 4 (the other opened a skill first). `48271 * 9173` was answered without a tool,
correctly, in 4 of 4; harder arithmetic is untested.

**Ordinary creation** (`test_ordinary_creation.py`). A forced refusal invents a call the model did
not make, so it is fault injection and not a production rate. Here a plain request asks for a Task
(`real_session.ORDINARY_CASES`, ids such as `orchestrator-task.create-ordinary-research`), nothing is
forced, and any refusal is one the model caused. It runs like `test_real_session.py`, with this file
in its place. It ends with `test_ordinary_first_try_rate`, the same 95% rule on the first call to
the tool, which here the real tool must also accept. The `invalid, valid later` line is the natural
recovery. Its sample is only the runs whose first call was bad, so a model that rarely gets it wrong
gives few runs to read. These runs have their own column, `real session, unforced`, so they change
no `test_the_agreed_rate`. Three cases at the default 10 repeats are 30 Sessions: expect about $0.035
a run ($1.05), and up to twice that with a changed tool list. Two 120-run measurements cost $4.11 and
$4.18, at 2.7 requests a run
([`docs/reports/tool-usability-2026-11-06.md`](docs/reports/tool-usability-2026-11-06.md)).

**The other two.** `test_undeclared_fields.py` checks that a model on a closed schema sends a field
only if it is declared, so a provider change is noticed. `test_tool_call_format.py` sends one
instruction per tool, on the shipped models and pins.

The default `pytest` needs no network and covers the judging rules, every `tool.action {fields}`
signature and its example values in the prompts, skills, task types and wakes against the specs
(`core/tests/test_tool_signatures.py`), and every field a validator requires against the schemas
(`core/tests/tools/test_tool_schemas.py`).

Hermes freezes a Session's tool list until a release's boot refreshes it (`docs/hosting.md`,
"Open chats after a release"). To compare a Session's
frozen list with the deployed validator, on a copy of a profile's `state.db`:

```bash
uv run python tools/schema_drift.py /path/to/state.db --role orchestrator [--session <id>]
```

`test_usability.py` measures how easy the research tools and skills are to find, read and use for
Kimi K3.1 Pro, with a session's real system prompt and tool list. It compares a run with
`core/tests/model/usability_baseline.json` to show what a tools or skills change did;
[`docs/reports/tool-usability-2026-11-06.md`](docs/reports/tool-usability-2026-11-06.md) is the
latest run and what it did and did not show: 905 runs, $37.75. Ctrl-C drops the queued requests,
and those already running finish and are billed. A change in the test's fixtures can move
these rates by itself, so judge a drop by running the same test code on both sides. A comparison
says so when the read results or a situation differ from the baseline's. `right` and
`wrong_act` are the strict measure: `wrong_act` is what the model tried, and it counts a
`task.complete` that `tasks.complete` would refuse. `compare` prints four run counts beside them:
`wrong_attempted` (tried an unwanted write, even a refused one), `wrong_applied` and
`wrong_unchecked` (a `wrong_act` whose call would apply, or that no store check here covers), and
`right_refused` (a `right` the store would refuse). The docstring of
[`usability_probe.py`](core/tests/model/usability_probe.py) defines the run fields behind them. The
offline tests of its judging and read results run in the default `pytest`:

```bash
OPENROUTER_API_KEY=... IDE_HERMES_SRC=/path/to/hermes-agent \
  uv run pytest -m model core/tests/model/test_usability.py -k measure
```

To run the profiles end to end on your Mac against a real Hermes gateway (one command, an
interactive session, or inside the hosted image), see [`docs/local-end-to-end.md`](docs/local-end-to-end.md).

## Hosting

Softmax hosting runs these distributions in its sandboxes. [`docs/hosting.md`](docs/hosting.md)
is the handoff: the image, `prepare.py`, the environment variables, the gateway, sleep and
drain, the bridge's seam, storage and releases. Pull requests target `beta`; a push to `beta` or
`main` that changes what the image holds dispatches Metta's image release for the beta or stable
channel (`.github/workflows/release-hermes.yml` lists the paths). `main` moves only through the
`Promote beta to stable` workflow; a nightly workflow runs the read-only platform check.
