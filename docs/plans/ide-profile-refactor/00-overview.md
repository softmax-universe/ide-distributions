# IDE Profile Refactor — implementation plan (overview)

> **Implemented.** Status as of November 12, 2026: this plan was built and merged as PR #106 on
> October 29, 2026 (Pacific). The task checkboxes in these files were not kept up to date, so an
> unticked box does not mean unbuilt work. The plan is historical. Since the merge the watcher
> moved into `research serve` (#146), `play/` was removed (#199), and the image runs Hermes
> v0.22.6 with no patches (#249, #252). For what landed and what is open, read
> [STATUS.md](STATUS.md); for how the code works today, read `README.md`, `core/README.md`,
> `docs/hosting.md` and `image/README.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (or
> superpowers:executing-plans) to implement this plan task by task. Each stack entry has its own
> file (`01-…` to `07-…`). Read this overview first, then `08-amendments.md` (binding changes from
> the plan review), then only the entry you are implementing. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build a fresh pair of Hermes profile distributions, `orchestrator/` and `researcher/`,
that implement the IDE Profile Refactor design end to end, with one shared Python package, a
SQLite store, a model-free watcher, platform tools on the official Softmax client, a minimal
client seam, generated SOULs and rewritten skills.

**Architecture:** One package `research_core` (repo folder `core/`) holds all shared code: the
store library, the record and platform tools, the Hermes plugin wiring, the watcher, the
platform client (real and fake), the `research` CLI, the prompt parts and the skills. Each
profile folder is thin: a manifest, a config, a generated `SOUL.md`, a plugin shim, scripts and
(Orchestrator only) a cron job. The plugin runs inside Hermes's interpreter and imports the
pure-Python base of `research_core` from the release checkout; everything that needs the Softmax
client or numeric libraries runs in one shared *workspace environment* (`uv`, Python 3.12) that
the `research` CLI and the watcher use.

**Tech stack:** Python 3.12 (`>=3.12` for the base package), stdlib `sqlite3` (WAL, FTS5),
`uv` workspace with one lock, `coworld` and `softmax-cli` (official clients, `httpx`), `PyYAML`
and `numpy` in the workspace environment only, `pytest` + `ruff`, Hermes Agent v0.21.6.

**Spec:** `docs/designs/ide-profile-refactor-2026-10-28-final.html` (open in a browser). A plain
text extraction used while planning sits at
`/private/tmp/claude-501/-Users-jamesboggs-coding-ide-distributions/8f3b8816-1589-4741-bbc5-853490136463/scratchpad/design.txt`
(session-scoped; regenerate from the HTML if absent). Section references below (`§8`, `B.13`,
`C.2`, `H`) are to the spec.

**Reference facts gathered for this plan** (session-scoped scratchpad files; implementers in this
session should read the one for their entry, later readers must re-derive from the sources named
inside them):

| File | Subject |
|---|---|
| `…/scratchpad/reads/3.md` | Hermes v0.21.6 plugin API: `register_tool`, hooks, prompt sections, deferral, hardline patterns |
| `…/scratchpad/reads/6.md` | Hermes cron `no_agent` jobs, the `/v1/runs` API, config keys, session ids |
| `…/scratchpad/reads/4.md` | Hermes profiles, `distribution.yaml`, skills loading, `state.db`, SOUL and prompt build, one-shot CLI |
| `…/scratchpad/reads/1.md` | Official Softmax clients (`softmax_client`, `softmax`, `coworld`): function → route reference |
| `…/scratchpad/reads/0.md` | Old fake platform mechanics and the stats modules (decision rule, noise, power) |
| `…/scratchpad/reads/2.md` | Old reuse candidates: source fetch, integrate, prepare, GitHub and attachment tools, git-object readers |
| `…/scratchpad/reads/7.md` | Hosted runtime facts (metta): interpreter, image layout, `.env` contents, autosleep, gateway |
| `…/scratchpad/reads/5.md` | `play/` coupling, the draft's record tables, the 20 old skills, reference repos (optimizer-seed template, playerADE, Pudge Wars Basic) |

The scratchpad root is `/private/tmp/claude-501/-Users-jamesboggs-coding-ide-distributions/8f3b8816-1589-4741-bbc5-853490136463/scratchpad`.

---

## Principles (the north star; never trade these away)

1. The user's explicit instructions come first. No tool, setting or gate ever refuses a request
   the platform allows. Settings bound only the agents' own initiative.
2. The agents are coworkers: they repair what breaks and keep going; they report progress and
   problems without being asked.
3. Gates are few. Approvals off, no deny list, an advisory hook that never blocks. Protection is
   teaching and visibility (notices, the events log, detection of unmarked store writes).

Corollaries the code must honour: tools return the updated record; every write is one SQLite
transaction with an `events` row; wakes never write into a user conversation (`ui.notify` is the
only background channel); each event kind has exactly one handler; external actions record an
intent first and reconcile before retry; leases carry epochs.

---

## Locked decisions (made while planning; do not re-open without a reason)

### Repository layout (fresh)

```
core/                              the shared package (src layout) and its tests
  pyproject.toml                   name "research-core"; extra "platform" = coworld, softmax-cli, PyYAML, numpy
  src/research_core/
    __init__.py                    __version__
    layout.py                      Workspace/Coworld directory model from IDE_WORKSPACE
    refs.py                        the one reference parser
    store/                         db.py, migrate.py, migrations/NNNN_*.sql, errors.py, actors.py,
                                   goals.py tasks.py claims.py experiments.py versions.py directives.py
                                   settings.py capabilities.py intents.py sessions.py champion.py ui_state.py
                                   pins.py events.py registry.py (field registry used by prompts + validation)
    tools/                         base.py (ToolSpec, run_tool), one module per tool, catalog.py (sets per role)
    plugin/                        register.py, briefing.py, session_context.py, advisory.py, rules.py, executor.py
    platform/                      protocol.py, real.py, fake.py, identity.py, errors.py, submission.py, xp.py,
                                   credits.py, manifest.py
    mixin.py                       MIXIN.md front matter
    stats/                         design.py decision_rule.py noise.py ingest.py power.py bootstrap.py
    watcher/                       tick.py handlers.py dispatch.py hermes_api.py waits.py recovery.py merge.py
                                   checks.py heartbeat.py context_panel.py
    coworld_setup.py               source fetch, mixin discovery/copy, lesson import, toolchains
    gitops.py locks.py             git helpers and file locks
    serve.py                       `research serve` (seam server) and the scripted client
    contract/                      context.schema.json, events.schema.json, ui-state.schema.json, CONTRACT.md
    prompts/                       SOUL parts: common/, orchestrator/, researcher/, vocabulary.md, build.py
    task_types/                    research.md improvement.md ideation.md maintenance.md
    skills/                        common/<category>/<skill>/SKILL.md, orchestrator/…, researcher/…
    prepare.py                     profile preparation (store, env, config keys, cron job, trust)
    cli.py                         the `research` console script
  tests/                           pytest; unit + scenario; `-m hermes` tests need a real Hermes
pyproject.toml                     the WORKSPACE ENVIRONMENT project: depends on research-core[platform]
uv.lock                            one lock; refreshed daily by .github/workflows/lock-refresh.yml
orchestrator/                      distribution.yaml config.yaml SOUL.md(generated) README.md .no-bundled-skills
  plugins/orchestrator/{plugin.yaml,__init__.py}   cron/jobs.json   scripts/{prepare.py,watcher.py}
researcher/                        same minus cron/ and watcher.py
play/                              untouched (its vendored ide_research_core copy stays; see 01)
tools/                             build_prompts.py check_prompts.py refresh_vocabulary.py
docs/                              designs/ (kept), reports/ (kept), hosting.md (rewritten), plans/ (this)
.github/workflows/                 ci.yml lock-refresh.yml release-hermes.yml release-play.yml
```

Deleted in entry 1: `orchestrator/*` (old), `researcher/*` (old), `shared/`, `ontology/`
(VOCABULARY.md moves to `core/src/research_core/prompts/vocabulary.md`), `tools/sync_shared.py`,
`.gitattributes` (rewritten), `docs/coworld-context.md`. `play/` is out of scope (Q11) and must
keep building; only its comments that name the deleted script change.

### Two environments, one package

- **Hermes's interpreter** (hosted: `/opt/hermes/.venv/bin/python`, Python 3.12; local: any venv
  that runs Hermes) imports `research_core` **from source**: the profile shim reads
  `<profile>/local/core-path.txt` (written by `prepare.py`, the absolute path of
  `<checkout>/core/src`) and inserts it into `sys.path`, falling back to a plain
  `import research_core` if the package is installed there. Therefore every module the plugin
  imports at load time or in-process (`layout`, `refs`, `store/*`, `tools/*` for in-process
  tools, `plugin/*`) is **stdlib only** and must import on Python 3.12 and 3.14. A test
  enforces this (`tests/test_stdlib_only.py`: import the plugin in a subprocess with
  `-S -I` style isolation and assert no third-party module is loaded).
- **The workspace environment** at `<IDE_WORKSPACE>/tools/venv` (override with
  `IDE_WORKSPACE_ENV` when hosting prebuilt it): `uv sync --frozen --no-dev --project <checkout>`
  with `UV_PROJECT_ENVIRONMENT` set, created by `prepare.py` under a file lock, Python 3.12.
  It holds `research-core[platform]`, so `coworld`, `softmax`, `research` and `numpy` live
  there. The watcher, `research` CLI, platform tools and `experiment.close` run here.
- **Executors.** Each tool declares `executor="inproc"` (store-only; runs inside Hermes) or
  `executor="workspace"` (runs `<venv>/bin/python -m research_core.cli tool <name> --args <json>`
  with `IDE_SESSION_ID`, `IDE_ROLE` and the profile `.env` values in the environment; stdout is
  the JSON result). The same handler function serves both paths, so tests call handlers directly.
- Mixin toolchains (another Python, Nim) install under `<IDE_WORKSPACE>/tools/<name>/`.

### Environment variables (new names; the old `RESEARCH_*` set is retired)

| Variable | Meaning |
|---|---|
| `IDE_WORKSPACE` | Absolute path of the sandbox's one workspace root (shared by every profile). Local default: `$HERMES_REAL_HOME/workspace`, else `~/.hermes/workspace`. |
| `IDE_WORKSPACE_ENV` | Optional: a prebuilt workspace environment; `prepare.py` then only checks its lock stamp. |
| `IDE_PLATFORM` | `real` (default) or `fake`. |
| `IDE_FAKE_FAULTS` | Fault injection for the fake platform (`name=value;…`). |
| `SOFTMAX_PLAYER_ID`, `SOFTMAX_PLAYER_CREDENTIAL` | The workspace's Player and its credential (hosting writes both today). |
| `SOFTMAX_USER_CREDENTIAL` | Optional: the user's User identity token (Orchestrator only; metta provisions it later, B.16). Absent ⇒ User-identity actions report "User identity not provisioned" and the briefing says so. |
| `COGAMES_API_URL` | Softmax API server (default `https://softmax.com/api`). |
| `API_SERVER_KEY`, `API_SERVER_HOST`, `API_SERVER_PORT` | Hermes API server (read by the watcher from the Orchestrator profile's `.env`; Hermes strips the key from cron script environments). |
| `IDE_RESEARCHER_PROFILES` | Comma list of Hermes profile names that run Researcher Sessions; default `researcher`. Hosting may set `researcher-1,researcher-2`. |
| `HERMES_HOME`, `HERMES_REAL_HOME` | The profile home and the host home (Hermes/hosting set them). |

### Workspace layout on disk (Figure 2)

```
<IDE_WORKSPACE>/
  tools/venv/                      workspace environment (or a symlink hosting provides)
  tools/<toolchain>/               mixin-declared toolchains
  .state/store.db                  workspace store: coworld registry, workspace directives, workspace settings, events
  .state/README.md                 "use the tools" note (B.10)
  <coworld_folder>/                platform Coworld id with '-' → '_' (B.11)
    code/                          git: main = policy + mixin/ ; tags v/<id>
    source/                        clone of the Coworld's public repository at the release-pinned commit
    worktrees/task-<n>/            branch task/<n>
    worktrees/orch-<n>/            Orchestrator changes
    documents/                     agents' own files
    .state/store.db                the Coworld store (all records, UI state, events, intents)
    .state/README.md
```

### Identifiers and vocabulary

- Record ids: integer primary keys shown as `G-3`, `T-42`, `V-7`, `C-120`, `X-5`, `D-2`, `I-9`
  (intents). Fragments: `X-5#methods`, `C-12#evidence-3`. Sessions are keyed by the Hermes
  session id (text). Capabilities are keyed by name.
- Actors: `session:<hermes id>` (from `IDE_SESSION_ID`, set from the tool kwarg `session_id`
  or `HERMES_SESSION_ID`), `user:<id>`, `harness`. Platform writes also record `identity:
  player|user`.
- Event kinds are dotted: `goal.created`, `task.status`, `task.completed`, `notice`,
  `ui.patch`, `intent.pending`, `intent.done`, `capability.failed`, `champion.changed`,
  `session.started`, `store.unmarked_write`, `watcher.heartbeat`.
- Use canonical Softmax vocabulary (Coworld, League, Division, Round, Episode, Policy, Player,
  Champion = the Player's representative, never the winner). Say "submission", never
  "promotion".

### Hermes facts the code relies on (verified in v0.21.6 source; see reads/3.md, 6.md, 4.md)

- `ctx.register_tool(name, toolset, schema, handler, check_fn=None, requires_env=None,
  is_async=False, description="", emoji="")`; `schema` is the OpenAI function body
  `{"name","description","parameters"}`; handler signature `handler(args: dict, session_id="",
  task_id="", **kw) -> str` returning a JSON string, never raising.
- Hooks are keyword-only callbacks: `transform_tool_result(tool_name, args, result, **kw) -> str|None`
  (replace the result text: the advisory note channel), `post_tool_call(tool_name, args, result,
  session_id, **kw)`, `pre_llm_call(..., is_first_turn, **kw) -> {"context": str}|None`.
  `pre_tool_call` can only block or modify args, so the advisory hook uses
  `transform_tool_result`.
- `ctx.register_system_prompt_section(id, callable, position="after_memory", max_chars=4000)`;
  the callable receives a mapping with `session_id, model, provider, platform, profile_name, cwd`;
  rendered once per session and again after compaction; 8,000 characters total.
- Plugin tools are deferred behind `tool_search` unless `tools.tool_search.enabled: off` — the
  profile config sets it off (protected key).
- Profile plugin dir: `<profile>/plugins/<name>/{plugin.yaml,__init__.py}`; `plugins.enabled:
  [<name>]`; `platform_toolsets.api_server: [hermes-api-server, ide-<role>]`;
  `known_plugin_toolsets.cron: [ide-<role>]` keeps the toolset off cron.
- Cron `no_agent` jobs: script must live under `<profile>/scripts/`, run by Hermes's Python
  (shebang ignored), cwd `<profile>/scripts`, timeout `cron.script_timeout_seconds` (3600),
  tick every 60 s, never overlapping itself; `API_SERVER_KEY` is stripped from the script's
  environment, so the watcher reads `<profile>/.env`. Exit non-zero only to raise a failure
  notice; print `{"wakeAgent": false}` as the last line for a silent run.
- Runs API: `POST /p/<profile>/v1/runs` body `{"input", "session_id", "instructions"?}` +
  `Idempotency-Key` header → `202 {"run_id","status"}`; `GET /v1/runs/{id}` status ∈
  `queued|running|waiting_for_approval|stopping|completed|failed|cancelled|interrupted`;
  `POST …/steer {"input"}` only while `running`; `POST …/stop` → `cancelled`; the same
  `session_id` resumes with history; the server does **not** serialise runs per session (the
  watcher must). Mint session ids as `api_<epoch>_<8hex>`.
- `hermes profile install <folder>` copies the folder minus user-owned paths, rejects symlinks,
  keeps `config.yaml` on `update` unless `--force-config`, merges `cron/jobs.json` by id with new
  jobs paused. `distribution.yaml` keys: `name, version, description, hermes_requires,
  env_requires[{name, description, required, default}]`. Nothing in the distribution is executed
  by Hermes; `prepare.py` is run by the host after install/update.
- Skills: `<root>/<category>/<skill>/SKILL.md` with front matter `name`, `description`
  (≤ 60 chars shown in the index); precedence project > local profile > create_dir >
  external_dirs; `skills.external_dirs` relative paths resolve against `HERMES_HOME`;
  `skills.trusted_project_dirs` must list the code repo root for `.agents/skills` overrides.
- SOUL is `<profile>/SOUL.md`, built into the system prompt once per session and after
  compaction. Hermes's "Finishing the job" guidance is on by default
  (`agent.task_completion_guidance`).
- Hardline shell patterns block catastrophic commands even with `approvals.mode: off`.

### Hosted facts the code relies on (metta today; see reads/7.md — do not design around them, but do not break them needlessly)

- Hermes gateway multiplexes profiles (`/p/<profile>/…`) on `127.0.0.1:8642`; the API key is the
  bridge service key in each profile `.env`.
- Each profile `.env` holds `SOFTMAX_PLAYER_ID`, `SOFTMAX_PLAYER_CREDENTIAL` (a `cga_` agent token),
  `COGAMES_API_URL`, `SOFTMAX_CONFIG_DIR`, `API_SERVER_*`, `OPENROUTER_*`. No User credential exists
  yet.
- `prepare.py` is invoked as `<hermes python> <profile>/scripts/prepare.py --source <source folder>
  --hermes <hermes exe> [--activate-supervisor]` with `HERMES_HOME=<profile>`,
  `HERMES_REAL_HOME=<host home>`, 300 s timeout. Keep this contract; `--activate-supervisor`
  is accepted and means "enable the watcher job once".
- The data volume is `/opt/data`; profiles at `/opt/data/profiles/<name>`; the sandbox runs as
  uid 10000 under gVisor with a read-only root and no Docker.
- Today's image build archives only `orchestrator/` and `researcher/` and rejects unknown roots in
  a profile folder. The new layout needs `core/`, root `pyproject.toml` and `uv.lock` in the
  archive and a shared workspace directory: this is recorded in `docs/hosting.md` as the hosting
  handoff (Appendix G).

---

## Global constraints (from the spec; every task inherits them)

- Tools per profile: Orchestrator exactly 18, Researcher exactly 14 (C.2 table); budget ≤ 25
  (a test fails otherwise).
- One record tool per kind with an `action` field and flat optional inputs; validation per
  action; on refusal report **all** problems at once; each record tool returns the updated record.
- Briefing prompt section ≤ 4,000 characters; session-context section ≤ 4,000 (Orchestrator).
- SOUL word counts: Orchestrator ≤ 2,500, Researcher ≤ 2,000; Records section ≈ 400 words.
- Claim summary ≤ 40 words, body ≤ 500 words. Ideation Task output 2–5 proposed Tasks; ≤ 1 open
  ideation Task per Goal.
- Settings defaults (H): `paused=false`, `max_researchers=2`, `approval_required.task_types=[]`,
  `submissions.mode=autonomous`, `submissions.until=null`, `submissions.max_count=null`,
  `submissions.quality_bar=true`, `submissions.min_margin_over_champion=0`,
  `platform_writes.forum=allowed`, `platform_writes.wiki=allowed`,
  `xp.ask_above_requests_per_experiment=null`, `ideation.park_after_rounds=3`,
  `recovery.max_attempts=2`, `anti_stall.hour="04:00"` (America/Los_Angeles).
- Watcher tick every minute; champion sync every 10 minutes; stuck Task = in progress, no live
  run, no wait, for ≥ 5 minutes; repair attempts before a notice = 2; away summary after ≥ 1 hour.
- Approvals: `approvals.mode: "off"`, `cron_mode/single_query_mode/unattended_mode: approve`,
  no `approvals.deny`. Memory: `memory.memory_enabled: false`, `user_profile_enabled: false`.
  Remove `agent.max_turns` and `run_budget_seconds`. `tools.tool_search.enabled: off`.
  `cron.catch_up_missed: true`. Compression for the Orchestrator: `protect_first_n: 6`,
  `protect_last_n: 30`, `threshold: 0.6`, `target_ratio: 0.3`.
- No store path appears in any prompt, skill or tool output (B.10). Tool errors always include
  full output, exit code and context (B.15).
- `version.create` refuses image policies with a clear message (Q13).
- Platform errors: retry with backoff (3 tries, 1 s, 4 s, 16 s), then the Task waits on
  `platform` with route, status and request id.

## Review focus (inputs the spec implies but no single task's happy path exercises)

1. **A user says "submit the starter as my champion" on a workspace whose Coworld is not set up
   yet.** Expected: the Orchestrator answers at once, the setup runs, and the request still ends in
   a submission; nothing refuses. Test: scenario `test_acceptance_submit_starter` (entry 6).
2. **Two concurrent `task.claim` calls on one Task, or a claim from a replaced run (old epoch).**
   Expected: exactly one wins; the stale holder's later writes are refused with the reason, and
   the merge uses the recorded commit, not the branch tip. Tests in entry 2 (`test_tasks_lease`)
   and entry 5 (`test_replaced_worker_cannot_merge`).
3. **A malformed or oversize context block from a client.** Expected: the turn still runs; the
   agent sees a one-line validation error and the valid fields; nothing crashes. Test in entry 6
   (`test_context_block_bad_input_never_crashes`).
4. **A platform call crashes after the platform accepted it (upload, submit, XP).** Expected: the
   intent row stays `pending`; the next attempt reconciles against the platform and does not send
   twice. Tests in entry 4 (`test_intents_reconcile_*`).
5. **The store's schema was altered by a direct `sqlite3` edit, or rows were written without the
   library.** Expected: the library refuses to work through a changed shape; the watcher rebuilds
   the schema keeping rows and reports it; unmarked writes raise a management wake and a notice.
   Tests in entry 2 (`test_schema_mismatch_refused`, `test_rebuild_keeps_rows`) and entry 5
   (`test_unmarked_write_detected`).

## Execution order and verification gates

Build order (differs from the spec's stack listing in one place: the platform layer comes before
the watcher because wait resolvers need it):

1. `01-core-package.md` — teardown, skeleton, workspace environment, profile shells, prepare v1, CI.
2. `02-store.md` — store library and per-table rules.
3. `03-tools-plugin.md` — record tools, plugin wiring, briefing, session context, advisory hook.
4. `04-platform.md` — platform protocol, real and fake platforms, Versions, upload, XP, submission, intents, mixin front matter, stats.
5. `05-watcher.md` — watcher/dispatcher, Hermes runs client, waits, recovery, merge, tool checks, event ownership, spike S1.
6. `06-seam.md` — context block, notices, `research serve`, scripted client, acceptance scenario.
7. `07-skills-soul-setup.md` — forum/wiki tools, skills and client guides, SOUL generation, task types, setup wake, lesson import, CI checks, lock refresh, prepare final, docs.

Gate after every entry: `uv run ruff check .` and `uv run pytest -q` green from the repo root;
one commit per task (agent-optimised message, matching `git log` style: imperative, what and
why); never push. Gate after entry 5: the dispatch spike `tests/hermes/test_spike_s1.py` run
against a local Hermes gateway when `HERMES_BIN` and an OpenRouter key are available (the token
broker scope `openrouter.inference` is auto-tier). Gate after entry 7: the acceptance scenario on
the fake platform passes; `python tools/build_prompts.py --check` and `tools/check_prompts.py`
pass; `docs/hosting.md` and `README.md` describe the built system.

Commit cadence: one commit per task, in the order of the tasks; a task that only adds tests and
code for one module is one commit. Branch: `jb/idd-profile-refactor` (already created).
