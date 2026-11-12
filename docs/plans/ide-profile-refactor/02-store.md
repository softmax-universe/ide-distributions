# Entry 2 — the store library

> **Implemented.** Status as of November 12, 2026: built and merged in PR #106 on October 29,
> 2026 (Pacific). The checkboxes were not kept up to date; the plan is historical. See
> [STATUS.md](STATUS.md) and `core/README.md` for the current state.
> **2026-11-12:** the `ui` tools described here were removed; `ui.tell` became `chat_update` (branch `jb/remove-ui-tools`).

Read `00-overview.md` first. The store is the fixed machinery: SQLite per Coworld plus a small
workspace store, numbered migrations, an append-only `events` table, token-marked writes with
detection, a schema check on open, leases with epochs, and per-table rules that refuse bad writes
with every problem at once. Everything here is **stdlib only** (it runs inside Hermes).

Spec: §5, B.1–B.16, Figure 3, Appendix H (settings), the draft tables quoted in
`reads/5.md` §2 (Experiment, Claim, Goal, Task, Version).

**Interfaces produced:**

```python
# research_core/store/errors.py
class StoreError(Exception):            # every refusal
    problems: list[str]                 # all problems at once
class SchemaMismatch(StoreError)
class NotFound(StoreError)
class LeaseError(StoreError)            # wrong holder / stale epoch

# research_core/store/actors.py
@dataclass(frozen=True)
class Actor:
    kind: Literal["session", "user", "harness"]; id: str; epoch: int | None = None
    def __str__(self) -> str            # "session:<id>" | "user:<id>" | "harness"
    @classmethod
    def parse(cls, text: str) -> Actor
    @classmethod
    def from_env(cls, env=os.environ) -> Actor   # IDE_SESSION_ID / HERMES_SESSION_ID → session; else harness

# research_core/store/db.py
class Store:
    path: Path; kind: Literal["coworld", "workspace"]
    @classmethod
    def open(cls, path: Path, *, kind) -> Store          # creates if absent (runs migrations), checks the schema, WAL
    def tx(self, actor: Actor) -> ContextManager[Tx]     # one SQLite transaction; commit stamps change_log + token
    def q(self, sql: str, params=()) -> list[sqlite3.Row]
    def one(self, sql, params=()) -> sqlite3.Row | None
    def events_after(self, seq: int, limit: int = 500) -> list[Event]
    def last_seq(self) -> int
    def schema_status(self) -> SchemaStatus              # .ok, .diff (list of table: expected vs actual)
    def unmarked_changes(self) -> list[dict]             # rows of change_log with token IS NULL
    def close(self)
class Tx:
    def execute(self, sql, params=()) -> sqlite3.Cursor
    def event(self, kind: str, subject: str, payload: dict) -> int   # inserts an events row; returns seq
    actor: Actor; token: str

# research_core/store/migrate.py
def apply_migrations(conn: sqlite3.Connection, kind: str) -> int      # PRAGMA user_version after
def expected_schema(kind: str) -> dict[str, str]                      # object name -> normalised SQL (from :memory:)
def actual_schema(conn) -> dict[str, str]
def rebuild_keeping_rows(path: Path, kind: str) -> RebuildReport      # new file from migrations, copy rows by shared columns, atomic replace

# research_core/store/events.py
@dataclass class Event: seq: int; at: str; actor: str; kind: str; subject: str; payload: dict

# research_core/store/registry.py  — the field registry (one source for validation, the SOUL Records section and tool schemas)
RECORDS: dict[str, RecordSpec]   # "goal","task","version","claim","experiment","directive","champion","capability","intent"
@dataclass class RecordSpec: name: str; prefix: str; table: str; fields: list[FieldSpec]; statuses: tuple[str,...]; transitions: dict[str, tuple[str,...]]; description: str
@dataclass class FieldSpec: name: str; type: str; required: bool; description: str; enum: tuple[str,...] | None = None; computed: bool = False
def display_id(prefix: str, n: int) -> str   # "G-3"
```

Record modules (`goals.py`, `tasks.py`, `claims.py`, `experiments.py`, `versions.py`,
`directives.py`, `settings.py`, `capabilities.py`, `intents.py`, `sessions.py`, `champion.py`,
`ui_state.py`, `pins.py`) expose plain functions `fn(store: Store, actor: Actor, ...) -> dict`
(the updated record as a JSON-ready dict, including computed fields and `id` in display form).
Reads take no actor. All validation raises `StoreError(problems)`.

