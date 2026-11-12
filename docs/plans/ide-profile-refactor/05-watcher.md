# Entry 5 — the watcher and dispatcher

> **Implemented, then changed.** Status as of November 12, 2026: built as a cron job and merged
> in PR #106 on October 29, 2026 (Pacific). The checkboxes were not kept up to date; the plan is
> historical. Since #146 (November 3, 2026) there is no cron job: `research serve`'s coordinator
> loop runs the watcher's observations, and the Orchestrator starts Researcher work with
> `task.assign`. See [STATUS.md](STATUS.md), `docs/hosting.md` ("Coordinator and pause") and
> `docs/specs/workspace-orchestration.md` for the current state.
> **2026-11-12:** the `ui` tools described here were removed; `ui.tell` became `chat_update` (branch `jb/remove-ui-tools`).

Read `00-overview.md`, then the interfaces of entries 2–4. The watcher is a Hermes `no_agent`
cron job of the Orchestrator profile that runs every minute in the workspace environment. It has
no model. It resolves waits, checks runs, recovers dead Sessions, delivers messages, merges
reviewed work, computes Goal conditions, runs due tool checks, starts Researcher runs and wake
Sessions through the Hermes runs API, and writes the heartbeat, the "has work" signal and the
computed `agent-context` panel.

Spec: §8, B.4 (stuck), B.12 (review and merge), B.13 (dispatch, waits, recovery, messages,
crash tolerance), B.15 (tool checks), H (wake table, pause, tick order, sleep and drain,
validation S1). Hermes facts: `reads/6.md`.

**Interfaces produced:**

```python
# research_core/watcher/hermes_api.py
@dataclass class RunStatus: run_id: str; status: str; session_id: str | None; output: str | None; error: str | None; terminal: bool
class HermesRuns:
    def __init__(self, *, base_url: str, api_key: str, profile_prefix: bool = True, http=urllib)   # reads <orchestrator profile>/.env for API_SERVER_KEY/HOST/PORT via HermesRuns.from_profile(profile_home)
    def start(self, *, profile: str, session_id: str, text: str, instructions: str | None = None, idempotency_key: str | None = None) -> str   # POST /p/<profile>/v1/runs → run_id; 429/503 → RunsBusy (retry next tick)
    def status(self, run_id: str, *, profile: str) -> RunStatus
    def steer(self, run_id: str, text: str, *, profile: str) -> bool
    def stop(self, run_id: str, *, profile: str) -> RunStatus
    def mint_session_id(self) -> str      # api_<epoch>_<8hex>
class FakeHermesRuns(HermesRuns)          # in-memory runs for tests: scripted outcomes, records calls; can "crash" a run

# research_core/watcher/handlers.py — the event-ownership map (data)
@dataclass(frozen=True) class Handler: kind: str; handler: Callable[["TickContext", Event], None]; wake: str | None; model: bool
HANDLERS: dict[str, Handler]   # exactly one per kind; test_event_ownership asserts uniqueness and that every kind in WAKE_TABLE is covered
WAKE_TABLE = {   # event kind → wake kind (None = no model)
  "coworld.setup_needed": "setup", "task.completed": "review", "task.reviewed": None, "task.asked": "management",
  "task.proposed": "management", "task.proposed_submission": "management", "change.completed": "review",
  "merge.failed": "management", "task.wait_resolved": None, "capability.failed": None, "repair.failed_twice": None,
  "goal.needs_ideation": "management", "goal.park": "management", "session.dead": None, "task.stuck": None,
  "workspace.resumed": None, "champion.changed": "management", "store.unmarked_write": "management", "anti_stall": "anti_stall",
}

# research_core/watcher/tick.py
@dataclass class TickContext: ws: Workspace; wstore: Store; store: Store | None; platform: Platform; hermes: HermesRuns; now: datetime; env: Mapping; profile_home: Path; researcher_profiles: list[str]; actor: Actor = Actor("harness","watcher")
@dataclass class TickReport: steps: dict[str, dict]; started_runs: list[str]; wakes: list[str]; errors: list[str]; has_work: bool
def tick(ctx: TickContext) -> TickReport     # the 12 steps in order (H "Watcher tick order"); every step is isolated by try/except and reported, so one failing step never stops the others
def main(argv=None) -> int                   # `research watcher tick`: builds the context from env, runs tick, prints a JSON summary line; exit 0 always unless the store is unopenable (then 1 + the error, so Hermes raises a failure notice)
```

