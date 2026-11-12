# Entry 4 — platform protocol, real and fake platforms, Versions, upload, XP, submission, intents, mixin front matter, stats, Coworld switch

> **Implemented.** Status as of November 12, 2026: built and merged in PR #106 on October 29,
> 2026 (Pacific). The checkboxes were not kept up to date; the plan is historical. See
> [STATUS.md](STATUS.md) and `core/README.md` for the current state.

Read `00-overview.md`, then `02-store.md` and `03-tools-plugin.md` interfaces. This entry gives
the agents everything that touches the Softmax platform, for file policies only (Q13). All code
here runs in the workspace environment (executor `workspace`), except the fake platform's state
layout, which tests also drive directly.

Spec: §11, B.6, B.8, B.11 (switch), B.13 (intents), B.16 (identities), C.2 (platform tool
contents), C.3 (`research softmax`), C.4 (client, errors, retries), D.1 (MIXIN.md), H (settings,
submission paths). Client facts: `reads/1.md`. Old fake mechanics worth keeping: `reads/0.md` §1.

**Interfaces produced:**

```python
# research_core/platform/protocol.py
class Identity(TypedDict): kind: Literal["player","user","anonymous"]; subject_id: str | None; email: str | None; expires_at: str | None
@dataclass class PolicyVersion: id: str; name: str; version: int; label: str; content_hash: str | None; player_id: str | None; created_at: str | None
@dataclass class League: id: str; name: str; game_id: str; coworld_id: str | None; participation_url: str | None; forum_markdown_url: str | None; wiki_markdown_url: str | None
@dataclass class Membership: id: str; league_id: str; division_id: str | None; policy_version: PolicyVersion; player_id: str | None; status: str; substatus: str | None; is_champion: bool
@dataclass class Submission: id: str; league_id: str; policy_version_id: str; status: str; membership_id: str | None; created_at: str
@dataclass class XpRequest: id: str; status: str; episode_count: int; completed_count: int; failed_count: int; coworld_id: str; created_at: str; error: str | None
@dataclass class Credits: balance: float; max: float | None; refill_amount: float | None; next_refill_at: str | None; spent_this_period: float | None
class PlatformError(Exception): route: str; status: int | None; request_id: str | None; detail: str; retryable: bool
class CreditsRefused(PlatformError): balance: float; held: float; next_refill_at: str | None
class Platform(Protocol):
    def whoami(self, identity: str = "player") -> Identity
    def credits(self) -> Credits
    def upload_file_policy(self, *, name: str, content: bytes, tags: dict[str, str]) -> PolicyVersion     # exact bytes; returns the server's content_hash (hex sha256)
    def policy_versions(self, *, name: str | None = None, player_id: str | None = None, mine: bool = True, identity="player") -> list[PolicyVersion]
    def policy_file(self, policy_version_id: str, identity="user") -> bytes | None
    def leagues(self, *, game_id: str | None = None) -> list[League]; def league(self, league_id) -> League
    def divisions(self, league_id) -> list[dict]; def standings(self, division_id) -> list[dict]
    def memberships(self, *, league_id=None, player_id=None, policy_version_id=None, active_only=False, mine=False, champions_only=False) -> list[Membership]
    def submissions(self, *, league_id=None, player_id=None, policy_version_id=None, mine=False) -> list[Submission]
    def submit(self, *, league_id, policy_version_id, auto_champion="always", player_id=None) -> Submission
    def set_champion(self, membership_id: str) -> dict                 # POST /v2/league-policy-memberships/{id}/champion via request()
    def xp_create(self, body: dict) -> XpRequest; def xp_get(self, xp_id) -> XpRequest; def xp_list(self, *, mine=True, limit=50) -> list[XpRequest]
    def xp_episodes(self, xp_id) -> list[dict]; def xp_download(self, xp_id, *, include: list[str], dest: Path) -> list[Path]   # results, replay, logs…
    def episodes_for_policy(self, policy_version_id) -> list[dict]; def episode(self, episode_request_id) -> dict
    def episode_artifact(self, episode_request_id, artifact_type: str) -> bytes
    def coworld_manifest(self, coworld_id) -> dict                       # {id, name, version, manifest{game{player_runtime, runnable{source_url,...}}}}
    def forum_list(self, slug, *, sort="hot", limit=20) -> list[dict]; def forum_read(self, post_id) -> str; def forum_search(self, query, *, slug=None) -> list[dict]
    def forum_post(self, *, title, body, idempotency_key) -> dict; def forum_comment(self, post_id, *, body, idempotency_key, parent_id=None) -> dict; def forum_vote(self, post_id, value, comment_id=None) -> dict
    def wiki_read(self, wiki, page) -> dict; def wiki_search(self, wiki, query) -> list[dict]; def wiki_history(self, page_id) -> list[dict]
    def wiki_edit(self, wiki, page, *, title, body, base_revision_id, idempotency_key, note=None) -> dict   # {"conflict": True, current_revision_id, current_body} on 409
    def request(self, method, path, *, params=None, json=None, identity="player") -> dict     # the softmax_api escape hatch; path relative to /observatory
def from_env(env, ws: Workspace) -> Platform      # IDE_PLATFORM=fake → FakePlatform(ws.state/"fake_platform.json", env) else RealPlatform(env)
def with_retries(fn, *, tries=3, delays=(1, 4, 16), sleep=time.sleep)   # retries PlatformError.retryable (5xx, 429, connection errors)
```

