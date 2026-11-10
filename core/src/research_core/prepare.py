"""Profile preparation, run by the host after ``hermes profile install`` or ``update``.

``<profile>/scripts/prepare.py`` loads this module from the release checkout and calls
``prepare``. It runs under Hermes's own interpreter, using its installed configuration API
without starting a CLI process for each key. The cron store is edited as JSON.
In order, ``prepare``:

1. records the core source path in ``<profile>/local/core-path.txt`` for the plugin shim;
2. creates the workspace directories and creates or migrates the workspace store and every
   registered Coworld's store (``ensure_stores``);
3. re-applies the protected config keys, removes the retired ones, points the profile at the
   shared skills, keeps profile cwd at the neutral workspace root, and applies the
   shipped values over earlier defaults only (``SHIPPED_VALUES``, ``EARLIER_DEFAULTS``) and
   unions the shipped ``terminal.env_passthrough`` entries with the owner's. A new install
   takes the model settings (``model``, ``fallback_providers``) from the shipped
   ``config.yaml`` and the bridge's ``/models`` route changes them; the one exception is
   ``migrate_superseded_model``. It adds the shipped OpenRouter host pins
   (``PROVIDER_ROUTING_MODELS``) the owner's routing does not already decide, so the two
   shipped Kimi models go only to Moonshot's own endpoint;
4. (Orchestrator) removes the retired observation cron; research serve owns scheduling;
5. moves open sessions onto this release's tools and instructions when those changed
   (``frozen_sessions``);
6. builds or checks the workspace environment under a file lock;
7. writes ``<profile>/.softmax-prepared`` and prints one JSON summary line.

``check=True`` (the script's ``--check``) plans steps 3 and 5 against the live config and
writes nothing. ``apply_skill_dirs`` is the same profile-pointing step for every profile of
the sandbox, which a Coworld switch calls after preparation.

Config comes before the (slow, network-bound) environment build so that a failed or timed-out
``uv sync`` still leaves the profile's gates in their protected state.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from research_core import layout
from research_core.frozen_sessions import STATE_DB, refresh_frozen_sessions
from research_core.layout import Workspace
from research_core.locks import file_lock
from research_core.store import coworlds
from research_core.store.actors import Actor
from research_core.store.db import Store

ROLES = ("orchestrator", "researcher")
ROLE_PLACEHOLDER = "<role>"
WATCHER_JOB_ID = "watcher"
PREPARED_MARKER = ".softmax-prepared"
CORE_PATH_FILE = Path("local") / "core-path.txt"
LOCK_STAMP = ".lock-sha"

# Config keys the profile machinery relies on, re-applied at every preparation so that an edit
# by the owner or an agent never switches a gate back on. ``<role>`` is substituted.
PROTECTED_KEYS: dict[str, Any] = {
    "attachments.storage": "workspace",
    "approvals.mode": "off",
    "approvals.cron_mode": "approve",
    "approvals.single_query_mode": "approve",
    "approvals.unattended_mode": "approve",
    "memory.memory_enabled": False,
    "memory.user_profile_enabled": False,
    "tools.tool_search.enabled": "off",
    # Hermes's post-turn self-improvement review patches skills in skills.external_dirs: the
    # shipped skills are release content, not a notebook.
    "auxiliary.background_review.enabled": False,
    # Hermes's auxiliary tasks default to the main model ("auto"). Titles and image reading are
    # commodity work for a small model: a title on Kimi K3.1 Pro costs about 50 times what it does
    # on Haiku, and the GLM fallback reads no images. Compression and the rest stay on the main
    # model, so they follow its fallback chain. Session search and web extraction call no model
    # in this Hermes.
    "auxiliary.title_generation.provider": "openrouter",
    "auxiliary.title_generation.model": "anthropic/claude-haiku-5.5",
    "auxiliary.vision.provider": "openrouter",
    "auxiliary.vision.model": "anthropic/claude-haiku-5.5",
    "cron.catch_up_missed": True,
    "plugins.enabled": ["<role>"],
    # The AIDE backend shows reasoning while it streams only through this hook
    # (plugin/reasoning_relay.py).
    "plugins.stream_reasoning_deltas": True,
    "platform_toolsets.api_server": ["hermes-api-server", "ide-<role>"],
    "known_plugin_toolsets.cron": ["ide-<role>"],
    # Hermes's loop guard only warns on api_server; a wake that repeated one refused call 185
    # times (2026-11-02). The hard stop (Hermes v0.22.6) halts after 5 failures of one tool,
    # blocks after 5 identical failing calls (Hermes's default) and halts after 10 identical
    # calls with identical results or 10 laps of an identical cycle, any tool but pollers;
    # that last limit is raised from Hermes's 5 so polling a job with `terminal` is not stopped.
    "tool_loop_guardrails.hard_stop_enabled": True,
    "tool_loop_guardrails.hard_stop_after.same_tool_failure": 5,
    "tool_loop_guardrails.hard_stop_after.idempotent_no_progress": 10,
    # Hermes tools these agents never called, each sent on every model call (JAME-178). Hermes
    # disables whole toolsets: `browser` also takes browser_vault, browser_cdp and browser_dialog.
    # `todo`: the plan is Goals and Tasks, and no call in 1,981 used it. `delegation` stays: the
    # expert-consultation skill needs it, and a child without a Coworld is told so when it tries.
    # `session_search` (a toolset of one tool): 11 calls in 3,761 model calls (2026-11-07 to
    # 11-09), half of them empty and the rest other Sessions' summaries the records hold; its
    # schema was 4.0k characters on every call.
    "agent.disabled_toolsets": [
        "browser",
        "connections",
        "cronjob",
        "todo",
        "session_search",
    ],
    # Reclaim stale results below Hermes's 8,000-character default cutoff as well. The existing
    # deterministic prune protects recent results and archives originals without a model call.
    "compression.proactive_prune_tokens": 64000,
    "compression.proactive_prune_min_result_chars": 2000,
}

# Orchestrator chats are long: compaction protects more of the head and tail and compacts less
# aggressively, so the user's instructions stay word for word (design F.5).
ORCHESTRATOR_PROTECTED_KEYS: dict[str, Any] = {
    "compression.protect_first_n": 6,
    "compression.protect_last_n": 30,
    "compression.threshold": 0.6,
    "compression.target_ratio": 0.3,
}

# Keys of earlier distributions that must not survive an update. ``key[element]`` means: drop
# that element from the list at ``key`` if it is present.
RETIRED_KEYS: tuple[str, ...] = (
    "approvals.deny",
    "agent.max_turns",
    "agent.run_budget_seconds",
    "timeouts",
    "kanban",
    "skills.external_dirs[../../games]",
    # The model is the profile's own setting, never the host's (the bridge changes it).
    "terminal.env_passthrough[OPENROUTER_MODEL]",
)

# Shipped values that are not protected: a retained value is replaced only when it is unset or
# equals a known earlier default, so a shipped change reaches updated profiles while a value the
# owner chose survives (amendment A7.4).
SHIPPED_VALUES: dict[str, Any] = {
    "terminal.timeout": 1800,
    "updates.check": False,
    "curator.prune_builtins": False,
}
EARLIER_DEFAULTS: dict[str, set[Any]] = {
    "terminal.timeout": {180},
}
# OpenRouter hosts per model id (Hermes's provider_routing.models). Kimi K3 on cheaper
# third-party hosts dropped required tool-call arguments in long sessions (2026-11-02), while
# Moonshot's own endpoint did not, so the Kimi models a profile may run are pinned to Moonshot.
# The pin is routing, not the model choice: it applies only while a profile runs that model.
PROVIDER_ROUTING_MODELS: dict[str, dict[str, Any]] = {
    "moonshotai/kimi-k3.1-pro": {"only": ["moonshotai"]},
    "moonshotai/kimi-k3": {"only": ["moonshotai"]},
}
# Per role, the model an earlier release shipped and the settings that replace it. See
# migrate_superseded_model.
SUPERSEDED_MODELS: dict[str, tuple[str, dict[str, Any]]] = {
    "orchestrator": ("moonshotai/kimi-k3", {"model.default": "moonshotai/kimi-k3.1-pro"}),
    "researcher": (
        "recursive/flint-v1.0:free",
        {
            "model.default": "moonshotai/kimi-k3.1-pro",
            "fallback_providers": [{"provider": "openrouter", "model": "z-ai/glm-5.3"}],
        },
    ),
}
# Lists the shipped config contributes to: the union of the shipped and retained entries is kept.
ENV_PASSTHROUGH: dict[str, list[str]] = {
    "common": [
        "BUILDKIT_HOST",
        "SSL_CERT_FILE",
        "SOFTMAX_PLAYER_ID",
        "SOFTMAX_PLAYER_CREDENTIAL",
        "COGAMES_API_URL",
        "SOFTMAX_CONFIG_DIR",
        "IDE_WORKSPACE",
        "IDE_WORKSPACE_ENV",
        "IDE_PLATFORM",
        "IDE_FAKE_FAULTS",
        "TMPDIR",
    ],
    "orchestrator": ["IDE_RESEARCHER_PROFILES"],
    "researcher": [],
}

Runner = Callable[..., subprocess.CompletedProcess]


class PrepareError(RuntimeError):
    pass


# --- keys -------------------------------------------------------------------------------------


def protected_keys_for(role: str) -> dict[str, Any]:
    """The protected keys of one role with ``<role>`` substituted."""
    keys = dict(PROTECTED_KEYS)
    if role == "orchestrator":
        keys.update(ORCHESTRATOR_PROTECTED_KEYS)
    return {key: _substitute_role(value, role) for key, value in keys.items()}


def _substitute_role(value: Any, role: str) -> Any:
    if isinstance(value, str):
        return value.replace(ROLE_PLACEHOLDER, role)
    if isinstance(value, list):
        return [_substitute_role(item, role) for item in value]
    return value


def plain_retired_keys() -> tuple[str, ...]:
    """The retired keys that are whole keys rather than list elements."""
    return tuple(key for key in RETIRED_KEYS if _split_retired(key)[1] is None)


def _split_retired(key: str) -> tuple[str, str | None]:
    """``"a.b[x]"`` -> ``("a.b", "x")``; a plain key -> ``(key, None)``."""
    if key.endswith("]") and "[" in key:
        name, _, element = key[:-1].partition("[")
        return name, element
    return key, None


def skill_dirs_for(core_src: Path, role: str) -> list[str]:
    """``skills.external_dirs``: the common core skills and the role's own (may not exist yet)."""
    skills = core_src / "research_core" / "skills"
    return [str(skills / "common"), str(skills / role)]


