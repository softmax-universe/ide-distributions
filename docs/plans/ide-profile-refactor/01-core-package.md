# Entry 1 — teardown, the shared package, the workspace environment, profile shells, prepare v1, CI

> **Implemented.** Status as of November 12, 2026: built and merged in PR #106 on October 29,
> 2026 (Pacific). The checkboxes were not kept up to date; the plan is historical. Since then
> `play/` was removed (#199) and both profiles' models changed (#237; see `config.yaml` in each
> profile). See [STATUS.md](STATUS.md), `README.md` and `core/README.md` for the current state.

Read `00-overview.md` first. This entry turns the repository into the new shape with no record or
tool logic yet: after it, `uv run pytest` passes with a handful of structural tests, both profiles
install into a Hermes home, their plugins load (registering zero tools), and `prepare.py` creates
the workspace environment.

**Interfaces produced (used by every later entry):**
- `research_core.layout.Workspace` and `Coworld` (paths), `research_core.layout.from_env()`.
- `research_core.plugin.register.make_register(role) -> Callable[[ctx], None]`, with the tool
  catalog empty until entry 3.
- `research_core.cli:main` as the `research` console script with subcommand dispatch
  (`research --help` works; subcommands arrive in later entries).
- `research_core.prepare.prepare(source: Path, hermes: str, activate_watcher: bool) -> None`.
- Root `pyproject.toml` (workspace env project) and `core/pyproject.toml`.

---

### Task 1.1: Remove the old distributions and shared code

**Files:**
- Delete: `orchestrator/` (whole folder), `researcher/` (whole folder), `shared/`, `ontology/`
  (after Task 1.2 copies `ontology/VOCABULARY.md`), `tools/sync_shared.py`,
  `docs/coworld-context.md`, `.gitattributes`.
