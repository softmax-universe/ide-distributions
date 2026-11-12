# Entry 3 — record tools, plugin wiring, briefing, session context, advisory hook

> **Implemented.** Status as of November 12, 2026: built and merged in PR #106 on October 29,
> 2026 (Pacific). The checkboxes were not kept up to date; the plan is historical. Since then the
> 13 hardest write actions became their own tools (#188), so the Orchestrator has 24 tools and a
> Researcher 23. See [STATUS.md](STATUS.md) and `core/README.md` for the current state.
> **2026-11-12:** the `ui` tools described here were removed; `ui.tell` became `chat_update` (branch `jb/remove-ui-tools`).

Read `00-overview.md` and the interfaces of `02-store.md` first. After this entry both profiles
expose their record tools (the platform tools arrive in entry 4 as further `ToolSpec`s in the same
catalog), the briefing and session-context prompt sections render, the advisory hook annotates
bypassing commands, and the `last_error_seen` hook fills Tasks.

Spec: §4 (tool naming, budget), §9, C.2 (tool table), C.4 (advisory hook), B.10, B.17, H
(briefing contents), A.4/A.8 (only the parts the seam needs: `ui.notify`, the context block).

**Interfaces produced:**

```python
# research_core/tools/base.py
Role = Literal["orchestrator", "researcher"]
@dataclass(frozen=True)
class ActionSpec: name: str; description: str; params: dict[str, dict]; required: tuple[str, ...]; roles: frozenset[Role]
@dataclass(frozen=True)
class ToolSpec:
    name: str; description: str; roles: frozenset[Role]; executor: Literal["inproc", "workspace"]
    actions: tuple[ActionSpec, ...] | None          # None = a single-purpose tool with flat params
    params: dict[str, dict]; required: tuple[str, ...]
    handler: Callable[["ToolCall"], dict]            # returns a JSON-ready dict; raises ToolError/StoreError
    def schema(self, role: Role) -> dict             # OpenAI function body; action enum limited to the role's actions
@dataclass
class ToolContext:
    role: Role; actor: Actor; workspace: Workspace; env: Mapping[str, str]
    def coworld(self) -> Coworld            # active Coworld (from the workspace store); raises ToolError("no active Coworld; use workspace.coworld_switch") 
    def store(self) -> Store                # the active Coworld store
    def wstore(self) -> Store               # the workspace store
    def platform(self) -> "Platform"        # entry 4
    def hermes_session_id(self) -> str
@dataclass
class ToolCall: spec: ToolSpec; args: dict; ctx: ToolContext; action: str | None
class ToolError(Exception): problems: list[str]; context: dict
def run_tool(name: str, args: dict, ctx: ToolContext) -> str    # JSON text; validates (unknown fields, missing required, enum, type) reporting all problems; never raises
def validate_args(spec, role, args) -> tuple[str | None, list[str]]

# research_core/tools/catalog.py
TOOLS: dict[str, ToolSpec]      # filled by each tool module at import (tools/__init__ imports them all)
def tools_for(role) -> list[ToolSpec]   # sorted by name; test: orchestrator 18, researcher 14 (after entry 4 and 7 add theirs)
```

Result conventions (every tool): success → `{"ok": true, "record": {...}}` or `{"ok": true, "records": [...]}`
or `{"ok": true, ...}`; refusal → `{"ok": false, "error": "<one line>", "problems": [...], "context": {...}}`.
Tool errors from subprocesses include `exit_code`, `stdout_tail`, `stderr_tail`, `command`.
No store path ever appears in a result (B.10): tests grep results for `.state/`.

---

### Task 3.1: `base.py`, `catalog.py`, the executor and the plugin handler

**Files:** `tools/base.py`, `tools/catalog.py`, `tools/__init__.py`, `plugin/executor.py`,
`plugin/register.py` (extend), `cli.py` (`research tool <name> --args <json>`),
`core/tests/tools/test_base.py`, `core/tests/plugin/test_executor.py`.

```python
# plugin/executor.py
def make_handler(spec: ToolSpec, role: Role) -> Callable[..., str]
# def handler(args, session_id="", **kw) -> str:
#   env = os.environ with IDE_SESSION_ID=session_id, IDE_ROLE=role
#   if spec.executor == "inproc": return run_tool(spec.name, args, ToolContext.from_env(role, env))
#   else: subprocess.run([ws.venv_python, "-m", "research_core.cli", "tool", spec.name, "--args", json.dumps(args)], env=env, capture_output=True, timeout=400)
#        → stdout is the JSON; a non-zero exit or empty stdout yields {"ok": false, "error": "tool process failed", exit_code, stderr_tail, command}
# register.py: register(ctx) registers tools_for(role) under toolset f"ide-{role}", plus hooks and sections from Tasks 3.8–3.10
```

- [ ] Tests: `validate_args` reports unknown field + missing required + bad enum together; `run_tool` wraps `StoreError.problems`; executor inproc path returns JSON; workspace path with a fake python (a tiny script that echoes) returns stdout; failure path includes `exit_code` and `stderr_tail`; `make_register` registers the catalog's tools with the right toolset and schema shape (`{"name","description","parameters"}`).
- [ ] Commit: `Add the tool base, catalog and plugin executor`.

### Task 3.2: `goal` and `directive` tools

**Files:** `tools/goal.py`, `tools/directive.py`, `core/tests/tools/test_goal_tool.py`, `test_directive_tool.py`.

- `goal`: actions `create`, `update`, `set_status` (Orchestrator); `read`, `list` (both). Flat params: `goal` (ref), `title`, `end_state`, `reason`, `description`, `judgement`, `tags`, `status`, `status_reason`, `update_for_human`, `where_it_stands`.
- `directive`: `add`, `update`, `retire` (Orchestrator); `list` (both). Params `directive`, `text`, `scope` (`workspace` | a Goal ref), `source` (the user's message reference, required on `add`). `add` with scope `workspace` writes to the workspace store; a Goal scope writes to the Coworld store.
- [ ] Tests: role gating (a Researcher calling `goal.create` gets a refusal naming the allowed actions), each action round trip, refs accepted as `G-3` or `3`.
- [ ] Commit: `Add the goal and directive tools`.

### Task 3.3: `task` tool

**Files:** `tools/task.py`, `core/tests/tools/test_task_tool.py`.

Actions and roles (C.2): Orchestrator: `create`, `approve`, `link`, `answer`, `message`, `abandon`, `review`; Researcher: `claim`, `wait`, `ask`, `complete`, `release`, `propose_submission`; both: `propose`, `read`, `list`.

- `claim` (workspace executor; it creates the worktree): requires `IDE_SESSION_ID`; `gitops.worktree_add(code_repo, path, branch=f"task/{n}", base="main")`; registers the Session (`sessions.register(kind="task", task_id)`), then `tasks.claim(..., run_id=args.get("run_id") or env HERMES_RUN_ID or "", worktree, branch)`. Returns the Task plus `worktree` path and the type instructions (`task_types/<type>.md` body; entry 7 writes the real files, this entry ships minimal placeholders with the checklist front matter from B.4).
- `complete`: `checklist` (dict), `commit` (sha; must exist on the Task branch: `gitops.commit_on_branch`), `merge` (`yes|no`), `merge_reason`, `submission_candidate`, `result`. Enforces the type checklist (B.4 table) via `task_types.load()`: `research` → claims list or `none: <reason>`; `improvement` → claims, experiment, versions, `submission_candidate` with reason; `ideation` → 2–5 proposed Task ids (must exist and be `proposed`); `maintenance` → commit and summary, plus `capability` name when it is a repair (then `capabilities` re-check is scheduled by the watcher).
- `propose_submission`: records a `task_messages` row kind `message` with `[submission proposal] V-<n>` and an event `task.proposed_submission` (handled by a management wake).
- `abandon` from the Orchestrator may act on a held Task: it also stops nothing itself (the watcher stops the run on the `task.abandoned` event).
- `message` (Orchestrator → Researcher) records a pending message; the watcher delivers by `steer` or on resume (entry 5).
- [ ] Tests: every action's happy path on a temp git repo; `complete` names missing checklist items; `claim` from a second session fails; `ideation` completion with 1 proposed Task is refused ("2–5").
- [ ] Commit: `Add the task tool`.

### Task 3.4: `claim` tool and evidence inspectors

**Files:** `tools/claim.py`, `store/evidence_types.py` (inspectors), `gitops.py` (`show_lines(repo, path, a, b, sha)`, `lines_changed_since(repo, path, a, b, sha) -> bool` lifted from the old `renew_code_citations` idea), `core/tests/tools/test_claim_tool.py`.

Actions (both roles): `search`, `read`, `write`, `add_evidence`, `retire`, `inspect_evidence`.
`inspect_evidence` dispatches to the evidence type's inspector: `code` shows the lines at the sha
and whether they changed since (`current: true|false`); `claim`/`experiment`/`version` show the
record summary; `transcript` reads Hermes `state.db` (`<profile>/state.db`, read-only) and verifies
the quote hash; `measurement` shows the stored output (pins); `doc` returns the stored excerpt;
`replay` returns the pin's download data and points to the mixin's replay skill.

- [ ] Tests: write with code evidence on a temp repo; inspect reports `current` false after the lines change; transcript inspector against a fixture `state.db` with the Hermes schema subset (`sessions`, `messages`).
- [ ] Commit: `Add the claim tool and evidence inspectors`.

### Task 3.5: `experiment` tool (store side; `close` computes the verdict in entry 4)

**Files:** `tools/experiment.py`, `core/tests/tools/test_experiment_tool.py`.

Actions (both roles): `create`, `read`, `search`, `update`, `start`, `add_run`, `close`, `abandon`.
`close` in this entry accepts an explicit `verdict` and `measurements`; entry 4 adds the computed
path (`decision_rule.evaluate` over ingested runs) and keeps the explicit path for the
Orchestrator's judgement with a `reason`.

- [ ] Tests: lifecycle through the tool; `start` refusal lists missing parts.
- [ ] Commit: `Add the experiment tool`.

### Task 3.6: `settings`, `workspace` (store-side actions), `ui` tools

**Files:** `tools/settings.py`, `tools/workspace.py`, `tools/ui.py`, `core/tests/tools/test_settings_tool.py`, `test_workspace_tool.py`, `test_ui_tool.py`.

- `settings`: `read` (both), `update` (Orchestrator; `cites` required; when the key is a preference it also suggests writing a preference claim in the result's `next`).
- `workspace` actions: `doctor`, `capabilities`, `environment_guide` (both); `coworld_switch`, `worktree_open`, `change_complete` (Orchestrator). This entry implements `doctor` (image/source commits from `<profile>/.softmax-runtime.json` if present and `git -C core rev-parse`, watcher heartbeat from the workspace store `meta`, store schema status for both stores, capability status, CLI versions via `importlib.metadata` in the workspace env, stale skill overrides: entry 7), `capabilities` (list + `check: true` runs the checks — entry 5 supplies `checks.run_all`; here it lists), `environment_guide` (generated from the runtime config and the last capability check: where you are, image/source commits, writable paths, credentials present (names only, never values), CLIs available, sleep/wake facts, how to reach the user, limits — the B.17 fallback), `worktree_open` (`worktrees/orch-<n>` from main), `change_complete` (records a `maintenance`-like review request: creates an event `change.completed {worktree, commit, summary}` for the review wake). `coworld_switch` arrives in entry 4 (needs the platform and the setup).
- `ui` (Orchestrator): `show {ref|view}` → `ui_state.patch` adding `{"op":"add","path":"/show","value":…}`; `panel_set {panel, kind, content}` → patch `/panels/<panel>`; `notify {text, refs, urgency}` → `notices.add` (and an `events` row kind `notice`); `ask {question, options, refs}` → patch `/asks/<id>` with status `open` and returns `ask_id`, telling the agent "the answer arrives as a UI action; if no client can answer, ask in chat". Results name attached clients that cannot show a kind when capabilities are known (entry 6 stores them); otherwise omit.
- [ ] Tests: each action; `environment_guide` contains the workspace root, the active Coworld, and no credential values; `doctor` reports both stores' schema status.
- [ ] Commit: `Add the settings, workspace and ui tools`.

### Task 3.7: `version` tool (store side) and the `task_types` loader

**Files:** `tools/version.py`, `task_types/__init__.py` (`load(type) -> TaskType(front: dict, body: str)`, parsing front matter without PyYAML: a strict `key: value` / list subset or JSON front matter — use JSON front matter in a fenced block to stay stdlib), `task_types/{research,improvement,ideation,maintenance}.md` (front matter with `required` and `checklist` items `{key, text, pattern, min_items, none_ok}` per B.4; bodies are short placeholders replaced in entry 7), `core/tests/tools/test_version_tool.py`, `core/tests/test_task_types.py`.

- `version`: `read`, `list` (both) here; `create` and `adopt` come in entry 4 (they build and talk to the platform).
- [ ] Tests: task type files parse; checklist evaluation helper `check(type, checklist) -> list[str]` returns the missing items by text.
- [ ] Commit: `Add the version tool reads and the task type loader`.

### Task 3.8: The briefing prompt section

**Files:** `plugin/briefing.py`, `core/tests/plugin/test_briefing.py`.

```python
def render(ws: Workspace, *, role: Role, session_id: str, env, max_chars=4000, clock=None) -> str
```
Contents in priority order (H "Briefing contents"): workspace status (paused, watcher heartbeat
age, credits balance from the last capability check, CLI staleness), session-start capability
facts and broken capabilities with their maintenance Tasks, active directives (workspace + this
session's Goal), champion and submission status of the last 3 Versions, autonomous-submission
window if active, this Session's Task and Experiment (for a task Session: the Task record,
instructions pointer, pending messages), active Goals (title, status, one line of
`where_it_stands`), user-preference claims (tag `preference`), recent claims tagged with active
Goals. Each item is a one-line summary with references. On overflow cut the lowest-priority list
first and append `+N more, search with claim.search`. Also: "User identity: available|not
provisioned" (Orchestrator) and the active Coworld id. **No store path.**

The plugin registers it as `register_system_prompt_section("ide-briefing", callable)`; the
callable reads `info["session_id"]` and calls `render`; any exception renders a one-line
`Briefing unavailable: <error>` instead of raising.

- [ ] Tests: ≤ 4,000 chars with 50 goals and 200 claims (overflow note present); a directive added after the first render appears in the next render (the "survives compaction" guarantee: the section re-renders); no `.state/` in output; task Session shows its Task.
- [ ] Commit: `Add the briefing prompt section`.

### Task 3.9: The advisory hook and the one rule set

**Files:** `plugin/rules.py`, `plugin/advisory.py`, `core/tests/plugin/test_advisory.py`.

```python
# rules.py — ONE rule set for both profiles (C.4): each rule = (pattern: re.Pattern over the command, tool: str, note: str)
RULES = [
  (r"\bcoworld\s+submit\b", "submit", "A raw submit leaves no Version or submission record; use the submit tool (it records intent and the result)."),
  (r"\bcoworld\s+upload-policy\b|\bsoftmax\s+upload\b", "upload", "…use the upload tool …"),
  (r"\bcoworld\s+xp-request\s+create\b", "xp", "…"),
  (r"\bsoftmax\s+(login|logout|set-token|get-token)\b|\bcoworld\s+player\s+(use|unset)\b", "softmax_api", "Identity is fixed for this workspace; …"),
  (r"git\s+push\b.*(--force|-f\b|\+)", "task", "Force-pushing the shared branch loses other Sessions' work; …"),
  (r"\.state/|store\.db|sqlite3\b", "goal/task/claim/experiment", "Records are changed through the record tools; direct edits are detected and reported."),
]
def notes_for(tool_name: str, args: dict) -> list[str]   # terminal/execute_code/file tools: inspect args["command"] / args["code"] / args["path"]
# advisory.py
def transform_tool_result(tool_name="", args=None, result=None, **kw) -> str | None   # append "\n\n[note] …" lines when notes_for is non-empty and the broken-capability hint when that tool's capability is broken (reads capabilities from the store, best effort)
def post_tool_call(tool_name="", args=None, result=None, session_id="", **kw) -> None   # last_error_seen: if result parses as JSON with "error" or a terminal exit code ≠ 0, write tasks.set_last_error for the session's held Task (sessions.read → task_id)
```

- [ ] Tests: each rule fires on a realistic command and not on near misses (`git push origin task/3` has no note; `coworld submit --help` does get a note — acceptable, by design); file tool on `.state/store.db` gets the note; `post_tool_call` records the last error on the held Task; both hooks never raise (exceptions swallowed and logged).
- [ ] Commit: `Add the advisory hook and the shared rule set`.

### Task 3.10: Session-context section and the chat-turn hook (Orchestrator)

**Files:** `plugin/session_context.py`, `plugin/turn.py`, `contract/context.schema.json` (seam v1), `core/tests/plugin/test_session_context.py`, `core/tests/plugin/test_turn.py`.

- `session_context.render(ws, session_id) -> str` (≤ 4,000): the session context recorded at session-open (entry 6 stores it: `sessions.session_context`), the attached clients' capabilities, and the fixed paragraph: "mode and command outrank your judgement on how to handle requests; only the user's explicit request outranks them; any field may be absent." Registered as `register_system_prompt_section("ide-session-context", callable)` for the Orchestrator only.
- `turn.pre_llm_call(**kw) -> dict | None` (Orchestrator chat Sessions only — a Session whose `kind` is `chat` or unknown): returns `{"context": text}` with (1) the labeled context block validated against `context.schema.json` if the user message contains one (`Context v1:` line followed by JSON; unknown fields dropped, malformed fields listed in one line, oversize refused with the limit named — never crashes), (2) unread notices (then `notices.mark_read` on the next committed write — simpler: mark read when rendered, with the ids in the event payload), (3) a "changed since your last turn" note from `events` after the last `turn.rendered` event of this session, (4) the active Coworld id and a line when the client's `coworld` differs, (5) the away summary when the last user turn in any conversation is ≥ 1 hour old (first turn of a new chat only): finished Tasks, submissions, decisions needed since then. Writes an event `turn.rendered {session, seq}`.
- `context.schema.json` v1 (seam subset of A.3/A.4): `contract_version` (int, required), `client {id, kind}`, `mode {name ≤40, instruction ≤1000}`, `command {same}`, `league`, `policy {id, version_id}`, `view {id,label,description}`, `selection`, `references[]`, `on_screen[]` (≤20), `replay_moment`, `voice`, `client_data` (≤ 8 KB), `coworld`; total ≤ 16 KB. Validation is hand-written stdlib (no jsonschema in Hermes's interpreter): a small validator module `contract/validate.py` that reads the schema file's `properties`, types, `maxLength`, `maxItems` and enforces them; the JSON Schema file remains the published contract.
- [ ] Tests: `test_context_block_bad_input_never_crashes` (garbage JSON, oversize `client_data`, a 2,000-char mode instruction → one-line errors, valid fields kept); notices appear once; changed-since note lists event kinds since the last turn; away summary appears only after ≥ 1 hour.
- [ ] Commit: `Add the session-context section and the chat-turn hook`.

### Task 3.11: Plugin manifests, budget and registration tests

**Files:** `tools/build_prompts.py` (first piece: `--manifests` rewrites `provides_tools` in both `plugin.yaml` files from the catalog), `core/tests/test_tool_budget.py`, `core/tests/hermes/test_plugin_loads.py`.

- [ ] `test_tool_budget`: `len(tools_for("orchestrator")) == 18` and `len(tools_for("researcher")) == 14` once entries 4 and 7 land — until then assert `<= 25` and that the current sets are subsets of the C.2 lists; `provides_tools` in each `plugin.yaml` equals the catalog (run `--manifests --check`).
- [ ] `test_plugin_loads` (hermes-marked): install the profile into a temp home, run `prepare.py`, then `hermes -p orchestrator tools list` (or the equivalent `hermes plugins list`/`hermes tools` command that prints registered tools; find the exact one in `hermes_cli` and record it in the test) and assert the catalog's tool names appear and the plugin shows no load error.
- [ ] Commit: `Keep plugin manifests in sync with the tool catalog`.