Wake Sessions are Orchestrator Sessions started with `hermes.start(profile="orchestrator",
session_id=<new>, text=<wake prompt>, instructions=<wake instructions>)` and registered as
`sessions.register(kind="wake", event=<kind>, …)`. The wake prompt is rendered from
`watcher/wakes/<kind>.md` templates (short; they tell the model what happened, what records to
read, which tools to use, and that it must not write into any user conversation and must use
`ui.notify` for news). An event is acknowledged in `handled_events` (A2.1) only after its wake's run ends
successfully; dispatch is idempotent on the event seq and attempt (idempotency key
`event:<store>:<seq>:<attempt>`, A5.1).

---

### Task 5.1: `HermesRuns` client and the fake

**Files:** `watcher/hermes_api.py`, `core/tests/watcher/test_hermes_api.py` (a local `http.server` stub).

- [ ] Tests: `from_profile` reads `.env` (`API_SERVER_KEY=…`, defaults host/port); `start` sends the body and `Idempotency-Key` and returns `run_id`; 429 → `RunsBusy`; `status` parses terminal states; `steer` returns False on 409; `stop` returns the status; prefix path `/p/researcher/v1/runs`.
- [ ] Commit: `Add the Hermes runs API client`.

### Task 5.2: Tick skeleton, heartbeat, pause, unmarked-write detection, event ownership

**Files:** `watcher/tick.py`, `watcher/handlers.py`, `watcher/heartbeat.py`, `watcher/wakes/*.md` (templates for each wake kind), `cli.py` (`watcher tick`), `orchestrator/scripts/watcher.py` (real launcher: reads `local/core-path.txt`? No — the launcher must find the workspace venv: it reads `IDE_WORKSPACE`/`IDE_WORKSPACE_ENV` from `<profile>/.env` (the Hermes `.env` values are already in the process env for non-secret keys) and execs `<venv>/bin/python -m research_core.cli watcher tick`; if the venv is missing it prints a JSON line `{"wakeAgent": false, "error": "workspace environment missing; run prepare.py"}` and exits 1), `core/tests/watcher/test_tick.py`, `core/tests/watcher/test_event_ownership.py`.

Steps implemented here: (1) pause check — when `settings.paused` (workspace store) only steps 2 and 12 run and user-requested work is untouched; (2) `store.unmarked_changes()` on both stores → event `store.unmarked_write` + `notices.add` (once per detection window: remember the last reported change_log id in `meta`); schema check: on `SchemaMismatch` run `rebuild_keeping_rows`, emit `store.schema_rebuilt` + a notice + a management wake; (11) dispatch pending wakes: for every unhandled event whose kind is in `WAKE_TABLE` with a wake kind, start a wake Session (one per event; repeat suppression: the same `(kind, subject)` within 6 hours reuses the open wake unless the first one finished); (12) heartbeat (`meta.heartbeat`, `meta.has_work`), and the `agent-context` panel's computed parts (Task 5.8).

- [ ] Tests: `test_event_ownership` (no kind has two handlers; every WAKE_TABLE kind has a handler; every handler kind is documented in the wake table); `test_pause_skips_background_steps`; `test_unmarked_write_detected` (raw sqlite insert → event + notice + management wake started on the fake Hermes); `test_schema_rebuild_reported`; `test_heartbeat_written`; `test_wake_dispatch_idempotent` (two ticks, one wake run; the event is acknowledged only after the fake run completes; a failed run leaves it unhandled and the next tick retries once with a new Session, then after `recovery.max_attempts` gives up with a notice).
- [ ] Commit: `Add the watcher tick skeleton, heartbeat, pause and store checks`.