Time: ISO 8601 UTC with microseconds (`2026-10-28T21:04:05.123456Z`), produced by one helper
`now_iso(clock=None)` so tests can inject a clock.

---

### Task 2.1: `db.py` and `migrate.py` — open, migrations, schema check, token-marked transactions, events, detection

**Files:** Create `store/__init__.py`, `store/errors.py`, `store/actors.py`, `store/db.py`,
`store/migrate.py`, `store/events.py`, `store/migrations/coworld/0001_init.sql`,
`store/migrations/workspace/0001_init.sql`, `core/tests/store/test_db.py`,
`core/tests/store/test_migrate.py`, `core/tests/store/test_actors.py`.

Schema `0001_init.sql` (Coworld store) — tables and the non-obvious constraints (write the full
DDL; names are binding):

- `meta(key TEXT PRIMARY KEY, value TEXT)` — `schema_kind`, `created`, `heartbeat`, `has_work`.
- `events(seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}')`; trigger `events_append_only` refuses UPDATE and DELETE (`RAISE(ABORT, 'events is append-only')`).
- `change_log(id INTEGER PRIMARY KEY, at TEXT NOT NULL, tbl TEXT NOT NULL, row_id INTEGER, op TEXT NOT NULL, token TEXT)`; AFTER INSERT/UPDATE/DELETE triggers on **every** record table (not on `change_log`, `meta`) insert a row with `token NULL`. The library's `Tx.__exit__` runs `UPDATE change_log SET token=? WHERE token IS NULL` inside the same transaction before commit, so any `NULL` token after commit is an unmarked write.
- `goals(id INTEGER PRIMARY KEY, title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200), end_state TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','parked','completed','abandoned')), status_reason TEXT, judgement TEXT, tags TEXT NOT NULL DEFAULT '[]', description TEXT NOT NULL DEFAULT '', where_it_stands TEXT NOT NULL DEFAULT '', updates_for_human TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, created_by TEXT NOT NULL, updated TEXT NOT NULL, CHECK(status NOT IN ('completed','abandoned','parked') OR (status_reason IS NOT NULL AND status_reason <> '')))`.
- `tasks(id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id) /* NULL only for maintenance, A2.2 */, type TEXT NOT NULL CHECK(type IN ('research','improvement','ideation','maintenance')), title TEXT NOT NULL, description TEXT NOT NULL, reasoning TEXT NOT NULL DEFAULT '', for_human TEXT NOT NULL DEFAULT '', end_condition TEXT CHECK(end_condition IN ('deliverable','steps','effort')), end_detail TEXT, experiment_id INTEGER REFERENCES experiments(id), status TEXT NOT NULL CHECK(status IN ('proposed','unclaimed','in-progress','complete','abandoned')), priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('normal','high')), proposed_by TEXT, approved_by TEXT, approved_at TEXT, outcome TEXT CHECK(outcome IN ('succeeded','failed','inconclusive','abandoned')), result TEXT, failure TEXT, last_error_seen TEXT, waiting_on TEXT, held_by TEXT, lease_epoch INTEGER NOT NULL DEFAULT 0, lease_renewed TEXT, run_id TEXT, worktree TEXT, branch TEXT, ideation_round INTEGER, checklist TEXT, merge TEXT, completed_commit TEXT, capability TEXT, created TEXT NOT NULL, created_by TEXT NOT NULL, updated TEXT NOT NULL, CHECK(status <> 'abandoned' OR failure IS NOT NULL))`.
- `task_deps(task_id INTEGER REFERENCES tasks(id), depends_on INTEGER REFERENCES tasks(id), PRIMARY KEY(task_id, depends_on), CHECK(task_id <> depends_on))`.
- `task_sessions(task_id, session_id, started TEXT, PRIMARY KEY(task_id, session_id))`.
- `task_messages(id INTEGER PRIMARY KEY, task_id REFERENCES tasks(id), direction TEXT CHECK(direction IN ('to_researcher','to_orchestrator')), kind TEXT CHECK(kind IN ('message','question','answer')), text TEXT NOT NULL, created TEXT, created_by TEXT, delivered TEXT)`.
- `sessions(id TEXT PRIMARY KEY, profile TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('chat','task','wake')), task_id INTEGER REFERENCES tasks(id), goal_id INTEGER, event TEXT, parent TEXT, run_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, started TEXT NOT NULL, ended TEXT, last_seen TEXT, session_context TEXT, client_capabilities TEXT, drained INTEGER NOT NULL DEFAULT 0)`.
- `versions(id INTEGER PRIMARY KEY, summary TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('built','adopted')), commit_sha TEXT, branch TEXT, artifact TEXT, task_id INTEGER REFERENCES tasks(id), experiment_id INTEGER REFERENCES experiments(id), uploaded TEXT, image TEXT, policy_name TEXT, created TEXT NOT NULL, created_by TEXT NOT NULL)` with `CHECK(source <> 'built' OR (commit_sha IS NOT NULL AND artifact IS NOT NULL))`; `version_parents(version_id, parent_id, PRIMARY KEY(version_id,parent_id))`.
- `champion(league_id TEXT PRIMARY KEY, division_id TEXT, membership_id TEXT, version_id INTEGER REFERENCES versions(id), policy_version_id TEXT, state TEXT CHECK(state IN ('pending','processing','placed','rejected','champion','none')), since TEXT, approved_by TEXT, reason TEXT, synced TEXT)`; `champion_history(id INTEGER PRIMARY KEY, league_id, policy_version_id, state, at, note)`.
- `claims(id INTEGER PRIMARY KEY, summary TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL CHECK(status IN ('active','superseded','refuted')), superseded_by INTEGER REFERENCES claims(id), see_also TEXT NOT NULL DEFAULT '[]', created TEXT, created_by TEXT, updated TEXT)`; `claim_evidence(id INTEGER PRIMARY KEY, claim_id REFERENCES claims(id), type TEXT NOT NULL, ref TEXT NOT NULL, stance TEXT NOT NULL CHECK(stance IN ('supports','contradicts','qualifies')), note TEXT NOT NULL DEFAULT '', added TEXT, by TEXT)`; FTS5 `claims_fts(summary, body, tags, content='claims', content_rowid='id')` with the three sync triggers.
- `pins(id INTEGER PRIMARY KEY, kind TEXT, ref TEXT, download TEXT, created TEXT, UNIQUE(kind, ref))`.
- `experiments(id INTEGER PRIMARY KEY, summary TEXT NOT NULL, goal_id INTEGER REFERENCES goals(id), task_id INTEGER REFERENCES tasks(id), tags TEXT DEFAULT '[]', status TEXT NOT NULL CHECK(status IN ('proposed','running','confirmed','refuted','inconclusive','abandoned')), hypothesis TEXT NOT NULL DEFAULT '{}', methods TEXT NOT NULL DEFAULT '{}', results TEXT NOT NULL DEFAULT '{}', related_claims TEXT DEFAULT '[]', related_experiments TEXT DEFAULT '[]', design_sha256 TEXT, created TEXT, closed TEXT, abandon_reason TEXT, created_by TEXT)`; `experiment_runs(id INTEGER PRIMARY KEY, experiment_id REFERENCES experiments(id), kind TEXT, platform_request_id TEXT, arm TEXT, seed INTEGER, validity TEXT NOT NULL DEFAULT 'valid', state TEXT, episodes_total INTEGER, episodes_completed INTEGER, results TEXT, created TEXT, updated TEXT)`.
- `directives(id INTEGER PRIMARY KEY, text TEXT NOT NULL, scope TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','retired')), created TEXT, created_by TEXT, retired TEXT)` (Coworld store holds `goal:<id>`-scoped directives; the workspace store holds `workspace` scope).
- `capabilities(name TEXT PRIMARY KEY, kind TEXT, status TEXT NOT NULL CHECK(status IN ('unknown','ok','broken')), last_check TEXT, output TEXT, maintenance_task_id INTEGER REFERENCES tasks(id), command TEXT)`.
- `settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated TEXT, updated_by TEXT, cites TEXT)`.
- `ui_state(conversation TEXT PRIMARY KEY, doc TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 0, updated TEXT)`.
- `intents(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('upload','submit','xp','forum','wiki','run','champion')), subject TEXT, key TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','done','failed')), result TEXT, created TEXT, resolved TEXT, created_by TEXT, UNIQUE(kind, key))`.
- `notices(id INTEGER PRIMARY KEY, text TEXT NOT NULL, refs TEXT DEFAULT '[]', urgency TEXT CHECK(urgency IN ('low','normal','high')), created TEXT, created_by TEXT, read_at TEXT, read_by TEXT)` — the store side of `ui.notify`; each notice also writes an `events` row of kind `notice`.

