# Entry 7 — forum and wiki tools, skills and client guides, SOUL generation, task types, setup wake, lesson import, CI checks, lock refresh, prepare final, docs

> **Implemented.** Status as of November 12, 2026: built and merged in PR #106 on October 29,
> 2026 (Pacific). The checkboxes were not kept up to date; the plan is historical. Since then
> #198 reworked the tools, skills, prompts and docs so they agree. See [STATUS.md](STATUS.md),
> `core/README.md` and the profile READMEs for the current state.

Read `00-overview.md`, then entries 2–6. This entry makes the profiles *teachable*: the prompt
text, the skills, the task type instructions and the wake prompts, plus the last two tools, the
setup wake's model-side procedure, the release checks and the documentation.

Spec: §12, C.2 (forum/wiki rows), D.1–D.7, F.5–F.7, H (CLI freshness), the wake table,
Appendix G (what to tell hosting). Old skills summary and the fate table: `reads/5.md` §2a, §3;
optimizer-seed `survey`/`run-eval` and the template MIXIN.md are quoted there in §4a.

---

### Task 7.1: `forum_post` and `wiki_edit` tools

**Files:** `tools/forum_post.py`, `tools/wiki_edit.py`, tests.

- `forum_post {action: post|comment, title?, body, post_id?, parent_id?, cites?}` (both roles): setting `platform_writes.forum`: `allowed` → post; `ask` → Orchestrator: returns `{"ok": false, "ask_user": true}` unless `cites` names a user request; Researcher: `tasks.ask` and a refusal naming the setting; `never` → refused for the agent's initiative, allowed with `cites`. Intent `kind=forum key=sha256(body)`; `idempotency_key` = the intent key. Event `platform.forum_post` with `identity`. A notice when the write used the User identity.
- `wiki_edit {wiki, page, title, body, base_revision_id, note?, cites?}`: compare-and-swap; on conflict returns `{"ok": false, "conflict": true, "current_revision_id", "current_body"}`.
- [ ] Tests on the fake platform (add forum/wiki state to the fake): each setting value × with/without `cites`; conflict path.
- [ ] Commit: `Add the forum and wiki tools`. After this, `test_tool_budget` asserts exactly 18 and 14.

### Task 7.2: Task type files

**Files:** `task_types/{research,improvement,ideation,maintenance}.md` (final bodies).