### Task 5.3: Dispatch and leases: start Researcher runs, S1 spike

**Files:** `watcher/dispatch.py`, `core/tests/watcher/test_dispatch.py`, `core/tests/hermes/test_spike_s1.py`, `docs/spikes/s1-dispatch.md` (results).

- Step (10): `tasks.dispatchable(store)`; free slots = `settings.max_researchers` − active Researcher runs (`sessions` with `kind task` whose `run_id` status is not terminal; status refreshed in step 5); repair Tasks (`priority high`, type maintenance with `capability`) first; for each: choose a researcher profile round-robin over `IDE_RESEARCHER_PROFILES`, mint a session id, `sessions.register(kind task, task_id, profile)`, `hermes.start(profile, session_id, text=render("task_start", task), idempotency_key=f"task:{id}:{epoch+1}")`; the run's first tool call `task.claim` takes the lease (the prompt says so). If the run never claims within 10 minutes the watcher stops it and records `attempts += 1`.
- Drained Sessions (after pause or switch) resume first, with the same session id and a short "you were paused; continue" text.
- [ ] Tests on `FakeHermesRuns`: slot accounting, high priority first, idempotency across ticks, drained-first order, no starts while paused.
- [ ] **Spike S1** (`-m hermes`, skipped unless `HERMES_BIN` and `OPENROUTER_API_KEY` are set): install both profiles into a temp home, enable the API server with a generated key in `.env` (`API_SERVER_ENABLED=true`, `API_SERVER_KEY=<32 hex>`, `API_SERVER_PORT=<free port>`), start `hermes gateway run` with `GATEWAY_MULTIPLEX_PROFILES=true` in a subprocess, start three runs in the `researcher` profile with different session ids whose input is "Reply with the word READY and stop", steer one, stop one, kill the gateway while one runs, restart it and resume that session id, assert each session's history is isolated (`GET /api/sessions/<id>/messages`) and the other runs completed. Write the result (pass/fail and timings) into `docs/spikes/s1-dispatch.md`. If it fails on isolation, stop and report: the Kanban fallback is a design change James must decide.
- [ ] Commit: `Add Researcher dispatch and the S1 dispatch spike`.

### Task 5.4: Waits

**Files:** `watcher/waits.py`, `core/tests/watcher/test_waits.py`.

Step (4): for every in-progress Task with `waiting_on`: `xp` → every request id in `ref` terminal via `platform.xp_get` (also `experiments.update_run`); `credits` → `platform.credits().balance` ≥ the recorded need; `permit` → the recorded ask answered (via `platform.request` on the ask route; keep the route in one constant); `submission` → `platform.submissions(policy_version_id)` state reached `placed|rejected`; `platform` → the failed route answers again (`platform.request` of the recorded route; a GET only); `process` → the recorded pid is gone (`os.kill(pid, 0)`); `question` → an unread answer exists in `task_messages`; `user` → the referenced setting/approval exists; `dependency` → every dependency complete. Resolved → `tasks.resolve_wait` + event `task.wait_resolved` whose handler (step 6/10) resumes the same Session: `hermes.start(profile, session_id=<same>, text="Your wait on <kind> resolved: <detail>. Continue.")`.
- [ ] Tests per kind on the fake platform/fake runs; a resolved wait resumes with the same session id and no new Session row.
- [ ] Commit: `Add wait resolution and session resume`.

### Task 5.5: Run checks, recovery and stuck Tasks

**Files:** `watcher/recovery.py`, `core/tests/watcher/test_recovery.py`.