Workspace store `0001_init.sql`: `meta`, `events`, `change_log`, `coworlds(coworld_id TEXT PRIMARY KEY, folder TEXT NOT NULL UNIQUE, mixin_version TEXT, tool_check_status TEXT, set_up_at TEXT, active INTEGER NOT NULL DEFAULT 0, created TEXT)` with a partial unique index `one_active ON coworlds(active) WHERE active = 1`, `directives` (as above), `settings` (as above), `notices` (as above; workspace-level notices such as "drive disconnected"), `sessions` (same shape, so chat sessions can be registered before a Coworld is active), `ui_state`.

`db.py` behaviour:
- `open` creates parent dirs, sets `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`; if `user_version == 0` applies migrations; compares `actual_schema` with `expected_schema(kind)` (normalise whitespace and case; compare the set of tables, indexes, triggers and their SQL) and raises `SchemaMismatch` with the diff otherwise.
- `tx(actor)` yields a `Tx` bound to one connection; nested use raises; on exception rollback; before commit it stamps `change_log` with `Tx.token = secrets.token_hex(8)`; `event()` is only valid inside a `Tx` and records `actor=str(actor)`.
- `events_after`, `last_seq`, `unmarked_changes` (rows with `token IS NULL`, plus `meta.schema_kind` check).
- A module-level README text `STATE_README` written by `open` into `<dir>/README.md` if absent: "Records live in store.db and are changed through the profile tools (goal, task, claim, …). Direct edits are detected and reported. Do not edit files here."