# --- the hermes executable --------------------------------------------------------------------


def _failed(result: subprocess.CompletedProcess) -> PrepareError:
    """A command failure with its full output, exit code and command line (design B.15)."""
    command = " ".join(str(part) for part in result.args)
    return PrepareError(
        f"`{command}` failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _key_not_set(result: subprocess.CompletedProcess) -> bool:
    return "Config key not set" in f"{result.stdout}{result.stderr}"


class HermesCli:
    """``hermes config`` and ``hermes cron`` against one profile home.

    Every call runs with ``HERMES_HOME`` set to the profile so Hermes edits that profile's
    ``config.yaml`` and cron store. Verified against Hermes v0.22.6: ``config get <key> --json``
    prints the resolved value as JSON; ``config set <key> <value>`` keeps a string-typed setting's
    text verbatim and parses a JSON list or mapping literal; ``config unset <key>`` exits 1 with
    "Config key not set" when the user's file has no such key.
    """

    dry_run = False
    planned: list[str]

    def __init__(self, hermes: str, profile: Path, env: Mapping[str, str], run: Runner) -> None:
        self.planned = []
        self.hermes = hermes
        self.env = {**env, "HERMES_HOME": str(profile)}
        self.run = run

    def _call(self, *args: str) -> subprocess.CompletedProcess:
        argv = [self.hermes, *args]
        try:
            return self.run(argv, env=self.env, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise PrepareError(f"hermes executable not found: {self.hermes}") from exc

    def config_get(self, key: str) -> Any:
        """The resolved value of ``key`` (defaults merged), or None when Hermes has no such key."""
        result = self._call("config", "get", key, "--json")
        if result.returncode != 0:
            if _key_not_set(result):
                return None
            raise _failed(result)
        return json.loads(result.stdout)

    def config_set(self, key: str, value: Any) -> None:
        if self.dry_run:
            self.planned.append(f"config: set {key}")
            return
        text = value if isinstance(value, str) else json.dumps(value)
        result = self._call("config", "set", key, text)
        if result.returncode != 0:
            raise _failed(result)

    def config_unset(self, key: str) -> bool:
        if self.dry_run:
            self.planned.append(f"config: unset {key}")
            return True
        """Remove ``key`` from the user's config; False when it was not set."""
        result = self._call("config", "unset", key)
        if result.returncode == 0:
            return True
        if _key_not_set(result):
            return False
        raise _failed(result)


class HermesConfig(HermesCli):
    """Prepare one profile using the installed Hermes configuration API.

    The preparation script already runs in Hermes's interpreter with HERMES_HOME selected.
    Keep its validated setters and file-signature cache, without restarting the CLI per key.
    Coworld switching still uses HermesCli because it runs in the workspace interpreter.
    """

    def __init__(self, hermes: str) -> None:
        from hermes_cli import config

        super().__init__(hermes, Path(os.environ["HERMES_HOME"]), os.environ, subprocess.run)
        self.config = config

    def config_get(self, key: str) -> Any:
        value = self.config.load_config()
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    def config_set(self, key: str, value: Any) -> None:
        if self.dry_run:
            self.planned.append(f"config: set {key}")
            return
        self.config.set_config_value(key, value if isinstance(value, str) else json.dumps(value))

    def config_unset(self, key: str) -> bool:
        if self.dry_run:
            self.planned.append(f"config: unset {key}")
            return True
        value = self.config.require_readable_config_before_write()
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return False
            value = value[part]
        self.config.unset_config_value(key)
        return True


def apply_skill_dirs(
    profile_home: Path,
    hermes: str,
    *,
    env: Mapping[str, str],
    run: Runner = subprocess.run,
) -> dict[str, list[str]]:
    """Point this profile and every Researcher profile at the session's Coworld (A5.9).

    Returns ``{"applied": [...], "problems": [...]}``: what was set, and the profiles that
    could not be configured (a missing home, a failing ``hermes`` call).
    """
    core_src = Path(env["IDE_CORE_SRC"]) if env.get("IDE_CORE_SRC") else _core_src_of(profile_home)
    actions: list[str] = []
    problems: list[str] = []
    for profile, role in profile_targets(profile_home, env):
        if not profile.is_dir():
            problems.append(f"profile {profile.name}: no profile home at {profile}")
            continue
        desired: dict[str, Any] = {"skills.external_dirs": skill_dirs_for(core_src, role)}
        ws = layout.from_env(env)
        with Store.open(ws.store_path, kind="workspace") as store:
            desired["skills.trusted_project_dirs"] = [
                str(ws.coworld(row["coworld_id"]).code) for row in coworlds.list(store)
            ]
        desired["terminal.cwd"] = str(ws.root)
        cli = HermesCli(hermes, profile, env, run)
        try:
            for key, value in desired.items():
                if cli.config_get(key) != value:
                    cli.config_set(key, value)
                    actions.append(f"{profile.name}: set {key}")
        except PrepareError as error:
            problems.append(f"profile {profile.name}: {error}")
    return {"applied": actions, "problems": problems}


def profile_targets(profile_home: Path, env: Mapping[str, str]) -> list[tuple[Path, str]]:
    """This profile and the Researcher profiles beside it, with their roles."""
    role = "researcher" if profile_home.name.startswith("researcher") else "orchestrator"
    names = [n.strip() for n in (env.get("IDE_RESEARCHER_PROFILES") or "researcher").split(",")]
    targets = [(profile_home, role)]
    for name in names:
        sibling = profile_home.parent / name
        if name and sibling.resolve() != profile_home.resolve():
            targets.append((sibling, "researcher"))
    return targets


def _core_src_of(profile_home: Path) -> Path:
    marker = profile_home / CORE_PATH_FILE
    return Path(marker.read_text().strip()) if marker.exists() else Path("core/src").resolve()


def _has_model_entry(model: str, models: Mapping[str, Any]) -> bool:
    """Whether Hermes would apply one of ``models`` to ``model``.

    Hermes matches spellings (``kimi-k3.1-pro`` and ``moonshotai/kimi-k3-1-pro`` both apply to
    ``moonshotai/kimi-k3.1-pro``), so its own resolver decides; prepare runs on Hermes's Python.
    Without Hermes (unit tests) only the exact id counts.
    """
    try:
        from hermes_constants import resolve_per_model_provider_routing
    except ImportError:
        return model in models
    # The resolver answers {} both for no entry and for an empty one (an owner's "no pin"), so
    # each entry is marked to tell them apart.
    marked = {
        name: {**entry, "_entry": True} for name, entry in models.items() if isinstance(entry, dict)
    }
    return bool(resolve_per_model_provider_routing(model, marked))


def unpinned_routing(routing: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The shipped host pins the owner's ``provider_routing`` does not already decide.

    Owner-set routing for every model (``only``, ``ignore``, ``order``, ``sort``) wins over a
    shipped pin, as does an owner entry Hermes would apply to the same model, even an empty one.
    """
    if any(rule in routing for rule in ("only", "ignore", "order", "sort")):  # even if empty
        return {}
    models = routing.get("models") or {}
    return {m: r for m, r in PROVIDER_ROUTING_MODELS.items() if not _has_model_entry(m, models)}


def apply_config(hermes: HermesCli, role: str, core_src: Path) -> list[str]:
    """Remove the retired keys, set every protected key whose value differs, point the profile
    with shared skills, and apply the shipped values the owner has not changed."""
    actions: list[str] = []
    for key in RETIRED_KEYS:
        name, element = _split_retired(key)
        if element is None:
            if hermes.config_unset(name):
                actions.append(f"config: unset {name}")
            continue
        current = hermes.config_get(name)
        if isinstance(current, list) and element in current:
            hermes.config_set(name, [item for item in current if item != element])
            actions.append(f"config: dropped {element!r} from {name}")
    desired = protected_keys_for(role)
    desired["skills.external_dirs"] = skill_dirs_for(core_src, role)
    for key, value in desired.items():
        if hermes.config_get(key) != value:
            hermes.config_set(key, value)
            actions.append(f"config: set {key}")
    for key, shipped in SHIPPED_VALUES.items():
        current = hermes.config_get(key)
        if current != shipped and (current is None or current in EARLIER_DEFAULTS.get(key, set())):
            hermes.config_set(key, shipped)
            actions.append(f"config: set {key} (shipped value)")
    routing = hermes.config_get("provider_routing") or {}
    missing = unpinned_routing(routing)
    if missing:
        hermes.config_set("provider_routing.models", {**(routing.get("models") or {}), **missing})
        actions.append("config: set provider_routing.models")
    shipped_env = ENV_PASSTHROUGH["common"] + ENV_PASSTHROUGH.get(role, [])
    current_env = hermes.config_get("terminal.env_passthrough") or []
    merged = list(dict.fromkeys([*current_env, *shipped_env]))
    if merged != current_env:
        hermes.config_set("terminal.env_passthrough", merged)
        actions.append("config: set terminal.env_passthrough (union)")
    return actions


def _model_change_recorded(ws: Workspace, profile_name: str) -> bool:
    """Whether this profile's model has already been changed through the bridge or by a
    migration. Read-only, so check mode can ask too."""
    if not ws.store_path.exists():
        return False
    with sqlite3.connect(f"file:{ws.store_path}?mode=ro", uri=True) as store:
        row = store.execute(
            "SELECT 1 FROM events WHERE kind='profile.model_updated' AND subject=? LIMIT 1",
            (profile_name,),
        ).fetchone()
    return row is not None


def _model_settings(hermes: HermesCli) -> dict[str, Any]:
    """The profile's model and fallbacks as a ``profile.model_updated`` event names them."""
    return {
        "model": hermes.config_get("model.default"),
        "fallbacks": [
            {"provider": entry.get("provider") or "", "model": entry.get("model") or ""}
            for entry in hermes.config_get("fallback_providers") or []
            if isinstance(entry, dict)
        ],
    }


def migrate_superseded_model(
    hermes: HermesCli, ws: Workspace, profile_name: str, role: str
) -> list[str]:
    """Move a profile still on the model an earlier release shipped to the current settings.

    The one case where preparation changes a model. Orchestrators installed earlier kept Kimi K3,
    which refuses far more first-try tool calls than K3.1 Pro (Asana 1220151358122555).
    Researchers installed earlier kept `recursive/flint-v1.0:free`, whose 1,000 free requests per
    UTC day ran out every busy day; each switch to the fallback mid-Session then re-read the whole
    context uncached. Both kept these only because they were installed earlier. A
    ``profile.model_updated`` event for the profile means the model was chosen on purpose, so it
    stays. The change records that event itself, so a user who moves back afterwards stays there.
    """
    superseded, replacement = SUPERSEDED_MODELS[role]
    if (
        hermes.config_get("model.default") != superseded
        or hermes.config_get("model.provider") != "openrouter"
        or _model_change_recorded(ws, profile_name)
    ):
        return []
    previous = _model_settings(hermes)
    changed = sorted(key for key, value in replacement.items() if hermes.config_get(key) != value)
    for key in changed:
        hermes.config_set(key, replacement[key])
    model = replacement["model.default"]
    action = f"config: moved model.default from {superseded} to {model} (one-time migration)"
    if hermes.dry_run:
        return [action]
    current = {"profile": profile_name, "role": role, "provider": "openrouter"}
    store = Store.open(ws.store_path, kind="workspace")
    try:
        with store.tx(Actor.harness()) as tx:
            tx.event(
                "profile.model_updated",
                profile_name,
                {
                    **current,
                    **_model_settings(hermes),
                    "previous": {**current, **previous},
                    "changed": changed,
                },
            )
    finally:
        store.close()
    return [action]


# --- the watcher cron job ---------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PrepareError(f"cannot read {path}: {exc}") from exc


def _job_list(payload: Any) -> list[dict[str, Any]]:
    """The jobs of a cron store in any shape Hermes reads: a list, ``{"jobs": [...]}`` or an
    id-keyed ``{"jobs": {...}}``."""
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    if not isinstance(jobs, list):
        raise PrepareError("cron store is not a job list")
    return jobs


def _find_job(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    return next((job for job in jobs if isinstance(job, dict) and job.get("id") == job_id), None)


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".json.pending")
    pending.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(pending, path)


def retire_watcher_job(profile: Path) -> str:
    """Remove the retired observation cron while preserving unrelated scheduled work."""
    jobs_path = profile / "cron" / "jobs.json"
    payload = _load_json(jobs_path) if jobs_path.exists() else {"jobs": []}
    if isinstance(payload, list):
        payload = {"jobs": payload}
    if not isinstance(payload, dict):
        raise PrepareError(f"{jobs_path} is not a cron store")
    payload["jobs"] = [job for job in _job_list(payload) if job["id"] != WATCHER_JOB_ID]
    _write_json_atomic(jobs_path, payload)
    return "cron: retired the watcher job; research serve owns observation"


# --- the workspace environment ----------------------------------------------------------------


def lock_sha(checkout: Path) -> str:
    return hashlib.sha256((checkout / "uv.lock").read_bytes()).hexdigest()


def _read_stamp(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def ensure_workspace_env(
    checkout: Path,
    ws: Workspace,
    run: Runner = subprocess.run,
    env: Mapping[str, str] = os.environ,
) -> bool:
    """Build the workspace environment from the checkout's ``uv.lock``, or verify a prebuilt one.

    Returns True when ``uv sync`` ran. The stamp ``<venv>/.lock-sha`` records the lock the
    environment was built from. A prebuilt environment (``IDE_WORKSPACE_ENV`` points elsewhere
    than ``tools/venv``, or ``tools/venv`` is a symlink hosting provided) is owned by hosting: it
    is never synced, only checked, and a stamp mismatch is an error naming both sides.
    """
    lock_path = checkout / "uv.lock"
    expected = lock_sha(checkout)
    stamp_path = ws.venv / LOCK_STAMP
    if ws.venv != ws.tools / "venv" or ws.venv.is_symlink():
        found = _read_stamp(stamp_path)
        if found != expected:
            raise PrepareError(
                f"prebuilt workspace environment {ws.venv} was built from a different uv.lock: "
                f"its {LOCK_STAMP} is {found or 'missing'} but {lock_path} hashes to {expected}. "
                "Rebuild it from this checkout, or unset IDE_WORKSPACE_ENV to build one here."
            )
        return False
    with file_lock(ws.tools / "venv.lock"):
        if _read_stamp(stamp_path) == expected:
            return False
        argv = ["uv", "sync", "--frozen", "--no-dev", "--project", str(checkout)]
        sync_env = {**env, "UV_PROJECT_ENVIRONMENT": str(ws.venv)}
        try:
            result = run(argv, env=sync_env, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise PrepareError(
                "uv not found on PATH; it is needed to build the workspace environment"
            ) from exc
        if result.returncode != 0:
            raise _failed(result)
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(expected + "\n")
        return True


# --- entry point ------------------------------------------------------------------------------


def load_profile_env(script: Path, source: Path) -> None:
    """Give the installed script what Hermes would give anything it runs for the profile.

    ``HERMES_HOME`` defaults to the profile the script is installed in, and the profile's ``.env``
    fills in what the environment does not set. The copy in the checkout belongs to no profile.
    """
    home = os.environ.get("HERMES_HOME")
    if not home:
        installed_in = script.resolve().parents[1]
        if installed_in == source.resolve():
            raise PrepareError("HERMES_HOME must name the profile home to prepare")
        home = os.environ["HERMES_HOME"] = str(installed_in)
    from hermes_cli.env_loader import resolve_home_dotenv

    for name, value in resolve_home_dotenv(Path(home)).items():
        os.environ.setdefault(name, value)


def prepare(
    source: Path,
    cli: HermesCli,
    env: Mapping[str, str] = os.environ,
    run: Runner = subprocess.run,
    *,
    check: bool = False,
) -> dict[str, Any]:
    """Prepare the profile at ``$HERMES_HOME`` from the distribution folder ``source``.

    ``source`` is ``<checkout>/orchestrator`` or ``<checkout>/researcher``; its name is the
    role. ``cli`` configures the selected Hermes profile. Preparation runs under Hermes's
    interpreter so a changed surface can request a refresh through its native session store.
    The research serve service owns observation and scheduling.
    """
    source = Path(source)
    role = source.name
    if role not in ROLES:
        raise PrepareError(f"the distribution folder must be named one of {ROLES}: {source}")
    if not env.get("HERMES_HOME"):
        raise PrepareError("HERMES_HOME must name the profile home to prepare")
    profile = Path(env["HERMES_HOME"])
    checkout = source.parent
    core_src = checkout / "core" / "src"
    actions: list[str] = []

    core_path = profile / CORE_PATH_FILE
    if not check:
        core_path.parent.mkdir(parents=True, exist_ok=True)
        core_path.write_text(str(core_src) + "\n")
    actions.append(f"wrote {CORE_PATH_FILE}")

    ws = layout.from_env(env)
    if not check:
        ws.ensure_dirs()
        actions.extend(ensure_stores(ws))

    cli.dry_run = check
    actions.extend(apply_config(cli, role, core_src))
    if cli.config_get("terminal.cwd") != str(ws.root):
        cli.config_set("terminal.cwd", str(ws.root))
    trusted = []
    if ws.store_path.exists():
        with sqlite3.connect(f"file:{ws.store_path}?mode=ro", uri=True) as store:
            trusted = [
                str(ws.coworld(row[0]).code)
                for row in store.execute(
                    "SELECT coworld_id FROM coworlds ORDER BY created,coworld_id"
                )
            ]
    if cli.config_get("skills.trusted_project_dirs") != trusted:
        cli.config_set("skills.trusted_project_dirs", trusted)
    if "IDE_LOCAL_STT_MODEL" in env:
        cli.config_set("stt.local.model", env["IDE_LOCAL_STT_MODEL"])
        actions.append("configured the hosted local speech model")

    # Hermes captures a login-shell environment before running terminal commands. Restore the
    # workspace CLI directory through its supported shell initialization hook after that login.
    shell_init = profile / "local" / "workspace-shell.sh"
    if not check:
        shell_init.write_text(f"export PATH={shlex.quote(str(ws.venv / 'bin'))}:$PATH\n")
    shell_files = cli.config_get("terminal.shell_init_files") or []
    if not shell_files and cli.config_get("terminal.auto_source_bashrc") is not False:
        shell_files = ["~/.profile", "~/.bash_profile", "~/.bashrc"]
    if str(shell_init) not in shell_files:
        cli.config_set("terminal.shell_init_files", [*shell_files, str(shell_init)])
        actions.append("config: add workspace terminal shell initialization")

    marker = profile / PREPARED_MARKER
    actions.extend(migrate_superseded_model(cli, ws, profile.name, role))
    if role == "orchestrator":
        if not check:
            actions.append(retire_watcher_job(profile))

    def request_prompt_refresh() -> None:
        from hermes_state import SessionDB

        with SessionDB(profile / STATE_DB) as state:
            state.request_prompt_refresh()

    actions.extend(refresh_frozen_sessions(profile, source, request_prompt_refresh, check=check))

    if check:
        actions.extend(cli.planned)
        summary = {"role": role, "workspace": str(ws.root), "check": True, "planned": actions}
        print(json.dumps(summary), flush=True)
        return summary

    if ensure_workspace_env(checkout, ws, run=run, env=env):
        actions.append("workspace environment synced")
    else:
        actions.append("workspace environment up to date")

    marker.touch()
    summary = {"role": role, "workspace": str(ws.root), "venv": str(ws.venv), "actions": actions}
    print(json.dumps(summary), flush=True)
    return summary


def ensure_stores(ws: layout.Workspace) -> list[str]:
    """Create or migrate the workspace store and every registered Coworld's store."""
    from research_core.store import coworlds
    from research_core.store.db import Store

    actions = []
    store = Store.open(ws.store_path, kind="workspace")
    try:
        actions.append("workspace store ready")
        for row in coworlds.list(store):
            coworld = ws.coworld(row["coworld_id"])
            coworld.ensure_dirs()
            Store.open(coworld.store_path, kind="coworld").close()
            actions.append(f"store ready: {row['coworld_id']}")
    finally:
        store.close()
    return actions
