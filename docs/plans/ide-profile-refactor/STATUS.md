# Status of the IDE profile rebuild

Status as of November 12, 2026 (Pacific), checked against `main` at `ec3e811d`. First written
October 28, 2026 on branch `jb/idd-profile-refactor`, which merged as PR #106 on October 29,
2026 (Pacific). The plan is `00-overview.md` … `07-skills-soul-setup.md` with the binding
amendments in `08-amendments.md`; those files are historical. This file says what landed, where
the code has moved on since, and what is still open. For how the code works today, read
`README.md`, `core/README.md`, `docs/hosting.md`, `image/README.md` and
`docs/specs/workspace-orchestration.md`.

## Landed

Every stack entry of the plan is implemented and committed:

1. **Core package and profile shells** (`core/`, root `pyproject.toml` + `uv.lock`,
   `orchestrator/`, `researcher/`; the old profiles, `shared/`, `ontology/` and
   `tools/sync_shared.py` removed; `play/` untouched here, then removed in #199).
2. **Store**: SQLite per Coworld plus a workspace store, migrations, token-marked transactions,
   the events table, every record module, `research store apply|export|snapshot|events`.
3. **Tools**: 18 Orchestrator and 14 Researcher tools with actions, validated at once, never
   raising (now 24 and 23: #188 gave the 13 hardest write actions their own tools, such as
   `claim_write`; `core/src/research_core/tools/base.py` `split_from`); the plugin (tools,
   hooks, prompt sections, the turn hook with notices, changed-since, away summary and
   reactions); the two executors (in-process and workspace).
4. **Platform**: one protocol, the real client on `coworld` and `softmax-cli`, the fake with
   faults, identity, waits, submission ladder and quality bar, XP, credits, `research softmax`,
   `research platform check --read-only`.
5. **Watcher**: the twelve-step model-free tick, the event-ownership map, wakes (setup, review,
   management, anti-stall) with their prompts, dispatch, recovery, waits, messages, resume, the
   mechanical merge (now emitting `submission.candidate` for merged candidates), Goal
   conditions, champion sync, tool checks, the context panel, drain and `has_work`. This shipped
   as a `no_agent` cron job of the Orchestrator profile. Since #146 (November 3, 2026) there is no
   cron job: `research serve`'s coordinator loop runs the watcher's observations, the
   Orchestrator starts Researcher work with `task.assign`, `orchestrator/cron/jobs.json` is
   empty, and `prepare.py` removes the old job (`retire_watcher_job`). See `docs/hosting.md`
   "Coordinator and pause" and `docs/specs/workspace-orchestration.md`.
6. **Seam**: contract files and validator, `research serve`, the scripted client, the
   acceptance scenario on the fake platform (`core/tests/scenarios/test_seam_end_to_end.py`).
7. **Skills, SOULs, task types, preparation**: 18 common, 8 Orchestrator and 2 Researcher
   skills (17 common today); generated SOULs (2,462 / 1,927 words) and manifests; the four
   task types; `prepare.py` with protected and retired keys, the active Coworld's directories
   for every profile, shipped values over earlier defaults, stores, the watcher job (now
   retired, see 5), the workspace environment, `IDE_DEFAULT_COWORLD` and `--check`.
8. **Host autosleep probe**: `orchestrator/scripts/activity.py` printed `{"waiting_on": [...]}`
   from the watcher's recorded reasons, the contract main's #105 introduced for the old
   profiles. #198 deleted it. Automatic sleep now belongs to the image: `image/control.py`
   reads `has_work` and `waiting_on` from `research serve`'s `/status` (`docs/hosting.md`,
   "Gateway, sleep and drain").
9. **The host's sleep cycle** (October 30, 2026): `research workspace sleep` pauses, drains,
   lets the Orchestrator's runs finish (stopping them after `--wait`), then checkpoints and
   fsyncs every database and syncs the volume, exiting 0 only when nothing runs and nothing is
   volatile; `research workspace wake` clears sleep's own pause so the next tick resumes the
   drained Sessions. A wake run stopped for sleep retries without using an attempt. Only running
   work keeps the workspace awake now: a Task waiting on the platform no longer does.