- [ ] **Step 1:** Tests (`test_db.py`): `test_open_creates_and_migrates` (user_version == 1, WAL), `test_tx_writes_event_and_stamps_change_log` (after a goal insert via raw `tx.execute`, `change_log` rows have the tx token and `events` has one row), `test_unmarked_write_detected` (a second plain `sqlite3` connection inserts a goal → `unmarked_changes()` returns one row naming `goals`), `test_events_append_only` (UPDATE events raises), `test_schema_mismatch_refused` (`ALTER TABLE goals ADD COLUMN x` via raw sqlite → `Store.open` raises `SchemaMismatch` listing `goals`), `test_rebuild_keeps_rows` (after the ALTER, `rebuild_keeping_rows` yields a store that opens, keeps the goal rows, and user_version is current), `test_two_stores_independent_seq` (workspace and coworld stores each start at seq 1). `test_actors.py`: parse/str round trip, `from_env`. `test_migrate.py`: `expected_schema("coworld")` has every table listed above; migrations are applied in numeric order; a second `apply_migrations` is a no-op.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Run → pass.
- [ ] **Step 5:** Commit: `Add the store library: migrations, token-marked transactions, events, schema check`.

### Task 2.2: `registry.py` and `_common.py` helpers

**Files:** Create `store/registry.py`, `store/_common.py` (`now_iso`, `json_field`, `parse_json`, `require(problems, cond, msg)`, `word_count`, `display_id`, `parse_display_id("G-3") -> ("goal", 3)`), `core/tests/store/test_registry.py`.

- [ ] Registry entries for every record kind with field specs (copy the field meanings from the spec tables B.3–B.9 and the draft tables in reads/5.md; mark computed fields). Test: every table column of the migration (except internal `updated`, `lease_*`) appears in its RecordSpec; prefixes are unique; `parse_display_id` round-trips and rejects `G-0`, `g-3`, `GX-1`.
- [ ] Commit: `Add the record field registry`.

### Task 2.3: Goals and directives

**Files:** `store/goals.py`, `store/directives.py`, `core/tests/store/test_goals.py`, `test_directives.py`.