---

### Task 4.1: `RealPlatform` on the official clients, identities, errors

**Files:** `platform/protocol.py`, `platform/identity.py`, `platform/errors.py`, `platform/real.py`,
`core/tests/platform/test_identity.py`, `core/tests/platform/test_real.py` (with an `httpx.MockTransport`).

- Identities (`identity.py`): `player_token(env)` parses `SOFTMAX_PLAYER_CREDENTIAL` (bare token or JSON `{"token","expires_at"}`), `user_token(env)` reads `SOFTMAX_USER_CREDENTIAL` or, if absent, `softmax_client.credentials.load_user_token(server=…)`; `server(env)` = `COGAMES_API_URL` default. `IdentityUnavailable(PlatformError)` when the User token is absent: message "User identity not provisioned for this workspace".
- Clients: `coworld.api_client.CoworldApiClient(server_url, token)`, `coworld.upload.CoworldUploadClient(server_url, token)`, `softmax_client.forum_wiki.ForumWikiClient(server=…, token=…)`, `softmax_client.http.observatory_client` for `request()`. Upload = `request_player_file_upload` → `httpx.put(upload_url, content=…)` → `complete_player_file_policy` (exact bytes; `content_hash` = hex sha256; the server verifies; the client also compares the returned version's hash when present and refuses a mismatch). Credits = `get_my_credits()`. Submit = `submit_to_league(league_id, UUID(pv), auto_champion=AutoChampion(…), player_id=…)`. XP = `create_experience_request(body)`, `get_experience_request`, `list_experience_request_episodes`, `coworld.episode_downloads.download_episodes(...)`. Episodes = `list_policy_version_episode_requests`, `get_episode_request`, `get_episode_request_artifact_bytes`. Coworld manifest = `coworld.upload.download_coworld(cow_id, server=…)`. Forum/wiki = `ForumWikiClient` methods (see reads/1.md §2.13–2.14).
- Errors: `httpx.HTTPStatusError` → `PlatformError(route, status, request_id=headers.get("x-request-id"), detail=body text, retryable=status in (429, 500, 502, 503, 504))`; 402 with a credits body → `CreditsRefused`; connection errors retryable.
- Every write records `identity: player|user` in its result for the caller to store.
- [ ] Tests: mock transport scripts for upload (3 calls), submit, credits, xp create/get, a 503 retried then success, a 402 → `CreditsRefused`, user identity absent → `IdentityUnavailable`.
- [ ] Commit: `Add the platform protocol and the real platform on the official clients`.

### Task 4.2: `FakePlatform`

**Files:** `platform/fake.py`, `core/tests/platform/test_fake.py`.

State: JSON at `<ws>/.state/fake_platform.json` under `locks.file_lock`, read-modify-write per
operation (as the old fake did), so the watcher process and tool subprocesses share it. Keep the
old mechanics: deterministic ids (`pv_<hash>`, `sub_<hash>`, `xreq_<hash>`), `polls_to_complete`,
seeded metrics with the `aggression`/`patience` lever read from an uploaded `policy.json`,
generated league episodes/standings, faults from `IDE_FAKE_FAULTS`: `credits=N`, `identity=user|rejected`,
`expires_in=<s>`, `fail_after=<op>` (raise after persisting), `xp_limit=N`, `upload=503` (first N
uploads return a retryable error: `upload=503:2`), `submit=409`, `champion=<pv>` (external champion
change), `platform=down` (every call raises retryable). Model the platform shapes of Task 4.1
(memberships with `status competing|pending`, `is_champion`, submissions `pending→placed`, XP
`pending→running→completed` by polls). `policy_file` returns the stored bytes. `coworld_manifest`
returns `{"id": "cow_fake", "name": "fakegame", "version": "1", "manifest": {"game": {"player_runtime": "game-hosted", "runnable": {"source_url": "<file:// path to tests/fixtures/fakegame_repo>", "commit": "<sha>"}}}}` (the fixture repo is created by a test helper `make_fakegame_repo(tmp)` with a `MIXIN.md`, `tools/build.sh`, a starter `policy.json`, skills and a `lessons/` folder — see Task 4.7).
- [ ] Tests: cross-process persistence (subprocess), each fault, seeded lever (same scores, fewer errors with higher patience), upload hash returned equals sha256 of the bytes, `champion` fault flips `is_champion`.
- [ ] Commit: `Add the fake platform`.

### Task 4.3: Mixin front matter and the code repository helpers

**Files:** `mixin.py`, `gitops.py` (extend: `init_repo`, `commit_all`, `worktree_add`, `worktree_remove`, `rev_parse`, `is_ancestor`, `merge_no_ff`, `tag`, `clean_check`, `commit_on_branch`), `core/tests/test_mixin.py`, `core/tests/test_gitops.py`.

```python
@dataclass class Mixin:
    coworld: str; coworld_version: str | None; commands: dict[str, str]   # build, check, upload, evaluate (each a shell command run in code/)
    metrics: list[dict]; artifact: dict   # {kind: file|image, path}
    evidence_types: list[dict]; toolchains: list[dict]; skills_dir: Path | None; path: Path
def load(mixin_dir: Path) -> Mixin        # parses MIXIN.md YAML front matter (PyYAML); problems → MixinError(problems)
def run_command(mixin: Mixin, name: str, *, cwd: Path, env, timeout=1800) -> CommandResult(exit_code, stdout, stderr, duration)
```
- [ ] Tests: front matter parse; missing `commands.build` is a problem; `run_command` captures output and exit code.
- [ ] Commit: `Add MIXIN.md front matter and git helpers`.

### Task 4.4: `version.create`/`version.adopt`, `upload`, `submit`, computed submission status, intents

**Files:** `platform/submission.py`, `tools/version.py` (extend), `tools/upload.py`, `tools/submit.py`,
`core/tests/tools/test_version_create.py`, `test_upload_tool.py`, `test_submit_tool.py`,
`core/tests/platform/test_intents_reconcile.py`.

- `version.create {summary, based_on, task?, experiment?, commit?}` (both roles; workspace executor): runs the mixin `build` command in the caller's worktree (Researcher: the held Task's worktree; Orchestrator: `orch-<n>` or `code/`), refuses `artifact.kind == image` ("image policies are not supported yet: hosted sandboxes have no Docker"), hashes the artifact file, `versions.create(..., artifact={kind:file, path, digest, size}, image=<runtime image digest if known>)`, tags `v/<id>` on the commit, returns the Version. The commit must be on the current branch; uncommitted changes → refused with the list of dirty files.
- `version.adopt {policy_version_id | label, summary}`: `platform.policy_versions` lookup; `versions.adopt`.
- `upload {version}`: `intents.begin(kind="upload", key=artifact.digest)`; if an existing pending intent → reconcile first (`platform.policy_versions(name=policy_name)` for a version with that content hash: found → `versions.mark_uploaded` + `intents.finish(done)` and return; not found → proceed); upload exact bytes; compare returned hash with `artifact.digest` (mismatch → `intents.finish(failed)` and refuse with both hashes); `versions.mark_uploaded`; `intents.finish(done)`; event `version.uploaded`; a notice when called from a wake. Never a hand-assembled upload.
- `submit {version, league, auto_champion?, reason?, user_requested: bool, cites?}` (Orchestrator): any uploaded Version. `user_requested=true` with `cites` (the user's message reference) skips every check; may add a warning list (no Experiment, not merged) but never refuses. Autonomous (`user_requested=false`): applies `submission.autonomous_allowed(store, version) -> (ok, reasons)`: `submissions.mode == autonomous`, window open (`until` null or in the future), `max_count` not reached (count events `version.submitted` with `autonomous: true` in the window), not paused, and if `quality_bar`: merged into main (`gitops.is_ancestor(main, commit)`), a confirmed Experiment against the current champion with effect ≥ `min_margin_over_champion`, or (no champion) the mixin `check` command passed for that commit. Refusal names each failed reason and says a user request overrides them. Intent `kind=submit key=<version>:<league>` with reconciliation via `platform.submissions(policy_version_id=…, league_id=…)`. On success: `champion.sync` from `platform.memberships(policy_version_id=…)`, event `version.submitted {autonomous, cites}`, `notices.add("Submitted V-n to <league name> …")`.
- `submission.status(store, platform, version) -> "built"|"uploaded"|"submitted"|"qualifying"|"competing"|"champion"` computed from the record and platform states (`pending|processing` → submitted; `placed` + membership `status competing` → competing; `is_champion` → champion; membership `pending` → qualifying). Cached on the version read for 60 s.
- [ ] Tests: `test_intents_reconcile_upload` (fault `fail_after=upload` → second call finds the policy version and does not upload twice; the fake counts uploads), `test_intents_reconcile_submit`, `test_submit_user_request_never_refused` (paused, quality bar failing, mode ask → still submits with warnings), `test_submit_autonomous_quality_bar` (unmerged → refused with reasons; merged + confirmed experiment → ok), `test_version_create_refuses_image`, `test_submission_status_ladder`.
- [ ] Commit: `Add versions, upload and submission with intents and the quality bar`.

### Task 4.5: `xp` tool, credits, `experiment.close` with the decision rule, stats

**Files:** `platform/xp.py`, `platform/credits.py`, `tools/xp.py`, `stats/{design,decision_rule,ingest,noise,bootstrap,power}.py` (lift the pure parts of the old modules, drop jsonschema and the Layout coupling; `design.py` validates with plain code), `tools/experiment.py` (extend `close`), `core/tests/tools/test_xp_tool.py`, `core/tests/stats/*`.

- `xp.request {experiment, candidate: V, baseline: V, seeds?: [int], episodes?: int, league?, opponents?, arm?: "pair"}` (both): checks credits first (`platform.credits()`; a refusal or `balance < estimate` → `tasks.wait(kind=credits)` for a task Session and a refusal naming balance/held/next refill), checks `xp.ask_above_requests_per_experiment` (above → the tool returns `{"ok": false, "ask_orchestrator": true, ...}` and, for a Researcher, calls `tasks.ask`), builds one request per (seed, arm) with `game_config_overrides.seed`, intent `kind=xp key=<experiment>:<seed>:<arm>` saving the exact body and UUID idempotency key locally, with recovery by idempotent POST replay (title `ide:X-<n>:<seed>:<arm>` is display only), records `experiments.add_run` at once with the request id, and for a task Session sets `tasks.wait(kind=xp, ref=<request ids>)`. `xp.status {experiment | request}`: refreshes runs from `platform.xp_get`, downloads results for completed runs into `<coworld>/documents/xp/<X-n>/<request>/` and runs the mixin `evaluate` command on them producing per-seat metric rows (`ingest.rows_from_evaluate_output`), stores them on the run (`results`), and reports progress.
- `experiment.close` computed path: `decision_rule.evaluate(design, pairs) -> Verdict(verdict, reason_codes, per_metric)` where verdict mapping: no failures → `confirmed`; `n_short` → `inconclusive`; `primary_not_significant` with enough pairs, or any guardrail/performance breach → `refuted`; the Orchestrator may pass an explicit verdict with `reason` which is recorded as `override`.
- `credits.py`: `read(platform) -> Credits`; `estimate(episodes, cost_per_episode)` using `Credits.recent_requests` average when present.
- [ ] Tests: request creation idempotent under `fail_after=xp_create`; credits refusal sets the wait; close verdict mapping (table-driven over failure codes); `ingest` pairs rows by seed; noise and power functions as in the old tests (port them).
- [ ] Commit: `Add XP requests, credits checks and the experiment decision rule`.

### Task 4.6: `platform` tool, `softmax_api`, `github`, `attachment_download`, `research softmax`

**Files:** `tools/platform.py`, `tools/softmax_api.py`, `tools/github.py`, `tools/attachment_download.py`, `platform/cli.py` (`research softmax exec -- <coworld|softmax> …` with identity env set; `research softmax whoami`), tests for each.

- `platform` (both; Researcher Player-only) actions: `leagues`, `league`, `divisions`, `standings`, `active_policies {league, exclude_player?, exclude_user?}` (memberships; `exclude_user` needs the User identity → lists all the user's Players via `softmax_client.players.list_players(token=user)` and excludes their policies), `policy_versions {player?, name?}`, `policy_file {policy_version}` (User identity; writes to `documents/policies/<id>` and returns the path), `results {player, league?}`, `episodes {policy_version | league}`, `episode {id}`, `replay {episode, dest?}` (artifact `replay` → file path + a pin), `credits`, `champion {league}`, `forum_read|forum_search|forum_list`, `wiki_read|wiki_search|wiki_history`, `coworld {id}` (manifest summary incl. `player_runtime`).
- `softmax_api {method, path, params?, json?, identity?}`: logs each call as an event `platform.request {method, path, identity}`; Researcher is Player only.
- `github` (Orchestrator): `repositories {page}` and `clone {repository, commit?}` through the Softmax broker as the old tool did (`GET {base}/github/repositories`, `git clone` with the bearer header via `GIT_CONFIG_COUNT`, pinned sha checkout, commit-keyed cache under `<coworld>/documents/repos/<owner>-<name>-<sha7>` — no uuid dirs).
- `attachment_download {sha256}` (Orchestrator): `GET /observatory/v2/cogents/{player}/artifacts/{sha256}` through `platform.request`, verify size+hash, store under `<coworld>/documents/attachments/<sha256>`; kept until the replacement route exists (A.5).
- `research softmax exec` keeps the identity env (`SOFTMAX_CONFIG_DIR` under `<ws>/.state/softmax` with `credentials.yaml` written from the Player token; `COWORLD_AGENT_GUIDANCE=0`) and refuses only identity-changing commands; a raw `coworld submit` runs (the advisory hook notes it).
- [ ] Tests on the fake platform; `exclude_user` without a User token → refusal naming the missing identity; `softmax_api` writes the event; github clone against a local bare repo via `file://` with the broker URL patched.
- [ ] Commit: `Add the platform, softmax_api, github and attachment tools`.

### Task 4.7: `workspace.coworld_switch`, the Coworld registry and the setup mechanics

**Files:** `coworld_setup.py`, `tools/workspace.py` (extend), `core/tests/test_coworld_setup.py`, `core/tests/fixtures/fakegame_repo.py` (builder).

```python
# coworld_setup.py (workspace executor)
def switch(ctx, coworld_id: str, *, source: str, drain: Callable) -> dict
# 1. drain(): asks the watcher-side helper to stop Researcher runs and mark them "resume after switch" (entry 5 supplies research_core.watcher.drain; until then a no-op hook)
# 2. registry upsert in the workspace store (coworlds.active flips under the partial unique index), event coworld.switched
# 3. layout dirs; if code/ has no .git: init with an empty commit on main
# 4. if the Coworld was never set up: fetch(ctx, coworld_id) and emit event coworld.setup_needed (the setup wake, entry 5/7)
# 5. prepare.apply_skill_dirs(profile_home, hermes, mixin_skills_dir, code_dir) — updates skills.external_dirs and trusted_project_dirs (next session picks them up)
def fetch(ctx, coworld_id) -> dict      # manifest → source_url + commit; git clone --depth 1 into source/ under a lock (commit-keyed); read COWORLD.md if present; find MIXIN.md (first match by walking source/); copy the mixin folder to code/mixin/ renaming each skill dir to <coworld_name>-<skill>; install toolchains declared in the front matter into <ws>/tools/<name>/ (document the supported kinds: "python" via uv python install + venv, "nim" via a pinned tarball URL in the declaration); commit "Import mixin from <source_url>@<sha>" on main; return what was found and what is missing (no mixin → {"mixin": None})
def import_lessons(ctx, source_dir) -> dict   # deterministic import for optimizer-seed seeds: claims with doc evidence pointing at source files (D.4 field map); other formats are left to the wake's judgement; returns counts
```
- The Orchestrator tool `workspace.coworld_switch {coworld, source}` calls `switch`; a user request switches at once with a notice "Switching to <name>; running work pauses and resumes on switching back".
- [ ] Tests: switch twice flips `active`; fetch from the fakegame fixture repo copies the mixin and prefixes skills (`fakegame-survey`); lesson import creates claims with `doc` evidence; a repository without MIXIN.md yields `mixin: None` and the event payload says so.
- [ ] Commit: `Add Coworld switching, source fetch, mixin import and lesson import`.

### Task 4.8: Scenario test — Goal to submission on the fake platform (tool level)

**Files:** `core/tests/scenarios/conftest.py` (fixtures: temp workspace, fake platform, fakegame Coworld switched and set up, a chat `ToolContext` for the Orchestrator and a task `ToolContext` factory for Researchers), `core/tests/scenarios/test_goal_to_submission.py`.

- [ ] Scenario (no watcher yet; call tools directly): `goal.create` → `task.create(type=improvement, experiment)` → `task.claim` (researcher ctx) → `version.create` (edit `policy.json` patience in the worktree, commit) → `experiment.start` → `xp.request` → fake completes → `xp.status` → `experiment.close` (verdict confirmed by the lever) → `task.complete(merge=yes, submission_candidate=yes)` → `upload` → `submit(user_requested=false)` refused (not merged) → `submit(user_requested=true, cites="msg-1")` succeeds → `version.read` shows `competing` → fake `champion` fault → `champion.sync` shows the change.
- [ ] Commit: `Add the Goal-to-submission scenario on the fake platform`.