Step (5): for each Session with a `run_id`: `hermes.status`; terminal `completed` → `sessions.end` (for wakes: acknowledge the event); `failed|interrupted|cancelled` while the Task is in progress and not waiting → dead run: resume the same session (`attempts += 1`) up to `recovery.max_attempts`, then stop the old run (`hermes.stop`), raise the epoch by a fresh `task.claim` path: mark the Task `unclaimed` with `lease_epoch+1`, create a fresh Session whose start text carries a summary (the Task, Experiment, branch commits, the last 20 messages of the old transcript read from `<profile>/state.db`), new worktree from the last recorded commit; then a management wake if that fails too. Stuck Task (in progress, no live run, no wait, `lease_renewed` older than 5 min) takes the same path and is flagged `stuck: true` in reads. Hold leases alive: a Session's tool calls renew (`tasks.renew_lease` is called by `run_tool` for the held Task on every Task-scoped write).
- [ ] Tests: dead run resumed twice then replaced; replaced worker's `require_holder` fails; stuck detection timing with an injected clock; `test_replaced_worker_cannot_merge` (merge uses `completed_commit`, a later commit on the branch is not merged).
- [ ] Commit: `Add run recovery and stuck-Task handling`.

### Task 5.6: Messages by steer or resume; abandon stops runs

**Files:** `watcher/messages.py`, `core/tests/watcher/test_messages.py`.

Step (6): pending `task_messages` `to_researcher`: if the Session's run is `running` → `hermes.steer(run_id, text)`; else if the Session has no run and the Task is in progress → resume with the message as input; mark delivered. `task.abandoned` events: stop the Task's run (`hermes.stop`), end the Session, keep the branch.
- [ ] Tests: steer when live, resume when not, abandon stops.
- [ ] Commit: `Deliver Orchestrator messages by steer or resume`.

### Task 5.7: Review wake and mechanical merge

**Files:** `watcher/merge.py`, `watcher/wakes/review.md`, `core/tests/watcher/test_merge.py`.