```python
goals.create(store, actor, *, title, end_state, reason, description="", judgement=None, tags=()) -> dict   # status active; event goal.created
goals.update(store, actor, goal_id, **fields) -> dict        # title,end_state,reason,description,judgement,tags,where_it_stands; appends to updates_for_human when `update_for_human=` given
goals.set_status(store, actor, goal_id, status, status_reason) -> dict   # transitions: active→parked|completed|abandoned, parked→active|abandoned; event goal.status
goals.read(store, goal_id) -> dict   # + computed: tasks {status: count, open: [ids]}, experiments {status: count}, claims (tag G-n, with freshness), ideation_rounds_without_progress, last_progress, directives (goal scope, active)
goals.list(store, status=None) -> list[dict]
goals.needs_ideation(store) -> list[int]       # active goals with no open task and no task waiting on user
goals.progress_since_round(store, goal_id, round) -> bool   # any task completed succeeded / claim created / version created after the round's ideation task was created
directives.add(store, actor, *, text, scope, source) -> dict       # scope "workspace" | "goal:<n>"; source = message reference text
directives.update(store, actor, directive_id, *, text=None, scope=None) -> dict
directives.retire(store, actor, directive_id) -> dict
directives.list(store, scope=None, status="active") -> list[dict]
```

- [ ] Tests: status transitions and required reasons (every problem reported at once: e.g. `set_status(…,"completed", "")` yields two problems: empty reason + nothing else? keep to real problems), `read` computed counts, `needs_ideation`, directive scope validation (`goal:99` missing → NotFound in problems), retire is idempotent-refusing (retiring twice → problem).
- [ ] Commit: `Add goals and directives`.

### Task 2.4: Tasks — statuses, approval, leases with epochs, waits, messages, dependencies, checklists

**Files:** `store/tasks.py`, `core/tests/store/test_tasks.py`, `core/tests/store/test_tasks_lease.py`.

```python
tasks.create(store, actor, *, goal_id, type, title, description, reasoning="", for_human="", end_condition=None, end_detail=None, experiment_id=None, priority="normal", depends_on=(), status="unclaimed"|"proposed", ideation_round=None) -> dict
# rules: improvement tasks may carry exactly one experiment; at most one open ideation task per goal (open = proposed|unclaimed|in-progress); proposed_by = actor when status proposed
tasks.propose(store, actor, **same) -> dict               # status proposed (Researchers)
tasks.approve(store, actor, task_id) -> dict              # proposed→unclaimed; sets approved_by/at; refused if settings approval_required.task_types contains type and actor.kind != "user" (settings looked up via settings.get)
tasks.update(store, actor, task_id, **fields) -> dict      # only in proposed|unclaimed; clears approval
tasks.link(store, actor, task_id, depends_on: list[int]) -> dict    # cycle check
tasks.claim(store, actor, task_id, *, run_id, worktree, branch) -> dict
# refuses: status != unclaimed; not approved; deps not complete; another holder with a fresh lease (<5 min); raises lease_epoch, held_by=actor.id, run_id, status in-progress, adds task_sessions row; event task.claimed. Of two concurrent claims one wins (BEGIN IMMEDIATE + a status re-check inside the tx).
tasks.renew_lease(store, actor, task_id) -> dict
tasks.require_holder(store, task_id, actor) -> None        # LeaseError unless held_by == actor.id and lease_epoch == actor.epoch
tasks.release(store, actor, task_id, *, reason) -> dict     # in-progress → unclaimed, lease cleared, event task.released
tasks.wait(store, actor, task_id, *, kind, detail, ref=None) -> dict   # kind ∈ question|process|xp|credits|permit|submission|platform|user|dependency; sets waiting_on {kind,detail,ref,since}; event task.waiting
tasks.resolve_wait(store, actor, task_id, *, note) -> dict  # clears waiting_on; event task.wait_resolved
tasks.ask(store, actor, task_id, *, question) -> dict       # task_messages(to_orchestrator, question) + wait(kind question); event task.asked
tasks.answer(store, actor, task_id, *, text, question_id=None) -> dict   # to_researcher answer; resolves the question wait; event task.answered
tasks.message(store, actor, task_id, *, text, direction) -> dict          # event task.message
tasks.pending_messages(store, task_id, direction) -> list[dict]; tasks.mark_delivered(store, actor, message_ids)
tasks.complete(store, actor, task_id, *, checklist: dict, commit: str | None, merge_request: "yes"|"no", merge_reason="", submission_candidate: "yes"|"no"|None=None, result: str) -> dict
# requires holder; validates checklist against task_types (entry 7 supplies the type files; until then a stub in tasks.py TYPE_CHECKLISTS); records completed_commit = commit; status complete; outcome succeeded; event task.completed (the review wake's trigger)
tasks.reopen(store, actor, task_id, *, reason) -> dict      # complete → in-progress (same session resumes); event task.reopened
tasks.review(store, actor, task_id, *, accept: bool, merge: "yes"|"no", reason) -> dict   # accept → merge field "pending" when merge yes else "not merged: <reason>"; event task.reviewed; reject → reopen
tasks.abandon(store, actor, task_id, *, failure: dict) -> dict   # from any open state; requires failure {what_failed,error,exit_code,tried,next_step}; outcome abandoned; event task.abandoned
tasks.fail(store, actor, task_id, *, failure) -> dict          # outcome failed, status complete? No: failed tasks are complete with outcome failed (B.4 "A failed Task records what failed and a next step"); event task.failed
tasks.read(store, task_id) -> dict      # + sessions, deps, messages, computed `stuck` (in-progress, no live run flag set by watcher, no waiting_on, lease_renewed older than 5 min)
tasks.list(store, *, status=None, goal_id=None, type=None, held_by=None) -> list[dict]
tasks.dispatchable(store) -> list[dict]  # unclaimed, approved, deps complete, ordered priority high first then created
tasks.set_run(store, actor, task_id, run_id) ; tasks.set_merge(store, actor, task_id, merge: str)
tasks.set_last_error(store, actor, task_id, error: dict)
```