10. **Profile models** (October 30, 2026): the model is the profile's own setting. The host
   no longer passes `OPENROUTER_MODEL` (it was never read) and `prepare.py` never touches
   `model` or `fallback_providers` (except one migration per profile off a retired default,
   `migrate_superseded_model`). Both profiles now ship with `moonshotai/kimi-k3.1-pro`, fallback
   `z-ai/glm-5.3` (`orchestrator/config.yaml`, `researcher/config.yaml`); titles and image
   reading run on `anthropic/claude-haiku-5.5` (`core/src/research_core/prepare.py`, the
   `auxiliary.*` keys); #237. At first the Orchestrator shipped with `moonshotai/kimi-k3` and a
   Researcher with `recursive/flint-v1.0:free`. The bridge's `/models` routes (contract
   "Profile models") show and change each profile's model; `research serve` implements them.
11. **Checks and docs**: `tools/check_prompts.py` (structural, plus a model check with a key),
   `tools/build_prompts.py --check`, the nightly platform-check workflow, `docs/hosting.md`,
   `core/README.md`, the profile READMEs, banners on the retired docs.

## Verified

These are the checks run on October 28, 2026, before the merge. The `play` row and Hermes
v0.21.6 are history: `play/` was removed in #199, and the image now runs Hermes v0.22.6
(`docs/hosting.md`) with no patches (#249, #252). CI (`.github/workflows/ci.yml`) is today's
check.

| Check | Result |
|---|---|
| `uv run ruff check .` and `ruff format --check .` | clean |
| `uv run pytest -q` | 1,447 passed, 2 skipped (model-gated), 6 deselected (hermes) |
| `python tools/build_prompts.py --check`, `python tools/check_prompts.py` | pass |
| `cd play && uv run pytest -q` | 81 passed |
| `HERMES_BIN=… uv run pytest -m hermes core/tests/hermes/test_prepare_real.py` (real Hermes v0.21.6) | 3 passed (install and prepare) |
| S1 spike and the real-Hermes acceptance run (model key from the token broker) | both passed; see "Real model runs" below |

## Real model runs

Both ran on the evening of October 28, 2026 (Pacific) against Hermes v0.21.6 in an isolated
environment, with `moonshotai/kimi-k3` through an OpenRouter key from the token broker.

- **Acceptance scenario on a real gateway** (`core/tests/hermes/test_seam_real_hermes.py`):
  **passed**. "Submit the starter as my champion" on a new workspace: the Orchestrator's chat
  run switched to `cow_fake` with the request pending, the setup wake built V-1, uploaded it
  and submitted it, and the fake platform held the submission as the profile's Player after
  7 watcher ticks (38 s). The first attempt had surfaced three defects, all fixed before the
  passing run: the plugin read `os.environ`, which a multiplexing gateway keeps free of the
  profile's `.env` (now `executor.profile_env` merges Hermes's secret scope); Hermes's
  post-turn self-improvement review patched four shipped skills (now a protected key, off);
  and the model called `ui.notify` without `text` repeatedly (validation errors now list the
  fields received, and the descriptions name the field).
- **S1 dispatch spike** (`core/tests/hermes/test_spike_s1.py`, report in
  `docs/spikes/s1-dispatch.md`): **passed**. Three Researcher Sessions in one profile started,
  one steered (accepted; kept pending when the one-step reply finished first), one stopped
  (cancelled), the gateway killed and restarted in 2.7 s, the third run's status durable
  through the key, its Session resumed and completed, each Session's history holding only its
  own turns, and the idempotency key replaying the run id for the same body and answering
  409 for another. In the passing run the model had answered before the kill; the 21:16 run
  exercised the kill under a live run (it read `interrupted` after the restart and the Session
  resumed and completed). Two harness defects were fixed on the way: a kill interrupts every
  live run, so run 1 is awaited first; and the history route answers in `data`, which the
  runs client now reads.

## Review decisions (October 29, 2026, Pacific)

Rohit's review of PR #106 (a data-loss review of the store, upload recovery, and three
acceptance cases from the superseded #91) was taken decision by decision with James:

1. **Store repair stays automatic, done in place.** The design's rebuild of a mismatched store
   keeps, but as one exclusive transaction inside the same file (no file swap), with a copy of
   the original saved first. A store from a newer release is refused, never rebuilt, and the
   workspace is paused until the matching runtime runs. Snapshots and exports read inside one
   read transaction. Every writable connection sets `synchronous=FULL`.
2. **Loss window: one hour.** The handoff requires hourly volume snapshots (the daily history
   kept); `research store backup` and `research store restore-check` are provided for
   database-aware copies hosting may run more often. Replaces the design's accepted daily bound
   (R6).
3. **Lost uploads.** Exact upload inputs are saved with the intent and recovery uses Coworld
   0.1.67's read-only lookup as the Player. The design's rule stands: one automatic retry only
   when that lookup answers definitively "no such version"; any error or refusal leaves the
   Version unresolved with a notice; an explicit user request always uploads again. (Rohit
   asked that even a definitive miss stay unresolved; James kept the design's rule.)
4. **Image policies: follow-up PR before metta's pin moves.** The design's deferral stands;
   `docs/designs/image-versions-2026-10-29.md` is the proposal.
5. **Build inputs git does not track.** `version.create` refuses ignored inputs and nested
   repositories (caches, the artifact output and submodules excepted) so the commit stays the
   single source of truth.
6. **Source editing after a pod replacement.** Proven now as a scenario test (worktree removed,
   workspace moved, Version resolved from the platform policy version id, source read from the
   tagged commit, an edit built as a new Version with the original unchanged); the seam's
   read/edit routes with stale-edit rejection belong to the contract-layer stack the design
   planned.

## Open items and known seams

Re-checked against the code on November 12, 2026.

- **Open.** `claim` exposes no `update` action although the store has one; the skills say a
  claim's text, tags and links do not change once written. Decide whether to expose it. The
  tool's actions are `search`, `read`, `write`, `add_evidence`, `retire` and
  `inspect_evidence` (`core/src/research_core/tools/claim.py`); `update` is in
  `core/src/research_core/store/claims.py`.
- **Open.** `task.wait` has no kind that resolves on the watcher's rerun of the checks
  (`WAIT_KINDS`, `core/src/research_core/store/registry.py`); the repair skill tells the agent
  to rerun the fixed command itself and read `workspace.capabilities`
  (`core/src/research_core/skills/common/ops/repair/SKILL.md`).
- **Changed, still open.** The checks no longer dry-run the upload: they really upload the
  starter artifact as `<coworld>-tool-check-<player>` when the platform is the fake or the
  route was never verified. That upload still writes no `intents` row (the upload tool does);
  see `_upload_check` in `core/src/research_core/watcher/checks.py`.
- **Open.** `experiments.methods.candidate` cannot be set after an Experiment starts (only a
  `proposed` Experiment can be updated); the verdict falls back to the one Version built for it
  (`update` and `candidate_of` in `core/src/research_core/store/experiments.py`).
- **Open.** `require_holder` gives the watcher no sanctioned way to write Task-scoped Experiment
  runs; the `xp` wait skips them and the Researcher records the results itself (module
  docstring of `core/src/research_core/watcher/waits.py`). Whether Tasks are still released as
  the old holder's Session actor was not re-checked.
- **Partly done.** The seam's contract layer: `session-open` with `capabilities` is built
  (`core/src/research_core/contract/CONTRACT.md`). `session-update`, the panel kinds'
  rendering rules and multi-client replay are still deferred (CONTRACT.md, "Deferred to the
  contract layer"). Clients reach the seam only through the AIDE 1.0.0 backend since #236.
- **Obsolete.** The plan's `tools/e2e.py` demo was not built. `tools/local_stack.py` now runs
  both profiles and `research serve` in the hosted image with a scenario
  (`docs/local-end-to-end.md`), and the scenario tests under `core/tests/scenarios/` cover the
  same path.
- **Partly obsolete.** Hosting changes for metta listed in `docs/hosting.md`: `sleep_through_jobs`
  no longer applies, since there is no cron job and automatic sleep belongs to the image
  (`image/control.py`). Per-Session turn serialisation, the read-only core checkout and one
  `API_SERVER_KEY` across profiles are still stated as requirements there; whether metta meets
  them was not checked here.