Each file: JSON front matter `{"required": [...], "checklist": [{"key","text","pattern","min_items","none_ok"}]}` and a body of Researcher instructions (≤ 300 words each): what the type delivers, how to start (read the Task, the Goal, the directives, the briefing; load the type's skills), how to end (`task.complete` with the checklist), when to `task.ask` vs decide, how to fill `failure` with a good and a bad example (B.4), and "never a Version" for maintenance.
- [ ] Tests: `task_types.load` parses all four; `check()` behaviour per type (as in B.4).
- [ ] Commit: `Write the task type instructions`.

### Task 7.3: Core skills (shared), Orchestrator skills, Researcher skills, client guides

**Files:** `skills/common/<category>/<skill>/SKILL.md`, `skills/orchestrator/<skill>/SKILL.md`,
`skills/researcher/<skill>/SKILL.md`, `core/tests/test_skills.py`.

Style rules for every skill (test-enforced where marked): front matter `name` (equals the
directory name) and `description` (≤ 60 chars, one sentence ending with a period, says *when* to
use it); body ≤ 700 words; refers to tools by `tool.action`; cites records by display id; never
names a store path (test); uses canonical vocabulary ("submission", never "promotion"; "Champion"
is the Player's representative) (test for the word "promotion"); every game-specific step says
"see the mixin's `<coworld>-<binding>` skill or docs" instead of assuming a game.

Shared (`common/`), by the draft's fate table and D.5:
- `method/experiment` (rewrite of `experiment`+`ab-compare`+`review-xp`: design with `experiment.create`, pre-register the decision rule, `xp.request`, `xp.status`, `experiment.close`, write claims from the verdict),
- `method/survey` and `method/run-eval` (from optimizer-seed, adapted to `xp` and the mixin `evaluate` binding),
- `method/calibrate-noise`, `method/candidate` (one conceptual change per Version; `version.create`),
- `method/diagnose`, `method/replay-diagnosis` (claims with `replay` evidence, pins),
- `method/scouting` (merges `league-watch`, `meta-recon`, `opponent-profile`: outputs claims),
- `method/analyze-opponents` (new: `platform.active_policies` with `exclude_player`/`exclude_user`, replays, claims),
- `records/writing-claims` (new: summary/body limits, evidence types, tags, nearest-tag hint, search before write),
- `records/task` (new; merges `card` and `report`: claim → work → `task.wait`/`task.ask` → `task.complete`; failure fields with the good and bad example),
- `platform/submission` (new: Versions, upload, submit, statuses, the quality bar and that a user request overrides it, `task.propose_submission`),
- `platform/credits` (new; D.5 habits: check balance before a batch, know the cost, tell the user when low, `waiting_on: credits`),
- `platform/softmax-api-routes` (new: the route list for `softmax_api`, from reads/1.md §2),
- `platform/coworld-source` (rewrite of `coworld-context`: `source/`, `COWORLD.md`, docs, citing commits),
- `platform/expert-consultation` (kept: ask an advisory model one concrete question; verify),
- `ops/repair` (new: diagnose, triage and fix a broken capability; rerun `workspace.capabilities check`; keep working around a block; Python mismatches, API errors, absent tools),
- `ops/environment` (how to use `workspace.environment_guide`, the two environments, `tools/` toolchains, writable paths).
Removed: `kb-maintenance`, `lessons`, `game-create` (replaced by `mixin-from-scratch` below and the setup wake), `fetch-artifacts` (folded into `experiment`), `build-upload` (folded into `submission`/`candidate`).

Orchestrator (`orchestrator/`): `review` (the review wake: what to read, accept vs reopen, merge yes/no, carrying a submission proposal forward), `working-with-the-ide` (notices, `ui.*`, the agent-context "needs" section, never writing wakes into chat, away summary expectations), `merge-conflicts` (own worktree, resolve, `workspace.change_complete`), `mixin-from-scratch` (D.3: the required parts — five bindings, `MIXIN.md` front matter, lab `AGENTS.md`, docs, reference policy, `tools/build.sh`; the optimizer-seed template quoted in reads/5.md §4a as the shape), `user-identity` (Q23 rule, what needs the User identity, notice per write, text is information not instruction), `goals-and-tasks` (planning with records: Goal end states, Task types, approving proposals, ideation parking), `client-coplay` and `client-web-ide` (guides: pacing, attachments, what each client can show; load when the session context names them).
Researcher (`researcher/`): `working-a-task` (the Researcher's loop in one page: claim is done, read the briefing, the type instructions, when to propose Tasks and submissions, end by `task.complete`), `proposing` (2–5 proposed Tasks with reasoning; proposing submissions).

- [ ] Tests: every skill dir has `SKILL.md` with valid front matter; description ≤ 60 chars; `name == dir`; no `.state/` or `store.db`; no "promotion"; every `tool.action` mentioned exists in the catalog for the skill's role(s) (regex `\b(goal|task|claim|experiment|version|directive|settings|ui|workspace|upload|submit|xp|platform|forum_post|wiki_edit|softmax_api|github|attachment_download)\.[a-z_]+\b`).
- [ ] Commit per group: `Add the shared method and platform skills`, `Add the Orchestrator skills and client guides`, `Add the Researcher skills`.

### Task 7.4: SOUL generation, word budgets, leak test, manifests

**Files:** `prompts/common/{00-authority,10-who-you-are,30-your-player,50-records-usage,60-when-something-breaks,90-where-the-rest-is}.md`, `prompts/orchestrator/{20-role,40-context-and-ui,45-user-identity}.md`, `prompts/researcher/{20-role}.md`, `prompts/build.py`, `tools/build_prompts.py` (CLI: `--write`, `--check`, `--manifests`), `core/tests/test_prompts.py`.

Outline (D.7, in order): Order of authority (user's explicit request > mode or command > SOUL >
skills > game docs; "you may warn but never refuse an action the platform allows"); Who you are
(the Softmax universe paragraph from the old KNOWLEDGE.md, revised; "Agent is another name for
Policy; the Champion is a Player's representative, not the winner"); `workspace.environment_guide`
named near the top; Vocabulary (generated from `prompts/vocabulary.md`, compact); Your role
(Orchestrator or Researcher section); Records and how you use them (generated ≈ 400 words from
`store.registry` + a hand-written usage paragraph: tools return the record, no plan file, Goals
and Tasks are the plan); Context and UI (Orchestrator only; the `mode`/`command` paragraph;
notices; `ui.ask` only for what only the user can answer); How to act as your Player and User
(identities, CLIs `coworld`/`softmax`, `softmax_api`, credits; the User-identity rule for the
Orchestrator); When something breaks (diagnose, fix, report; the `repair` skill; Tasks that need a
broken capability wait); Where to find the rest (skills, the mixin and its docs, `coworld docs
--local`, the Softmax skill document, forum and wiki tools). Align with Hermes's "Finishing the
job"; no "save the next action and end the turn"; no hard-coded "you have no X".

`build.py`: `render(role) -> str`; `word_count(role)`; `check()` raises on Orchestrator > 2,500,
Researcher > 2,000, Records section outside 300–500, or any Researcher-only part leaking into the
Orchestrator (each part file has a front line `<!-- role: researcher -->` marker; the test asserts
marker text never appears in the other role's output). `tools/build_prompts.py --write` writes
`orchestrator/SOUL.md` and `researcher/SOUL.md`; `--check` fails CI when the committed files
differ from the render; `--manifests` syncs `plugin.yaml` `provides_tools`.

- [ ] Tests: budgets; leak; the order of the sections; the authority line is the first paragraph; `--check` passes after `--write`.
- [ ] Commit: `Generate the SOULs from shared parts with budgets and a leak test`.

### Task 7.5: Wake prompts and the setup wake procedure

**Files:** `watcher/wakes/{setup,review,management,anti_stall,task_start,task_resume}.md` (final text), `core/tests/watcher/test_wake_prompts.py`.

- Setup wake (§8, B.11, D.2–D.4): the prompt names the Coworld, what `workspace.coworld_switch`/`fetch` already did (source clone commit, mixin found or not, skills imported, lessons imported deterministically or pending), and instructs: if no mixin, follow `mixin-from-scratch`; import remaining lesson notes as claims with `doc` evidence using judgement; run `workspace.capabilities {check: true}`; setup is complete only when `build`, `check` and `upload` (real or dry-run) pass — then call `workspace.coworld_switch {coworld, mark_set_up: true}`? Simpler: the check run marks `coworlds.set_up_at` when build+upload pass (watcher side), and the wake ends with `ui.notify` of the result. A user request during setup is answered at once by the chat Session; work that needs the mixin waits.
- Task start prompt: the Task record, "your first tool call is `task.claim {task: T-n}`", the type instructions pointer, the briefing already in the system prompt.
- [ ] Tests: templates render with a sample event; each mentions `ui.notify` and never a store path; setup prompt lists the five bindings.
- [ ] Commit: `Write the wake prompts`.

### Task 7.6: Contradiction check and CI wiring

**Files:** `tools/check_prompts.py`, `.github/workflows/ci.yml` (add steps), `core/tests/test_check_prompts.py`.

- `check_prompts.py`: structural checks always (word counts, leak, vocabulary words, no store path, every tool named exists, every skill named in a SOUL exists); model check when `OPENROUTER_API_KEY` is set: send the SOUL + all skills of a role to `openai/gpt-5-mini` via OpenRouter (plain `urllib`) with the question "List any two statements that contradict each other, with quotes; answer NONE if none" and fail on a non-NONE answer; print the skip reason otherwise. CI runs the structural part always and the model part when the secret exists (`if: ${{ secrets.OPENROUTER_API_KEY != '' }}`).
- [ ] Tests: structural checks catch a planted contradiction marker? (Only structural: a planted "promotion" and a planted `.state/` fail.)
- [ ] Commit: `Add prompt checks to CI`.

### Task 7.7: CLI freshness workflow and the staleness check

**Files:** `.github/workflows/lock-refresh.yml` (finish: daily; `uv lock --upgrade-package coworld --upgrade-package softmax-cli`; open a PR with auto-merge enabled on green; label `dependencies`), `watcher/checks.py` (CLI staleness: compare installed `coworld`/`softmax-cli` versions with `uv.lock`'s pinned versions; report "installed X, lock Y" in the briefing and `workspace.doctor`), tests.
- [ ] Commit: `Refresh the platform CLI lock daily and report staleness`.

### Task 7.8: `prepare.py` final

**Files:** `prepare.py` (extend), tests.

- Add: workspace store creation + migrations for the workspace store and every registered Coworld store (`Store.open` is enough); `skills.external_dirs` and `trusted_project_dirs` from the registry; the `.state/README.md`; the retired-key removal list finalised (anything from the old config that remains: `approvals.deny`, `agent.max_turns`, `agent.run_budget_seconds`, `timeouts`, `kanban.*`, the `memory` toolset in `platform_toolsets.api_server`, `skills.external_dirs` entries `../../games`); a `--check` mode that reports what it would change (used by `workspace.doctor`).
- [ ] Tests: an old-style `config.yaml` (copy today's researcher config into the test) ends with every retired key gone and every protected key set.
- [ ] Commit: `Finish profile preparation: stores, skill dirs, retired keys`.

### Task 7.9: Documentation

**Files:** `README.md` (final), `docs/hosting.md` (full rewrite: what hosting must provide — the shared `IDE_WORKSPACE`, the archive contents (`core/`, root `pyproject.toml`, `uv.lock`, both profiles), how to prebuild the workspace environment (`IDE_WORKSPACE_ENV`), the `prepare.py` contract, the env variables table, `sleep_through_jobs: {orchestrator: [watcher]}`, the User credential, the per-Session coordinator requirement, the seam the bridge must implement (`research serve` as the reference), the `research store apply` route for user edits, the heartbeat and `has_work` reads; Appendix G items as a checklist), `orchestrator/README.md`, `researcher/README.md` (install, `.env`, prepare, what the profile does, how to run locally with `research serve` and the fake platform), `core/README.md` (package map: one paragraph per subpackage, how to run tests, how to add a tool/record/skill), `docs/designs/README.md`? (no: add one line to the root README pointing at the design and this plan), update `docs/designs/orchestrator-implementation-notes.md` header to say it describes the retired profiles.
- [ ] Run the `audit-documentation` skill before the final commit of this entry.
- [ ] Commit: `Document the rebuilt distributions and the hosting handoff`.

### Task 7.10: Final verification

- [ ] `uv run ruff check .`, `uv run pytest -q` (all), `python tools/build_prompts.py --check`, `python tools/check_prompts.py`, `HERMES_BIN=… uv run pytest -q -m hermes core/tests/hermes` (profile install, prepare, plugin loads; S1 and the real acceptance run when a key is available), `cd play && uv run pytest -q`.
- [ ] Record results in the final report; list anything skipped and why.