- [ ] Tests: creation rules (ideation uniqueness per goal, improvement experiment), approval setting (`approval_required.task_types=["improvement"]` makes a session approve fail with the reason and a `user:` actor succeed), `test_claim_requires_approval_and_deps`, `test_tasks_lease`: two actors claim; the second gets `LeaseError`; after the lease ages past 5 minutes (inject clock) a re-claim raises the epoch and the old holder's `require_holder` fails; `test_complete_requires_holder_and_checklist` (missing items are named, all at once), `test_review_reopen_same_task`, `test_abandon_requires_failure_fields` (each missing key is a problem), `test_dispatchable_order`, `test_wait_kinds_validated`, `test_ask_answer_roundtrip`.
- [ ] Commit: `Add tasks: statuses, approval, leases with epochs, waits, messages`.

### Task 2.5: Claims and evidence

**Files:** `store/claims.py`, `store/evidence_types.py`, `core/tests/store/test_claims.py`.

```python
# evidence_types.py — the registry (B.7): each type has ref_format (doc string), validate(ref) -> list[str], inspect(ctx, ref) -> dict (entry 3 wires inspectors needing the workspace), skill (name)
EVIDENCE_TYPES = {"claim": …, "code": …, "doc": …, "transcript": …, "measurement": …, "experiment": …, "version": …, "replay": …, "annotation": …}
# code ref: "<path>:<a>-<b>@<sha>" (sha 7-40 hex); doc: "<repo-or-url>#<section>" or "<url> (retrieved <date>): <quote>"; transcript: "session:<id>/<message id>#<sha256 of quote>"; measurement: "<command> @<sha> -> <stored output path or pin id>"; experiment: "X-<n>"; version: "V-<n>"; claim: "C-<n>"; replay: "episode:<id>[/seat <n>][/phase <p>] xp:<request id>"; annotation: "annotation:<id>"
claims.write(store, actor, *, summary, body, tags, evidence: list[dict], see_also=()) -> dict   # ≥1 evidence; ≤40 words / ≤500; every ref validates and resolves (C-/X-/V- must exist); claim→claim cycle check; event claim.created; new tags report "nearest existing tags" in the result (difflib)
claims.update(store, actor, claim_id, *, summary=None, body=None, tags=None, see_also=None) -> dict
claims.add_evidence(store, actor, claim_id, *, type, ref, stance, note) -> dict
claims.retire(store, actor, claim_id, *, status: "superseded"|"refuted", superseded_by=None, evidence: dict | None) -> dict  # refuted requires killing evidence; superseded requires an existing claim
claims.read(store, claim_id) -> dict    # + computed freshness (newest supporting/qualifying evidence added), contested (contradicting evidence newer than freshness), counts, cited_by (experiments/claims)
claims.search(store, query, *, tags=None, include_retired=True, limit=20) -> list[dict]   # FTS5 match; retired listed last and marked
claims.lint(store) -> dict              # single-use tags, dangling see_also
```

