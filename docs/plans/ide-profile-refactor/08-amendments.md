# Amendments from the plan review (binding)

> **Implemented.** Status as of November 12, 2026: these amendments bound the implementers of
> the plan, and the build that applied them merged as PR #106 on October 29, 2026 (Pacific). They
> are historical and do not bind later changes. See [STATUS.md](STATUS.md) for the current state.
> **2026-11-12:** the `ui` tools described here were removed; `ui.tell` became `chat_update` (branch `jb/remove-ui-tools`).

Three independent reviewers checked entries 00–07 against the design. The findings below are
adopted; where this file and an entry file differ, **this file wins**. Each item names the entry
and task it changes. Implementers read this file with their entry.

## Store (entry 2)

- A2.1 **`handled_events` table** in both stores: `handled_events(seq INTEGER PRIMARY KEY, kind,
  run_id, session_id, attempts INTEGER NOT NULL DEFAULT 0, handled_at, last_error)`. `events`
  stays append-only; acknowledging a wake writes here, never into the event payload.
  `events.unhandled(store, kinds)`, `events.claim_handling(store, actor, seq, kind, session_id)
  -> attempts`, `events.mark_handled(store, actor, seq, run_id)`, `events.mark_failed(store,
  actor, seq, error)`. (Replaces "`events` payload `handled_by_run`" in 05.)
- A2.2 **`tasks.goal_id` is nullable** with `CHECK(goal_id IS NOT NULL OR type = 'maintenance')`;
  `tasks.create(..., goal_id=None, capability=None)` accepts both. Goal-less maintenance Tasks are
  "workspace maintenance" in `goals.read`, the briefing and the panel.
- A2.3 **Leases carry the epoch on the session row**: `task_sessions(task_id, session_id,
  epoch, kind 'task'|'change', started)`; `claim` writes the epoch. `require_holder(store, task_id,
  actor)` = `tasks.held_by == actor.id AND task_sessions.epoch == tasks.lease_epoch` for that
  session. `Actor.epoch` is only a test override. Task-scoped writes that call `require_holder`:
  `renew_lease`, `wait`, `ask`, `release`, `complete`, `propose_submission`, `set_last_error`,
  `experiments.add_run/update_run/close` when the Experiment has a Task, `versions.create` with a
  `task_id`, `task.message` from a Researcher. Test each from a stale epoch in `test_tasks_lease`.
- A2.4 **Where rows live**: `sessions` and `notices` exist only in the workspace store
  (`sessions.coworld_id` nullable; `task_id` without a foreign key). `settings` exists in both
  stores and each key has a scope in `SettingSpec`: workspace = `paused`, `max_researchers`,
  `recovery.max_attempts`, `anti_stall.hour`; coworld = `submissions.*`, `platform_writes.*`,
  `xp.*`, `ideation.*`, `approval_required.task_types`. `settings.get(ctx_or_stores, key)` picks the
  store from the spec. Wake dispatch and unmarked-write detection iterate `[wstore, store]`;
  `handled_events` is per store.
- A2.5 **`store/coworlds.py`** (new, entry 2): `register(wstore, actor, coworld_id, *, folder) ->
  dict`, `activate(wstore, actor, coworld_id) -> dict` (flips the single `active` row; event
  `coworld.switched`), `active(wstore) -> dict | None`, `list(wstore)`, `mark_set_up(wstore,
  actor, coworld_id, *, mixin_version, tool_check_status)`, `set_pending_request(wstore, actor,
  coworld_id, request: dict | None)`. The row gains `pending_request TEXT` (A6.3).
- A2.6 **FTS** also for experiments (`experiments_fts` over summary, hypothesis, methods,
  results, tags).