- `task.completed` and `change.completed` → review wake (model). The wake reads the Task, Experiment results, new claims and the Researcher's answers and calls `task.review {accept, merge, reason}`; on reject the same Researcher Session resumes with the reason (`task.reviewed` with `accept=false` → handler resumes).
- Step (7): Tasks with `merge == "pending"`: `merge.integrate(coworld, task)`: `main` clean (`gitops.clean_check(code)`), worktree clean at the recorded `completed_commit` (if the branch tip moved past it, merge the recorded commit, not the tip), host-wide lock `<ws>/.state/merge.lock`, `git merge --no-ff <commit>` into `main` in a temporary worktree of main (never in the agents' worktrees), run the mixin `build` and `check` commands on the result, then move `main` (fast-forward the real `main` ref) and set `tasks.set_merge("merged <sha>")`, event `task.merged`; on conflict or failed checks: abort, leave `main` unchanged, `tasks.set_merge("not merged: <reason>")`, event `merge.failed` → management wake (which resolves in its own worktree, opens a maintenance Task, or asks the user via `ui.notify`/`ui.ask`). Protected paths (refused from Task branches): `mixin/MIXIN.md` front matter? **No** — the design lets agents edit the mixin; protect only `.state/` and nothing under `code/` beyond what git protects. Pause stops step 7.
- [ ] Tests on temp repos: clean merge moves main and re-runs checks (fakegame `check` passes), a failing check leaves main unchanged with the reason, a conflicting branch produces `merge.failed`, the recorded commit is merged rather than a later tip.
- [ ] Commit: `Add the review wake and the mechanical merge`.

### Task 5.8: Goal conditions, champion sync, tool checks and repair Tasks, agent-context panel

**Files:** `watcher/goals.py`, `watcher/checks.py`, `watcher/context_panel.py`, `watcher/wakes/{management,setup,anti_stall}.md`, `core/tests/watcher/test_goals.py`, `test_checks.py`, `test_context_panel.py`.

- Step (8): `goals.needs_ideation` → event `goal.needs_ideation` (management wake creates or approves an ideation Task; ≤ 1 open per Goal); `ideation.park_after_rounds` reached without progress → event `goal.park` (management wake parks the Goal with a reason and notifies).
- Step (3) every 10 minutes: `champion.sync` from `platform.memberships(mine=True, champions_only=True)` for the active Coworld's leagues (the leagues the user's Versions were submitted to, from `events version.submitted`); a change made outside the workspace → event `champion.changed` (management wake).
- Step (9) tool checks (B.15): due when never run, daily, or after a release (the `core` version in `meta.last_checked_version` differs): `checks.run_all(ctx) -> list[CheckResult]`: logins (`platform.whoami` for player and, if provisioned, user), CLIs (`coworld --version`, `softmax --version` in the workspace env; compare with PyPI once a day via `platform.request`? No network beyond Softmax: use `uv pip index`? Keep it simple: compare installed versions with the lock file and report "lock refresh pending" when the lock-refresh workflow's marker file is newer), GitHub broker reachability (`github.repositories` HEAD), each mixin command (`build`, `check`, `evaluate` on the starter; `upload` as a dry run: build the starter and call `platform.upload_file_policy` only when `IDE_PLATFORM=fake` or the capability `upload` was never verified — a real upload of the starter is acceptable per B.14 "real or dry-run"; record the policy version). Each result → `capabilities.mark`; a new failure → one `high` `maintenance` Task for that capability (`tasks.create(type=maintenance, priority=high, capability=name, title="Repair <name>")`, repeats join it), dependents get `task_deps` rows (Tasks whose type needs that capability: improvement needs build/check/evaluate/upload; research needs evaluate for replays), a second failed repair (`task.failed` on a repair Task twice) → `notices.add` (event `repair.failed_twice`). Also the session-start capability check: `research capabilities check --quick` runs logins + CLIs (no builds) and is what the briefing shows; the plugin triggers it lazily when the briefing finds no check in the last hour.
- Step (12) `context_panel.compute(ctx) -> dict` (A.13 workspace part: active Coworld, image version, acting Player, User identity available, Goals, champion, latest Versions with submission status, running Tasks/Researchers, capabilities with maintenance Tasks) written with `ui_state.patch(conversation="workspace", ops=[replace /panels/agent-context/computed])` only when changed; the "needs" section is left to the agent.
- Anti-stall: daily at `anti_stall.hour` local (America/Los_Angeles) → event `anti_stall` → wake (reads the briefing, decides whether anything should move, notifies).
- [ ] Tests: ideation event once per Goal (not every tick); park after 3 rounds; champion change detected; a failing check creates one maintenance Task and a second failure joins it; dependents gain the dependency; the panel is written only on change.
- [ ] Commit: `Add Goal conditions, champion sync, tool checks with repair Tasks, and the context panel`.

### Task 5.9: Drain, `has work`, `research workspace drain`

**Files:** `watcher/drain.py`, `cli.py` (`workspace drain`), `core/tests/watcher/test_drain.py`.

- `drain(ctx) -> dict`: stop running Researcher runs (`hermes.stop`), stop agent-started background processes recorded in `sessions` (pids from `process` waits), `sessions.mark_drained`, return when quiet (poll statuses up to 60 s). `research workspace drain` prints the result; the first tick after a wake or resume resumes drained Sessions first (Task 5.3).
- `has_work` (H "Sleep and drain"): a running Researcher run, a Task waiting on xp/credits/submission/process, an open autonomous-submission window with a candidate, or a queued wake → `meta.has_work` true.
- [ ] Tests: drain stops and marks; resume order; has_work cases.
- [ ] Commit: `Add drain and the has-work signal`.

### Task 5.10: Watcher scenario tests on the fake platform and fake Hermes

**Files:** `core/tests/scenarios/test_watcher_lifecycle.py`.

- [ ] Scenarios (H "Scenario tests"): Goal → ideation wake (fake run executes a scripted tool sequence: the `FakeHermesRuns` accepts a "script" per expected wake kind that calls tools through `run_tool` with a wake actor) → proposed Tasks → approval → dispatch → claim → Version → XP wait → tick resolves → resume → complete → review wake accepts → merge → autonomous submit allowed (quality bar met) and a notice; review reopen; merge conflict → management wake; dead-Session recovery; stuck Task; broken tool → repair Task takes the next slot → resume of dependents; steer delivery; champion changed outside; pause and resume (drained first); direct-write detection; events stream resume from a seq (`events_after`).
- [ ] Commit: `Add watcher lifecycle scenarios`.