- [ ] Tests: word limits (both problems at once), evidence requirement, cycle refusal (A cites B cites A), freshness/contested computation with injected times, search ranks summary matches and lists refuted last, nearest-tags hint, retire rules.
- [ ] Commit: `Add claims with typed evidence, freshness and search`.

### Task 2.6: Experiments

**Files:** `store/experiments.py`, `core/tests/store/test_experiments.py`.

```python
experiments.create(store, actor, *, summary, goal_id=None, task_id=None, tags=(), hypothesis: dict, methods: dict, related_claims=(), related_experiments=()) -> dict  # status proposed
# hypothesis keys: statement, if_true, if_false (must differ), motivation; methods keys: baseline (V-id), candidate (V-id or null), procedure, metrics (list of {name, direction up|down, role primary|guardrail|performance, min_effect?, tolerance?}), decision_rule (text + {n_min, k?}); any Version may be the baseline
experiments.update(store, actor, experiment_id, *, summary=None, hypothesis=None, methods=None, tags=None, related_*=None) -> dict   # only while proposed
experiments.start(store, actor, experiment_id) -> dict   # requires hypothesis+methods complete incl. decision_rule and ≥1 primary metric; locks; design_sha256 = sha256(json of hypothesis+methods); status running; event experiment.started
experiments.add_run(store, actor, experiment_id, *, kind, platform_request_id, arm, seed=None, episodes_total=None) -> dict   # running only; records request id at once (B.13)
experiments.update_run(store, actor, run_id, *, state=None, episodes_completed=None, results=None, validity=None) -> dict
experiments.close(store, actor, experiment_id, *, verdict: "confirmed"|"refuted"|"inconclusive", measurements: list[dict], interpretation: str, reason_codes: list[str]) -> dict   # requires ≥1 run, measurements, interpretation; event experiment.closed
experiments.abandon(store, actor, experiment_id, *, reason) -> dict   # never delete; event experiment.abandoned
experiments.read(store, experiment_id, section=None) -> dict   # + cited_by claims, versions tested
experiments.search(store, query, limit=20) -> list[dict]
```

- [ ] Tests: start lock (update after start refused), start requirements (every missing part named), close requirements, if_true == if_false refused, abandon-and-recreate (related_experiments must resolve), measurements shape.
- [ ] Commit: `Add experiments with a locked design and verdicts`.

### Task 2.7: Versions and champion

**Files:** `store/versions.py`, `store/champion.py`, `core/tests/store/test_versions.py`.

```python
versions.create(store, actor, *, summary, commit_sha, branch, artifact: dict, based_on: list[int], task_id=None, experiment_id=None, image=None, policy_name=None) -> dict   # source built; artifact {kind: file, path, digest, size}; event version.created
versions.adopt(store, actor, *, summary, policy_version_id, policy_name, uploaded_at, digest=None) -> dict   # source adopted; uploaded set
versions.mark_uploaded(store, actor, version_id, *, policy_version_id, at, digest_checked: bool) -> dict   # at most one upload per version (second call with a different id is refused; same id is a no-op)
versions.read(store, version_id) -> dict    # + submission_status (computed, Task 4.x: uses champion + platform states cached on the record), experiments using it
versions.list(store, limit=20) -> list[dict]
versions.is_merged_into_main(code_repo: Path, commit_sha) -> bool   # git merge-base --is-ancestor (gitops; entry 4 wires)
champion.sync(store, actor, *, league_id, division_id, membership_id, policy_version_id, state, reason=None) -> dict  # upsert; writes champion_history + event champion.changed when policy_version_id or state changes
champion.read(store, league_id) -> dict | None
```

- [ ] Tests: built requires commit+artifact; adopted needs no commit; one upload rule; champion change produces history + event, unchanged sync produces none.
- [ ] Commit: `Add versions and the champion record`.

### Task 2.8: Settings, capabilities, intents, sessions, ui_state, notices, pins

**Files:** `store/settings.py`, `store/capabilities.py`, `store/intents.py`, `store/sessions.py`, `store/ui_state.py`, `store/notices.py`, `store/pins.py`, tests for each.