- A2.7 **Task outcomes**: `tasks.complete(..., outcome="succeeded"|"failed", failure=None)`:
  `failed` requires `failure` and skips the checklist, emits `task.failed`; `succeeded` keeps the
  checklist path. `tasks.fail` is the function `complete` delegates to; the `task` tool has no
  separate `fail` action. `failure` requires `what_failed` and `next_step`; `error`, `exit_code`
  and `tried` are optional (a user's "drop T-5" needs no exit code).
- A2.8 **Approval and the user's word**: `tasks.approve(store, actor, task_id, *, cites=None)`: a
  session actor with `cites` passes even when `approval_required.task_types` names the type;
  `approved_by` records `user:<id> via session:<id>` and the citation; without `cites` the refusal
  says a user request overrides it. `tasks.create(status="unclaimed")` sets `approved_by = actor`
  unless the setting names the type and the actor is a session without `cites`, in which case the
  Task is stored `proposed` and the result says why.
- A2.9 **Event kinds are exhaustive**: every store function names the event kind it emits
  (`task.created`, `task.proposed`, `task.approved`, `task.claimed`, `task.released`,
  `task.waiting`, `task.wait_resolved`, `task.asked`, `task.answered`, `task.message`,
  `task.completed`, `task.failed`, `task.reopened`, `task.reviewed`, `task.abandoned`,
  `task.merged`, `task.proposed_submission`, `goal.created`, `goal.updated`, `goal.status`,
  `claim.created`, `claim.updated`, `claim.retired`, `experiment.created`, `experiment.started`,
  `experiment.run`, `experiment.closed`, `experiment.abandoned`, `version.created`,
  `version.uploaded`, `version.submitted`, `champion.changed`, `directive.added`,
  `directive.updated`, `directive.retired`, `settings.updated`, `capability.failed`,
  `capability.repaired`, `intent.pending`, `intent.done`, `intent.failed`, `session.started`,
  `session.ended`, `notice`, `ui.patch`, `coworld.registered`, `coworld.switched`,
  `coworld.setup_needed`, `coworld.set_up`, `store.unmarked_write`, `store.schema_rebuilt`,
  `platform.request`, `platform.forum_post`, `platform.wiki_edit`, `turn.rendered`,
  `watcher.heartbeat`, `merge.failed`, `goal.needs_ideation`, `goal.park`, `session.dead`,
  `task.stuck`, `workspace.resumed`, `repair.failed_twice`, `anti_stall`). `store/events.py`
  exports `KINDS` (the set) and a test asserts every kind any store function emits is in it.
- A2.10 **`store apply` allow-list** adds `tasks.reject` (a `proposed` Task → `abandoned` with
  `failure {what_failed: "rejected by the user", next_step}`), `workspace.coworld_switch`
  (delegates to the workspace environment: `research coworld switch <id> --cites <ref>`), and
  defines `workspace.pause`/`resume` as `settings.update("paused", true|false, cites=<ui action>)`.
- A2.11 **Directive ids name their store** (added 2026-11-04; replaces `D-<n>` for all directives
  in design B.14): workspace directives are `W-<n>` and Goal directives stay `D-<n>`. Each store
  numbers its own `directives` table, so one prefix let `D-1` mean two directives. The directive
  tool and `store apply` require the prefixed form and refuse a bare number. Stored rows did not
  change, so a workspace directive once called `D-1` is now `W-1`, and older events and chat text
  that say `D-1` about it are out of date.

## Tools and plugin (entry 3)

- A3.1 **Executor table** (asserted by a test in `catalog.py`): `inproc` = `goal`, `task`,
  `directive`, `claim`, `settings`, `ui`; `workspace` = `experiment`, `version`, `workspace`,
  `upload`, `submit`, `xp`, `platform`, `softmax_api`, `github`, `attachment_download`,
  `forum_post`, `wiki_edit`. The stdlib guard test imports the whole catalog
  (`research_core.tools`) and every `research_core.plugin.*` module, calls `make_register` for
  both roles with a fake ctx, and asserts no `yaml|numpy|coworld|softmax_client|httpx|pydantic`
  in `sys.modules`.
- A3.2 **Timeouts and background work**: `ToolSpec.timeout_s` (default 400 for the workspace
  executor, bounded by Hermes's 420 s). Mixin commands run through
  `mixin.run_command(..., budget_s=240)`: the command starts detached (`Popen`,
  `start_new_session`, stdout+stderr to `<coworld>/documents/logs/<name>-<ts>.log`); if it ends
  within the budget the tool finishes synchronously; otherwise the tool returns `{"ok": true,
  "background": {"pid", "log", "resume": <token>}}` and the agent either waits
  (`task.wait {kind: process, ref: pid}` and end its run) or calls the same tool again with
  `resume: <token>` when the process is done. `version.create`, `xp.status` (evaluate) and the
  build in `upload` use this. `workspace.capabilities {check: true}` never runs checks in the
  tool: it marks them due and the watcher's step 9 runs them next tick. Full output always goes
  to the log file; results carry the log path plus 2,000-char tails.
- A3.3 **Prompt sections** register with `max_chars=3800`; the briefing renders to ≤ 3,600 on the
  Orchestrator (two sections share Hermes's 8,000-character aggregate, which also counts
  headings and markers) and ≤ 3,800 on the Researcher; tests assert those numbers.
- A3.4 **Briefing never runs checks**: it reads the last stored capability check and prints its
  age. Freshness is the watcher's (step 9 runs `--quick` when the last check is older than an
  hour or a `session.started` event arrived since).
- A3.5 **`ui` conversation key**: a chat Session writes UI state under its own session id; a wake
  Session writes under the fixed conversation `workspace` (notices, the agent-context needs
  section, `ui.ask` for the user), which every client sees.
- A3.6 **`workspace.change_complete {worktree, commit, summary}`** creates a `maintenance` Task
  with `goal_id NULL`, `worktree`, `branch`, `completed_commit`, status `complete`, held by the
  Orchestrator session (`task_sessions.kind = 'change'`), and emits `task.completed`, so the review
  wake and the mechanical merge reuse the Task path. `change.completed` is dropped from the wake
  table.
- A3.7 **Run ids**: `task.claim` takes `run_id` from the Session row
  (`sessions.read(IDE_SESSION_ID).run_id`, written by the watcher after `hermes.start`); never
  from the environment (Hermes exposes no run id).
- A3.8 **`task.approve`** has a `cites` parameter (A2.8). **`task.complete`** has `outcome` and
  `failure` (A2.7).
- A3.9 The platform tool wrapper (entry 4) sets `waiting_on: platform` for a task Session when a
  `PlatformError` escapes `with_retries`: `tasks.wait(kind="platform", detail=f"{route} {status}",
  ref=request_id)`, and the result carries route, status and request id.
- A3.10 **Preferences are directives** (added 2026-11-04; replaces "user-preference claims" in
  design §9 and entry 3): a user's preference becomes a directive, on the workspace unless it
  concerns one Goal, or a setting when a setting key covers it. Claims live in one Coworld's
  store and need evidence, so a preference claim did not follow the user between Coworlds.
  `settings.update` no longer suggests writing a preference claim. The briefing still lists
  claims tagged `preference` that already exist, below Goals, saying directives and settings override them,
  and omits the item when there are none; it can be removed once no workspace has any. Directives rank above Goals in the briefing,
  so many long preferences would push Goal information out.

## Platform (entry 4)

- A4.1 **`capabilities.report_failure(store, actor, name, *, output, command)`** marks the
  capability broken, creates or joins the `high` `maintenance` Task, adds `task_deps` for Tasks
  whose type needs that capability, emits `capability.failed`, and after the second failed repair
  Task emits `repair.failed_twice` plus a notice. Called from every mixin command failure
  (`run_command`), from the GitHub broker, the attachment route and platform login failures, and
  from the watcher's checks. Test: a failing `build` inside `version.create` leaves `build` broken
  and exactly one repair Task; a second failure joins it.
- A4.2 **Intents everywhere the design says**: `upload`, `submit`, `xp`, `forum`, `wiki` (key
  `sha256(wiki|page|base_revision_id|body)`, reconciled through `wiki_history`), `run` (A5.2) and
  `champion`.
- A4.3 **Mixin Coworld id must match**: `fetch` refuses a mixin whose front matter `coworld`
  differs from the registry id (`{"mixin": None, "problem": "MIXIN.md declares X, workspace is
  Y"}`), `set_up_at` stays unset. Test with a fixture naming another id.
- A4.4 **XP**: `xp.request` never sets `waiting_on`; it records runs and returns request ids; the
  Researcher calls `task.wait {kind: xp, ref: [...]}` and ends its run (taught in the `task`
  skill). No local credits pre-check: the platform's 402 (`CreditsRefused`) is the refusal; the
  tool then reports balance, held and next refill and, for a Researcher, says to `task.wait
  {kind: credits}`. `xp.ask_above_requests_per_experiment` is a warning for the Orchestrator and a
  `task.ask` suggestion for a Researcher; it never refuses. The create body sets `title =
  f"ide:X-{n}:{seed}:{arm}"`; `XpRequest` has `title` for display only. The local intent saves a UUID idempotency key
  and the exact create body before sending. Recovery replays that body through the platform
  create endpoint; it never searches request titles. `Credits` gains `recent_requests: list[dict]` (what the platform returns) and
  `credits.estimate` falls back to the mixin front matter `cost_per_episode` when there is no
  history.
- A4.5 **`submit`**: `cites` is a record, never a gate: a `user_requested` submit without
  `cites` still runs and the result asks for the reference to be recorded.
- A4.6 **`platform.policy_file`** tries the Player identity first and the User identity only when
  the Player is refused (so Researchers can read their own Player's policies).
- A4.7 **`softmax_api`** writes a notice for every non-GET call made with the User identity
  ("Acted as your User identity: <METHOD> <path>") in the same transaction as the event.
- A4.8 **`workspace.coworld_switch {coworld, cites?, source?, pending_request?}`**: the source
  URL and commit come from the platform manifest; `source` is an override for tests and private
  repositories. `cites` records the user's message. `pending_request {text, cites}` is stored on
  the registry row and copied into the `coworld.setup_needed` payload (A6.3).
- A4.9 **Champion sync** reads `memberships(mine=True, champions_only=True)` for every league of
  the active Coworld's game, not only leagues the workspace submitted to.
- A4.10 The stats tests to port live in git history: `git show f4e18cb:researcher/tests/unit/stats/`.
- A4.11 The fakegame fixture repository includes an optimizer-seed-format `seed/` folder (claims,
  lessons, `evidence/source-citations.json`) so the deterministic lesson import has a test case;
  the shape is in `git show f4e18cb:researcher/games/pudge-wars-basic/seed/`.

## Watcher (entry 5)

- A5.1 **Idempotency keys carry the attempt**: `task:<id>:<epoch>:<attempt>` and
  `event:<store>:<seq>:<attempt>` (attempt from `handled_events.attempts` or
  `sessions.attempts`). `FakeHermesRuns` answers a reused key with 409 so tests see it.
- A5.2 **Run intents**: before every `hermes.start`, `intents.begin(kind="run",
  key=f"{session_id}:{attempt}")` in its own transaction; after `sessions.set_run` commits,
  `intents.finish(done, {run_id})`. A `pending` run intent at the next tick is reconciled against
  Hermes (`GET /v1/runs/{run_id}` when the id is known, else the session's runs through
  `GET /api/sessions/<id>`) before any new start. Test with a crash injected between start and
  commit.
- A5.3 **One resume function**: `resume_session(ctx, session, text)` reads `hermes.status` first:
  `running|queued` → `steer` (defer to the next tick if refused), otherwise `start` with the same
  session id. Steps 4, 6, 5.3 and recovery all use it; step 4 only emits `task.wait_resolved` and
  its handler runs in step 6 after step 5 refreshed statuses.
- A5.4 **HANDLERS is exhaustive**: it includes `task.abandoned` (stop the run), `task.failed`
  (repair counter), `store.schema_rebuilt` (management wake), and steps 5 and 9 and drain emit
  `session.dead`, `task.stuck`, `workspace.resumed` before handling them. `UNWATCHED_KINDS` lists
  every emitted kind the watcher ignores; `test_event_ownership` asserts
  `KINDS == set(HANDLERS) | UNWATCHED_KINDS` and no duplicates.
- A5.5 **No 10-minute claim kill**: a run that ends without claiming counts one attempt; after
  `recovery.max_attempts` a management wake. The stuck rule applies only with no live run.
- A5.6 **Watcher-initiated stops are not deaths**: `sessions.stopped_by TEXT` (`watcher:drain`,
  `watcher:replace`, `watcher:abandon`) is set before `hermes.stop`; recovery ignores runs whose
  session carries it.
- A5.7 **Pause drains**: step 1, when `paused` is true and Researcher runs are live, runs
  `drain` once (recorded in `meta.paused_drained`) and never starts new work.
- A5.8 **Per-profile API keys**: `HermesRuns` holds a key per profile read from
  `<profiles root>/<profile>/.env`, falling back to the Orchestrator's; `doctor` reports differing
  keys; S1 and 6.4 write one key into every installed profile's `.env`; `docs/hosting.md` records
  the requirement.
- A5.9 **Skill dirs and cwd for every profile**: `apply_skill_dirs` iterates
  `[profile_home] + [profiles_root / p for p in IDE_RESEARCHER_PROFILES]` and sets, in each,
  `skills.external_dirs` (core common, core role, `<active>/code/mixin/skills`),
  `skills.trusted_project_dirs` (`<active>/code`) and `terminal.cwd` (`<active>/code`). The
  task-start prompt tells a Researcher Session to work in its worktree. A hermes-marked test
  checks an override under `code/.agents/skills/<name>/` appears in `skills_list` for both
  profiles.
- A5.10 Scenario additions to 5.10: `test_concurrent_chat_and_two_wakes` (three Sessions, each
  event acknowledged once, the chat run not blocked), `test_submissions_mode_ask` (autonomous
  candidate → Orchestrator asks via `ui.ask` → UI answer → submit), `test_orchestrator_change_is_
  reviewed_and_merged` and its conflict path.
- A5.11 *Superseded:* `has_work` no longer counts an autonomous-submission candidate. The
  `submission.candidate` event queues the management wake that acts on it, which counts as
  `wake:queued` while pending; a candidate with no such wake (a baseline arm, a Version below the
  submission bar) has nothing to run and would keep the workspace awake forever.

## Seam (entry 6)

- A6.1 **`research serve` serialises per session**: `/message` on a session with a live run
  steers it; otherwise it starts one. The same rule is the reference for the bridge.
- A6.2 The real-Hermes acceptance run (6.4) also runs the concurrency case.
- A6.3 **Setup carries the pending request**: on a fresh workspace the chat Session answers at
  once ("setting up <Coworld>; your request runs when it is ready"), calls `coworld_switch` with
  `pending_request`, and the setup wake ends by carrying it out (`version.create` on `main` →
  `upload` → `submit user_requested=true` citing the message) and notifying. The scripted agent
  drives the acceptance scenario through this path and never polls.
- A6.4 **First Coworld of a new workspace**: optional `IDE_DEFAULT_COWORLD` (hosting knows the
  application's Coworld); `prepare.py` registers and activates it when the registry is empty and
  emits `coworld.setup_needed`, so setup runs before the first chat. Without it, the first user
  request or client context selects the Coworld.

## Skills, prompts, prepare, docs (entry 7)

- A7.1 **Nightly read-only platform check**: `research platform check --read-only` calls every
  read route the tools use (one route table shared with the `softmax-api-routes` skill) and
  `.github/workflows/platform-check.yml` runs it daily with a test-Player credential from a
  repository secret, opening or updating an issue on failure; the step is skipped when the secret
  is absent.
- A7.2 **Tool-call format tests** (model-gated, skipped without `OPENROUTER_API_KEY`):
  `core/tests/model/test_tool_call_format.py` asks each configured model to call each tool once
  from a one-line instruction and asserts `validate_args` accepts the call; pass rates per model
  are printed.
- A7.3 **Stale skill overrides**: when an override exists under `code/.agents/skills/<skill>/`,
  the setup/switch path writes `.upstream-sha` (sha256 of the core skill); `doctor` and the
  briefing list overrides whose upstream changed.
- A7.4 **Retained-value rule**: `prepare.py` replaces a non-protected retained value when it is
  unset or equals a known earlier shipped default (`EARLIER_DEFAULTS`), so shipped changes reach
  updated profiles while owner choices survive.
- A7.5 The old doctrine and skills are in git history (`git show f4e18cb:researcher/doctrine/`,
  `git show f4e18cb:shared/skills/autoresearch/`) for the skill rewrites.
- A7.6 The compaction summariser has no instruction key in Hermes v0.21.6; the SOUL carries one
  line: "If this conversation is summarised, keep the user's instructions word for word."
- A7.7 **Hosting handoff additions** (`docs/hosting.md`): the checkout that `core-path.txt` and
  the editable install point at must be read-only to the sandbox user (Q24; `doctor` reports
  "core path writable: yes/no"); one `API_SERVER_KEY` across profiles; checks S3 (the bridge reads
  the store with WAL under gVisor) and S4 (writable profile venv); the backup volume holds
  `IDE_WORKSPACE`; `sleep_through_jobs: {orchestrator: [watcher]}`; `IDE_DEFAULT_COWORLD`.
- A7.8 CLI staleness reports "installed X, lock Y, lock refreshed <date of the last lock-refresh
  commit>" without calling PyPI.
- A7.9 `session_search` is its own Hermes toolset; confirm it is in the `hermes-api-server`
  composite (reads/3.md §2) and, if not, list it in `platform_toolsets.api_server`.