- Modify: `play/pyproject.toml:33` comment, `play/README.md:18-22,73-76`,
  `play/src/ide_research_core/GENERATED.md` (rewrite as "This folder is play's own vendored copy
  of the former shared `ide_research_core` package. It is no longer generated; edit it here.").
- Keep: `play/` otherwise untouched; `docs/designs/`, `docs/reports/`.

- [ ] **Step 1:** `git rm -r orchestrator researcher shared tools/sync_shared.py docs/coworld-context.md .gitattributes` (move `ontology/VOCABULARY.md` first, Task 1.2 Step 1, then `git rm -r ontology`).
- [ ] **Step 2:** Edit the three `play/` texts named above so nothing in `play/` mentions `tools/sync_shared.py` or `shared/`. Run `grep -rn "sync_shared\|shared/ide_research_core" play` → no output.
- [ ] **Step 3:** `cd play && uv sync --frozen && uv run pytest -q` → passes (play is untouched functionally). Record the result in the commit message.
- [ ] **Step 4:** Commit: `Remove the old orchestrator, researcher and shared code for the profile refactor`.

### Task 1.2: The `core` package skeleton and the workspace environment project

**Files:**
- Create: `core/pyproject.toml`, `core/src/research_core/__init__.py`, `core/src/research_core/py.typed`,
  `core/src/research_core/prompts/vocabulary.md` (moved from `ontology/VOCABULARY.md`),
  `core/tests/conftest.py`, `core/tests/test_package.py`, `pyproject.toml` (root), `.python-version` (`3.12`),
  `tools/refresh_vocabulary.py` (modify output path to the new location).

`core/pyproject.toml`:

```toml
[project]
name = "research-core"
version = "0.1.0"
description = "Shared code of the Softmax IDE research profiles: store, tools, plugin, watcher, CLI"
requires-python = ">=3.12"
dependencies = []                      # the base is stdlib only (the plugin imports it inside Hermes)

[project.optional-dependencies]
platform = [
    "coworld>=0.1.66,<0.2",
    "softmax-cli>=0.26.40,<0.27",
    "PyYAML>=6",
    "numpy>=1.26",
]

[project.scripts]
research = "research_core.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/research_core"]
```

Root `pyproject.toml` (the workspace environment):

```toml
[project]
name = "ide-workspace-environment"
version = "0.1.0"
description = "The Python environment the IDE profiles' tools, CLI and watcher run in"
requires-python = ">=3.12,<3.13"
dependencies = ["research-core[platform]"]

[tool.uv.sources]
research-core = { path = "core", editable = true }

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6"]

[tool.pytest.ini_options]
testpaths = ["core/tests"]
markers = ["hermes: needs a real Hermes install (HERMES_BIN); skipped by default"]
addopts = "-m 'not hermes'"

[tool.ruff]
line-length = 100
target-version = "py312"
extend-exclude = ["play"]
```

- [ ] **Step 1:** `git mv ontology/VOCABULARY.md core/src/research_core/prompts/vocabulary.md`; then `git rm -r ontology`. Update `tools/refresh_vocabulary.py` so its output path is the new file (read the script; change only the path constant and its docstring).
- [ ] **Step 2:** Write the two `pyproject.toml` files above, `.python-version`, `core/src/research_core/__init__.py` with `__version__ = "0.1.0"`.
- [ ] **Step 3:** Write the failing test `core/tests/test_package.py`:
  ```python
  def test_version():
      import research_core
      assert research_core.__version__ == "0.1.0"
  ```
- [ ] **Step 4:** `uv lock && uv sync` from the repo root (Python 3.12 is installed via uv; `coworld 0.1.66` resolves). `uv run pytest -q` → 1 passed. `uv run ruff check .` → clean.
- [ ] **Step 5:** Commit: `Add the research-core package and the workspace environment project`.

### Task 1.3: `layout.py` — the workspace directory model

**Files:** Create `core/src/research_core/layout.py`, `core/tests/test_layout.py`.

**Interfaces (produces):**

```python
@dataclass(frozen=True)
class Coworld:
    coworld_id: str                   # platform id, e.g. "cow_abc-123"
    root: Path                        # <workspace>/<folder>
    @property
    def folder_name(self) -> str      # id with "-" replaced by "_" (B.11); must be a valid Python identifier tail
    code: Path; source: Path; worktrees: Path; documents: Path; state: Path; store_path: Path; mixin: Path  # code/mixin
    def worktree_for_task(self, task_id: int) -> Path     # worktrees/task-<n>
    def worktree_for_change(self, n: int) -> Path          # worktrees/orch-<n>

@dataclass(frozen=True)
class Workspace:
    root: Path
    tools: Path; venv: Path; venv_python: Path; state: Path; store_path: Path
    def coworld(self, coworld_id: str) -> Coworld
    def ensure_dirs(self) -> None     # mkdir tools/, .state/ (no store creation here)

def from_env(env: Mapping[str, str] = os.environ) -> Workspace
# IDE_WORKSPACE, else $HERMES_REAL_HOME/workspace, else ~/.hermes/workspace; venv = IDE_WORKSPACE_ENV or root/tools/venv
def folder_name_for(coworld_id: str) -> str   # raises ValueError on characters other than [A-Za-z0-9_-]
```

- [ ] **Step 1:** Tests: `test_folder_name_replaces_hyphens` (`cow_a-b` → `cow_a_b`), `test_folder_name_rejects_slash`, `test_from_env_prefers_ide_workspace`, `test_from_env_falls_back_to_hermes_real_home`, `test_coworld_paths` (every path is under `root/<folder>`), `test_venv_override`.
- [ ] **Step 2:** Run → fail (module missing).
- [ ] **Step 3:** Implement `layout.py` (stdlib only).
- [ ] **Step 4:** Run → pass. Commit: `Add the workspace directory model`.

### Task 1.4: The `research` CLI skeleton and the plugin registration skeleton

**Files:** Create `core/src/research_core/cli.py`, `core/src/research_core/plugin/__init__.py`,
`core/src/research_core/plugin/register.py`, `core/src/research_core/tools/__init__.py`,
`core/src/research_core/tools/catalog.py`, `core/tests/test_cli.py`, `core/tests/test_plugin_register.py`,
`core/tests/test_stdlib_only.py`.

**Interfaces:**

```python
# cli.py
def main(argv: list[str] | None = None) -> int
# argparse with subparsers; each later entry adds a module exposing `add_parser(subparsers)`;
# the registry is COMMANDS: list[Callable[[argparse._SubParsersAction], None]] in cli.py.
# "research --version" prints the version; unknown command exits 2 with usage.

# tools/catalog.py
Role = Literal["orchestrator", "researcher"]
def tools_for(role: Role) -> list["ToolSpec"]        # empty until entry 3
TOOL_BUDGET = 25

# plugin/register.py
def make_register(role: Role) -> Callable[[Any], None]
# register(ctx): for spec in tools_for(role): ctx.register_tool(name=spec.name, toolset=f"ide-{role}",
#   schema=spec.schema(), handler=make_handler(spec, role), description=spec.description)
# plus hooks and prompt sections added in entry 3. Must finish in < 1 s and import nothing heavy.
```

- [ ] **Step 1:** Tests: `test_cli_version` (`main(["--version"])` prints `research 0.1.0`), `test_cli_unknown_command_exits_2`; `test_make_register_registers_nothing_yet` with a fake `ctx` that records calls; `test_stdlib_only`: run `sys.executable -c "import sys; sys.path.insert(0, 'core/src'); import research_core.plugin.register, research_core.layout; third=[m for m in sys.modules if m.split('.')[0] in {'yaml','numpy','coworld','softmax_client','httpx','pydantic'}]; assert not third"` in a subprocess with `-I` → exit 0.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Run → pass.
- [ ] **Step 5:** Commit: `Add the research CLI and plugin registration skeletons`.

### Task 1.5: The two profile shells

**Files:**
- Create `orchestrator/distribution.yaml`, `orchestrator/config.yaml`, `orchestrator/SOUL.md` (placeholder: one line "Generated by tools/build_prompts.py; see entry 7." — replaced in entry 7), `orchestrator/README.md`, `orchestrator/.no-bundled-skills`, `orchestrator/plugins/orchestrator/plugin.yaml`, `orchestrator/plugins/orchestrator/__init__.py`, `orchestrator/scripts/prepare.py`, `orchestrator/cron/jobs.json`, `orchestrator/scripts/watcher.py` (stub that exits 0 printing `{"wakeAgent": false}` until entry 5).
- Create the same for `researcher/` without `cron/` and `watcher.py`; plugin dir `researcher/plugins/researcher/`.
- Create `core/tests/test_profiles.py`.

`distribution.yaml` (Orchestrator; Researcher differs in `name`, `description`, and omits `SOFTMAX_USER_CREDENTIAL`):

```yaml
name: orchestrator
version: 1.0.0
description: The research lead of the Softmax IDE. Speaks to the user, manages Goals and Tasks, submits, runs the watcher.
hermes_requires: ">=0.21.6"
env_requires:
  - {name: OPENROUTER_API_KEY, required: true, description: OpenRouter API key for the model.}
  - {name: OPENROUTER_MODEL, required: false, default: moonshotai/kimi-k3, description: Model id on OpenRouter.}
  - {name: SOFTMAX_PLAYER_ID, required: true, description: The Player this workspace acts as.}
  - {name: SOFTMAX_PLAYER_CREDENTIAL, required: true, description: That Player's agent credential (cga_ token) or a player-session JSON.}
  - {name: SOFTMAX_USER_CREDENTIAL, required: false, description: The user's User identity token (Orchestrator only). Without it, User-identity reads and writes are unavailable and the briefing says so.}
  - {name: COGAMES_API_URL, required: false, default: https://softmax.com/api, description: Softmax API server.}
  - {name: IDE_WORKSPACE, required: true, description: Absolute path of the one workspace root shared by every profile of this sandbox.}
  - {name: IDE_RESEARCHER_PROFILES, required: false, default: researcher, description: Comma-separated Hermes profile names that run Researcher Sessions.}
  - {name: IDE_PLATFORM, required: false, default: real, description: "real" or "fake" (tests).}
```

`config.yaml` (Orchestrator; the Researcher omits `compression`, `SOFTMAX_USER_CREDENTIAL`, and uses `ide-researcher`):

```yaml
# Shipped defaults. prepare.py re-applies the keys the machinery relies on at every start
# (see research_core/prepare.py PROTECTED_KEYS) and removes retired keys; other values the
# owner changes are kept across `hermes profile update`.
model:
  default: moonshotai/kimi-k3
  provider: openrouter
fallback_providers:
  - provider: openrouter
    model: z-ai/glm-5.3
memory:
  memory_enabled: false
  user_profile_enabled: false
approvals:
  mode: "off"
  cron_mode: approve
  single_query_mode: approve
  unattended_mode: approve
compression:
  protect_first_n: 6
  protect_last_n: 30
  threshold: 0.6
  target_ratio: 0.3
platform_toolsets:
  api_server: [hermes-api-server, ide-orchestrator]
  cron: []
known_plugin_toolsets:
  cron: [ide-orchestrator]
plugins:
  enabled: [orchestrator]
tools:
  tool_search:
    enabled: "off"
cron:
  catch_up_missed: true
terminal:
  timeout: 1800
  env_passthrough:
    - SSL_CERT_FILE
    - SOFTMAX_PLAYER_ID
    - SOFTMAX_PLAYER_CREDENTIAL
    - SOFTMAX_USER_CREDENTIAL
    - COGAMES_API_URL
    - SOFTMAX_CONFIG_DIR
    - IDE_WORKSPACE
    - IDE_WORKSPACE_ENV
    - IDE_PLATFORM
    - IDE_FAKE_FAULTS
    - IDE_RESEARCHER_PROFILES
    - OPENROUTER_MODEL
    - TMPDIR
skills:
  external_dirs: []        # prepare.py fills: <core>/skills/common, <core>/skills/<role>, <active coworld>/code/mixin/skills
  trusted_project_dirs: [] # prepare.py adds the active Coworld's code repository
updates:
  check: false
curator:
  prune_builtins: false
```

`plugins/orchestrator/plugin.yaml`:

```yaml
name: orchestrator
version: 1.0.0
description: Record, platform and ui tools of the IDE Orchestrator, its briefing and advisory hook.
author: ide-distributions
kind: standalone
requires_hermes: ">=0.21.6"
provides_tools: []        # filled by tools/build_prompts.py --manifests in entry 3 (kept in sync by a test)
provides_hooks: [transform_tool_result, post_tool_call, pre_llm_call]
```

`plugins/orchestrator/__init__.py` (STDLIB ONLY; identical for the researcher except the role):

```python
"""Hermes plugin entry. Runs inside the Hermes process: stdlib only.

prepare.py records the release's core source path in local/core-path.txt; this shim puts it on
sys.path and delegates to research_core. If research_core is already importable (installed into
Hermes's interpreter), that copy wins only when it reports the same version."""
import os, sys
from pathlib import Path

def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or Path(__file__).resolve().parents[2])

def _add_core_path(home: Path) -> None:
    marker = home / "local/core-path.txt"
    if marker.exists():
        path = marker.read_text().strip()
        if path and path not in sys.path:
            sys.path.insert(0, path)

_add_core_path(_home())
from research_core.plugin.register import make_register  # noqa: E402
register = make_register("orchestrator")
```

`scripts/prepare.py` (STDLIB ONLY, run by the host with Hermes's Python):

```python
"""Prepare this profile after install or update. Usage: prepare.py --source <folder> --hermes <exe> [--activate-supervisor]
Loads research_core from <source>/../core/src (the release checkout) so an agent's edits cannot stop preparation."""
import argparse, sys
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--source", required=True, type=Path)
parser.add_argument("--hermes", required=True)
parser.add_argument("--activate-supervisor", action="store_true")   # hosting's flag name; enables the watcher once
args = parser.parse_args()
core_src = (args.source.resolve().parent / "core/src")
sys.path.insert(0, str(core_src))
from research_core.prepare import prepare  # noqa: E402
prepare(source=args.source.resolve(), hermes=args.hermes, activate_watcher=args.activate_supervisor)
```

`cron/jobs.json` (Orchestrator): one job `{"id": "watcher", "name": "watcher", "prompt": "", "script": "watcher.py", "no_agent": true, "schedule": {"kind": "interval", "minutes": 1, "display": "every 1m"}, "schedule_display": "every 1m", "repeat": {"times": null, "completed": 0}, "enabled": false, "state": "paused", "paused_reason": "Installed from a profile distribution; prepare.py enables it.", "deliver": "local", "skills": [], "enabled_toolsets": []}` wrapped as `{"jobs": [...]}`.

- [ ] **Step 1:** Tests in `core/tests/test_profiles.py`: `test_distribution_manifests_parse` (both YAML files load — use a tiny stdlib YAML subset reader? No: tests run in the workspace env where PyYAML exists; use `yaml.safe_load`), `test_config_has_protected_keys` (approvals off, memory off, tool_search off, plugin enabled, toolset listed), `test_plugin_shim_is_stdlib_only` (AST-walk the shim: every import is stdlib or `research_core`/`hermes_constants`), `test_no_approvals_deny_list`, `test_cron_job_shape` (fields of A.2; `no_agent` with `script`).
- [ ] **Step 2:** Write the files. Run tests → pass. `uv run ruff check .` clean.
- [ ] **Step 3 (hermes-marked):** `core/tests/hermes/test_profile_install.py`: if `HERMES_BIN` is set, in a temp `HERMES_HOME` run `hermes profile install <repo>/orchestrator --name orchestrator --yes` and the same for `researcher`; assert the profile dirs exist with `config.yaml`, `plugins/`, `scripts/`; assert `cron/jobs.json` holds the `watcher` job paused. (Locally: `HERMES_BIN=<scratchpad>/hermes-venv/bin/hermes uv run pytest -m hermes core/tests/hermes/test_profile_install.py`.)
- [ ] **Step 4:** Commit: `Add the orchestrator and researcher profile shells`.

### Task 1.6: `prepare.py` v1 — workspace environment, core path marker, protected config keys, watcher job

**Files:** Create `core/src/research_core/prepare.py`, `core/src/research_core/locks.py` (file lock with timeout; lifted from the old `io/locks.py`: `fcntl.flock` LOCK_EX|LOCK_NB polled every 50 ms, default timeout 60 s, `LockTimeout(TimeoutError)`), `core/tests/test_prepare.py`, `core/tests/test_locks.py`.

**Interfaces:**

```python
PROTECTED_KEYS: dict[str, Any] = {
    "approvals.mode": "off", "approvals.cron_mode": "approve", "approvals.single_query_mode": "approve",
    "approvals.unattended_mode": "approve", "memory.memory_enabled": False, "memory.user_profile_enabled": False,
    "tools.tool_search.enabled": "off", "cron.catch_up_missed": True, "plugins.enabled": ["<role>"],
    "platform_toolsets.api_server": ["hermes-api-server", "ide-<role>"], "known_plugin_toolsets.cron": ["ide-<role>"],
}
RETIRED_KEYS = ("approvals.deny", "agent.max_turns", "agent.run_budget_seconds", "timeouts", "kanban",
                "skills.external_dirs[../../games]")   # the last means: drop that list element if present
def prepare(source: Path, hermes: str, activate_watcher: bool, env: Mapping[str, str] = os.environ, run=subprocess.run) -> None
# 1. role = source.name ("orchestrator" | "researcher"); profile = Path(env["HERMES_HOME"]); core_src = source.parent / "core/src"
# 2. write profile/local/core-path.txt = str(core_src)
# 3. ws = layout.from_env(env); ws.ensure_dirs(); ensure_workspace_env(source.parent, ws, run)  (Task 1.6a)
# 4. config: read profile/config.yaml via `hermes config get/set/unset --json` (as the old prepare did) — set every
#    PROTECTED_KEY (role-substituted), unset every RETIRED_KEY, set skills.external_dirs to
#    [core_src/research_core/skills/common, core_src/research_core/skills/<role>] + the active Coworld's mixin skills dir
#    if one is registered (entry 4 fills the registry; for now just the two), and skills.trusted_project_dirs to the active
#    Coworld code dir if any.
# 5. Orchestrator only: reconcile cron/jobs.json: ensure the "watcher" job exists with the shipped script/no_agent/schedule
#    fields (keep pause state, history, other jobs); if activate_watcher and profile/.softmax-prepared is absent:
#    run `hermes cron resume watcher` (via the `hermes` exe with HERMES_HOME) then touch the marker.
# 6. write profile/.softmax-prepared; print a one-line JSON summary {"role", "workspace", "venv", "actions": [...]}.
def ensure_workspace_env(checkout: Path, ws: Workspace, run=subprocess.run) -> bool
# If IDE_WORKSPACE_ENV is set (ws.venv is that path): verify ws.venv/.lock-sha == sha256(checkout/uv.lock) else raise
# PrepareError naming both; return False. Else under locks.file_lock(ws.tools/"venv.lock"): if stamp matches return False;
# run ["uv","sync","--frozen","--no-dev","--project",str(checkout)] with UV_PROJECT_ENVIRONMENT=ws.venv; write stamp; True.
```

Use `hermes config set <key> <json>` for structured values; a string value goes verbatim (Hermes stores a string setting's text). Where `hermes config` lacks list-element removal, read the YAML (PyYAML is available in the workspace env? **No** — prepare runs under Hermes's Python; use `hermes config get <key> --json` and `hermes config set`, both exist in v0.21.6; never import yaml here).

- [ ] **Step 1:** Tests with a fake `run` that records argv and a fake `hermes` shim script answering `config get` from an in-memory dict: `test_prepare_writes_core_path`, `test_prepare_sets_protected_keys_and_unsets_retired`, `test_prepare_reconciles_watcher_job_keeps_pause_state` (an existing jobs.json with the watcher paused and a foreign job stays paused and keeps the foreign job; a missing watcher job is added paused), `test_prepare_activates_watcher_once` (resume called only when the marker is absent), `test_ensure_workspace_env_syncs_when_stamp_differs`, `test_ensure_workspace_env_prebuilt_mismatch_raises`, `test_locks_timeout`.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Run → pass.
- [ ] **Step 5 (hermes-marked):** `core/tests/hermes/test_prepare_real.py`: install the profile into a temp home, run `scripts/prepare.py --source <repo>/orchestrator --hermes $HERMES_BIN` with `HERMES_HOME`, `HERMES_REAL_HOME`, `IDE_WORKSPACE` set to temp dirs; assert `hermes -p … config get approvals.mode --json` is `"off"`, `local/core-path.txt` exists, the venv python exists and `python -c "import research_core, coworld"` succeeds in it (allow ~2 min), the watcher job is enabled when `--activate-supervisor` was passed.
- [ ] **Step 6:** Commit: `Add profile preparation: workspace environment, core path, protected config, watcher job`.

### Task 1.7: CI for the new layout

**Files:** Rewrite `.github/workflows/ci.yml`; modify `.github/workflows/release-hermes.yml` paths
(add `core/**`, `pyproject.toml`, `uv.lock`); create `.github/workflows/lock-refresh.yml` (entry 7
fills the auto-merge detail; create the scheduled job now: daily 11:00 UTC, `uv lock
--upgrade-package coworld --upgrade-package softmax-cli`, open a PR with `peter-evans/create-pull-request@v7`).

`ci.yml` jobs:
- `changes` (pull requests): filters `core` = everything except `docs/**`, `README.md`, `play/**`, release workflows; `play` = `play/**` + workflows.
- `core`: container `nousresearch/hermes-agent@sha256:9774f4f39a9bb8c2f68ce728ed5e99ddbad282163be56764afacf88ed952b784` (the hosted Hermes), `astral-sh/setup-uv@v6` with python 3.12, `uv sync --frozen`, `uv run ruff check .`, `uv run pytest -q`, then `HERMES_BIN=/opt/hermes/.venv/bin/hermes uv run pytest -q -m hermes core/tests/hermes` (the real-Hermes tests; the gateway/model spike is excluded by its own skip unless `OPENROUTER_API_KEY` is set). Also `python tools/build_prompts.py --check` once entry 7 exists (add the step then).
- `play`: unchanged from today's play job (uv sync, ruff, pytest, the `-m hermes` registration test).

- [ ] **Step 1:** Write the workflows. Validate YAML with `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` (workspace env has PyYAML).
- [ ] **Step 2:** Commit: `Rewrite CI for the core package and the two profile shells`.

### Task 1.8: Root README (interim) and docs pointers

**Files:** Rewrite `README.md` (interim: what the folders are, how to install both profiles, how to run tests; mark "being rebuilt per the design; see docs/plans/ide-profile-refactor/"); update `docs/hosting.md` with an "in rework" banner pointing at the overview (full rewrite in entry 7).

- [ ] **Step 1:** Write both. **Step 2:** Commit: `Describe the new repository layout`.