```python
# settings.py — SETTINGS registry: key -> SettingSpec(type, default, scope "workspace"|"coworld", enforced_by, description); defaults from Appendix H
settings.get(store, key) -> value (default when unset); settings.all(store) -> dict
settings.update(store, actor, key, value, *, cites: str) -> dict   # type-checked; cites required (the user's message reference); event settings.updated
# capabilities.py
capabilities.mark(store, actor, name, *, status, output, command=None, kind=None) -> dict   # event capability.failed on ok→broken, capability.repaired on broken→ok
capabilities.set_maintenance_task(store, actor, name, task_id); capabilities.status(store) -> list[dict]; capabilities.broken(store) -> list[str]
# intents.py (B.13)
intents.begin(store, actor, *, kind, key, subject) -> dict        # UNIQUE(kind,key): an existing pending row is returned with existing=True
intents.finish(store, actor, intent_id, *, state: "done"|"failed", result: dict) -> dict
intents.pending(store, kind=None) -> list[dict]
# sessions.py
sessions.register(store, actor, *, session_id, profile, kind, task_id=None, goal_id=None, event=None, parent=None) -> dict   # upsert; event session.started on insert
sessions.set_run(store, actor, session_id, run_id, attempts=None); sessions.end(store, actor, session_id)
sessions.read(store, session_id) -> dict | None; sessions.for_task(store, task_id) -> list[dict]; sessions.mark_drained(store, actor, session_ids, drained: bool)
# ui_state.py — JSON Patch (RFC 6902) applied to the conversation's doc; event ui.patch with the patch
ui_state.patch(store, actor, conversation, ops: list[dict]) -> dict; ui_state.get(store, conversation) -> dict
# notices.py
notices.add(store, actor, *, text, refs=(), urgency="normal") -> dict    # event notice
notices.unread(store, limit=20) -> list[dict]; notices.mark_read(store, actor, ids, by)
# pins.py
pins.add(store, actor, *, kind, ref, download: dict) -> dict; pins.find(store, kind, ref)
```

- [ ] Tests: settings type check and default; `cites` required; intents uniqueness returns the existing pending row; capability transitions emit the right events; ui_state patch add/replace/remove and invalid path refused; notices unread/mark_read; sessions upsert.
- [ ] Commit: `Add settings, capabilities, intents, sessions, UI state, notices and pins`.

### Task 2.9: `refs.py` — the one reference parser

**Files:** `core/src/research_core/refs.py`, `core/tests/test_refs.py`.

```python
@dataclass(frozen=True)
class Ref:
    kind: Literal["record","player","policy_version","policy_label","league","division","episode","xp_request","submission","url","unknown"]
    value: str; record_kind: str | None = None; number: int | None = None; fragment: str | None = None
def parse(text: str) -> Ref
# "G-3", "T-42#failure" → record; "ply_<uuid>" → player; a UUID → policy_version; "name:v3" → policy_label;
# "league_…", "div_…", "ereq_…", "xreq_…", "sub_…"; https://softmax.com/... URLs mapped by path to league/division/episode/policy ids; else unknown
def parse_many(text: str) -> list[Ref]      # all references found in free text
def resolve_record(store, ref: Ref) -> dict  # NotFound problems otherwise
```

- [ ] Tests: each form above (table-driven), fragments, URL forms (`/leagues/<id>`, `/observatory/v2/episode-requests/<id>`), `parse_many` on a sentence with three refs.
- [ ] Commit: `Add the reference parser`.

### Task 2.10: `store export` and `store apply` (CLI) and the store-side of the user-edit route

**Files:** `core/src/research_core/store/cli.py` (adds `store` subcommands to `research`), `core/tests/store/test_store_cli.py`.

- `research store apply --actor user:<id> <op> <json>`: `op` is `<module>.<function>` from an allow-list (`goals.set_status`, `goals.update`, `tasks.approve`, `tasks.update`, `settings.update`, `directives.add|update|retire`, `workspace.pause`, `workspace.resume`) → calls the library function; prints the record and the new `events` seq (`{"record":…, "seq": n}`); refusals exit 1 with `{"error": "...", "problems": [...]}`.
- `research store export [--coworld <id>] --out <dir>`: JSON lines per table + events.
- `research store snapshot` and `research store events --after <seq> [--follow]`: for the seam (entry 6) and debugging.

- [ ] Tests: apply round trip with a user actor; disallowed op refused; export writes every table; events --after returns rows > seq.
- [ ] Commit: `Add store apply, export, snapshot and events commands`.
