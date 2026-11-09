"""Profile preparation against a fake ``hermes`` executable and a fake ``uv``."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from research_core import layout, prepare

REPO = Path(__file__).resolve().parents[2]
CORE_SRC = REPO / "core" / "src"


def _parse_set_value(text: str):
    """What ``hermes config set`` stores for a command-line value: list and mapping literals
    are parsed, scalar words and numbers coerced, anything else kept as text."""
    if text[:1] in ("[", "{"):
        return json.loads(text)
    if text in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[text]
    for convert in (int, float):
        try:
            return convert(text)
        except ValueError:
            pass
    return text


class FakeHermes:
    """Stands in for the ``hermes`` executable and for ``uv``.

    Answers ``config get`` from an in-memory dotted-key dict, applies ``config set`` and
    ``config unset`` to it the way Hermes does (a missing key is "Config key not set", exit 1),
    records every ``cron resume`` and every argv it was called with.
    """

    def __init__(self, config=None):
        self.config = dict(config or {})
        self.calls: list[tuple[list[str], dict]] = []
        self.resumed: list[str] = []
        self.uv_result = CompletedProcess(["uv"], 0, "", "")

    def run(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.calls.append((argv, kwargs))
        if argv[0] == "uv":
            return self.uv_result
        if "research_core.cli" in argv:  # the default-Coworld switch in the workspace env
            return CompletedProcess(argv, 0, '{"ok": true}', "")
        exe, *rest = argv
        assert exe == "hermes", argv
        if rest[:2] == ["config", "get"]:
            key = rest[2]
            assert rest[3:] == ["--json"], rest
            value = self._get(key)
            if value is None:
                return CompletedProcess(argv, 1, "", f"Config key not set: {key}\n")
            return CompletedProcess(argv, 0, json.dumps(value) + "\n", "")
        if rest[:2] == ["config", "set"]:
            key, value = rest[2], rest[3]
            self.config[key] = _parse_set_value(value)
            return CompletedProcess(argv, 0, f"✓ Set {key}\n", "")
        if rest[:2] == ["config", "unset"]:
            key = rest[2]
            if key not in self.config:
                return CompletedProcess(argv, 1, "", f"Config key not set: {key}\n")
            del self.config[key]
            return CompletedProcess(argv, 0, f"✓ Unset {key}\n", "")
        if rest[:2] == ["cron", "resume"]:
            self.resumed.append(rest[2])
            return CompletedProcess(argv, 0, f"Resumed job: {rest[2]}\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    def _get(self, key):
        """The value at ``key``; a section is assembled from its dotted sub-keys, as Hermes does."""
        if key in self.config:
            return self.config[key]
        prefix = key + "."
        section = {k[len(prefix) :]: v for k, v in self.config.items() if k.startswith(prefix)}
        return section or None

    def hermes_calls(self):
        return [argv for argv, _ in self.calls if argv[0] == "hermes"]

    def sets(self):
        """The last value passed to ``config set`` per key."""
        return {argv[3]: argv[4] for argv in self.hermes_calls() if argv[1:3] == ["config", "set"]}

    def unsets(self):
        return [argv[3] for argv in self.hermes_calls() if argv[1:3] == ["config", "unset"]]


@pytest.fixture
def homes(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    env = {
        "HERMES_HOME": str(profile),
        "HERMES_REAL_HOME": str(tmp_path / "host"),
        "IDE_WORKSPACE": str(tmp_path / "workspace"),
        "PATH": os.environ.get("PATH", ""),
    }
    return profile, env


def run_prepare(fake, env, role="orchestrator"):
    prepare.prepare(
        source=REPO / role,
        cli=prepare.HermesCli("hermes", Path(env["HERMES_HOME"]), env, fake.run),
        env=env,
        run=fake.run,
    )


def read_jobs(profile):
    return json.loads((profile / "cron" / "jobs.json").read_text())


# --- keys -------------------------------------------------------------------------------------


def test_protected_keys_for_role_substitutes_the_role():
    orchestrator = prepare.protected_keys_for("orchestrator")
    researcher = prepare.protected_keys_for("researcher")
    assert orchestrator["plugins.enabled"] == ["orchestrator"]
    assert researcher["plugins.enabled"] == ["researcher"]
    assert researcher["platform_toolsets.api_server"] == ["hermes-api-server", "ide-researcher"]
    assert researcher["known_plugin_toolsets.cron"] == ["ide-researcher"]
    assert orchestrator["approvals.mode"] == "off"
    assert orchestrator["memory.memory_enabled"] is False
    assert orchestrator["tools.tool_search.enabled"] == "off"
    assert orchestrator["cron.catch_up_missed"] is True
    for keys in (orchestrator, researcher):
        assert keys["agent.disabled_toolsets"] == ["browser", "connections", "cronjob", "todo"]
        assert keys["compression.proactive_prune_tokens"] == 64000
        assert keys["compression.proactive_prune_min_result_chars"] == 2000
    # Compression protection is Orchestrator only (design F.5).
    assert orchestrator["compression.protect_first_n"] == 6
    assert orchestrator["compression.protect_last_n"] == 30
    assert orchestrator["compression.threshold"] == 0.6
    assert orchestrator["compression.target_ratio"] == 0.3
    assert {key for key in researcher if key.startswith("compression.")} == {
        "compression.proactive_prune_tokens",
        "compression.proactive_prune_min_result_chars",
    }
    assert "<role>" not in json.dumps(orchestrator) + json.dumps(researcher)


def test_prepare_rejects_unknown_role(tmp_path, homes):
    profile, env = homes
    source = tmp_path / "checkout" / "supervisor"
    source.mkdir(parents=True)
    with pytest.raises(prepare.PrepareError, match="supervisor"):
        prepare.prepare(
            source=source,
            cli=prepare.HermesCli("hermes", profile, env, FakeHermes().run),
            env=env,
        )


def test_the_checkouts_own_script_belongs_to_no_profile(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    source = REPO / "orchestrator"
    with pytest.raises(prepare.PrepareError, match="HERMES_HOME"):
        prepare.load_profile_env(source / "scripts" / "prepare.py", source)


def test_prepare_requires_hermes_home():
    with pytest.raises(prepare.PrepareError, match="HERMES_HOME"):
        prepare.prepare(
            source=REPO / "orchestrator",
            cli=prepare.HermesCli("hermes", Path("/unused"), {}, FakeHermes().run),
            env={},
        )


# --- the core path and the summary ------------------------------------------------------------


def test_prepare_writes_core_path(homes):
    profile, env = homes
    run_prepare(FakeHermes(), env)
    assert (profile / "local" / "core-path.txt").read_text().strip() == str(CORE_SRC)


def test_prepare_prints_one_json_summary_line(homes, capsys):
    profile, env = homes
    run_prepare(FakeHermes(), env)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["role"] == "orchestrator"
    assert summary["workspace"] == env["IDE_WORKSPACE"]
    assert summary["venv"] == str(Path(env["IDE_WORKSPACE"]) / "tools" / "venv")
    assert isinstance(summary["actions"], list) and summary["actions"]


def test_prepare_runs_every_hermes_command_in_the_profile_home(homes):
    profile, env = homes
    fake = FakeHermes()
    run_prepare(fake, env)
    hermes_calls = [kwargs for argv, kwargs in fake.calls if argv[0] == "hermes"]
    assert hermes_calls
    for kwargs in hermes_calls:
        assert kwargs["env"]["HERMES_HOME"] == str(profile)
        assert kwargs["env"]["PATH"] == env["PATH"]


# --- config keys ------------------------------------------------------------------------------


def test_prepare_sets_protected_keys_and_unsets_retired(homes):
    profile, env = homes
    fake = FakeHermes(
        {
            "approvals.mode": "smart",
            "approvals.deny": ["rm -rf /"],
            "agent.max_turns": 40,
            "agent.run_budget_seconds": 5400,
            "timeouts": {"tools": {"sequential_call": 420}},
            "kanban": {"auto_decompose": False},
            "skills.external_dirs": ["/kept/elsewhere", "../../games"],
            "terminal.timeout": 1800,
        }
    )
    run_prepare(fake, env)

    for key, value in prepare.protected_keys_for("orchestrator").items():
        assert fake.config[key] == value, key
    for key in ("approvals.deny", "agent.max_turns", "agent.run_budget_seconds", "timeouts"):
        assert key not in fake.config, key
    assert "kanban" not in fake.config
    assert fake.config["skills.external_dirs"] == [
        str(CORE_SRC / "research_core" / "skills" / "common"),
        str(CORE_SRC / "research_core" / "skills" / "orchestrator"),
    ]
    assert fake.config["terminal.timeout"] == 1800, "an owner's value is kept"

    sets = fake.sets()
    assert sets["approvals.mode"] == "off", "a string value goes verbatim"
    assert json.loads(sets["plugins.enabled"]) == ["orchestrator"], "structured values are JSON"
    assert sets["memory.memory_enabled"] == "false"
    assert set(fake.unsets()) >= {"approvals.deny", "agent.max_turns", "timeouts", "kanban"}


def test_prepare_only_sets_keys_whose_value_differs(homes):
    profile, env = homes
    fake = FakeHermes()
    run_prepare(fake, env)
    first = len(fake.sets())
    assert first >= len(prepare.protected_keys_for("orchestrator"))
    fake.calls.clear()
    run_prepare(fake, env)
    assert fake.sets() == {}, "a second run finds every protected key in place"
    # The retired keys are still probed (Hermes reports "not set") but nothing fails.
    assert set(fake.unsets()) == set(prepare.plain_retired_keys())


def test_prepare_researcher_uses_its_role_and_has_no_cron(homes):
    profile, env = homes
    fake = FakeHermes()
    run_prepare(fake, env, role="researcher")
    assert fake.config["plugins.enabled"] == ["researcher"]
    assert fake.config["platform_toolsets.api_server"] == ["hermes-api-server", "ide-researcher"]
    assert "compression.protect_first_n" not in fake.config
    assert fake.config["skills.external_dirs"][1].endswith("/skills/researcher")
    assert not (profile / "cron").exists()
    assert fake.resumed == []
    assert (profile / ".softmax-prepared").exists()


def test_prepare_reports_a_failed_hermes_command_with_its_output(homes):
    profile, env = homes

    def failing_run(argv, **kwargs):
        if argv[0] == "uv":
            return CompletedProcess(argv, 0, "", "")
        if argv[1:3] == ["config", "get"]:
            return CompletedProcess(argv, 1, "", "Config key not set: x\n")
        return CompletedProcess(argv, 1, "partial stdout\n", "boom: config is unreadable\n")

    with pytest.raises(prepare.PrepareError) as info:
        prepare.prepare(
            source=REPO / "orchestrator",
            cli=prepare.HermesCli("hermes", profile, env, failing_run),
            env=env,
            run=failing_run,
        )
    message = str(info.value)
    assert "exit code 1" in message and "boom: config is unreadable" in message
    assert "partial stdout" in message


# --- the watcher job --------------------------------------------------------------------------


@pytest.mark.parametrize("bare", [False, True])
def test_prepare_retires_watcher_and_preserves_other_jobs(homes, bare):
    profile, env = homes
    foreign = {"id": "nightly", "enabled": True, "state": "scheduled"}
    jobs = [foreign, {"id": "watcher", "enabled": True, "state": "scheduled"}]
    (profile / "cron").mkdir()
    payload = jobs if bare else {"jobs": jobs, "updated_at": "retained"}
    (profile / "cron" / "jobs.json").write_text(json.dumps(payload))
    fake = FakeHermes()
    run_prepare(fake, env)
    run_prepare(fake, env)
    retained = read_jobs(profile)
    assert retained["jobs"] == [foreign]
    if not bare:
        assert retained["updated_at"] == "retained"
    assert not fake.resumed
    assert not list((profile / "cron").glob("*.pending"))


def test_prepare_does_not_install_observation_cron(homes):
    profile, env = homes
    run_prepare(FakeHermes(), env)
    assert read_jobs(profile)["jobs"] == []


# --- the workspace environment ----------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "uv.lock").write_text("version = 1\n")
    return root


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_ensure_workspace_env_syncs_when_stamp_differs(tmp_path, checkout):
    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path / "ws")})
    ws.ensure_dirs()
    fake = FakeHermes()

    assert prepare.ensure_workspace_env(checkout, ws, run=fake.run, env={"PATH": "/bin"}) is True
    ((argv, kwargs),) = fake.calls
    assert argv == ["uv", "sync", "--frozen", "--no-dev", "--project", str(checkout)]
    assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(ws.venv)
    assert kwargs["env"]["PATH"] == "/bin"
    assert (ws.venv / ".lock-sha").read_text().strip() == sha(checkout / "uv.lock")

    fake.calls.clear()
    assert prepare.ensure_workspace_env(checkout, ws, run=fake.run, env={}) is False
    assert fake.calls == [], "a matching stamp means no sync"

    (checkout / "uv.lock").write_text("version = 2\n")
    assert prepare.ensure_workspace_env(checkout, ws, run=fake.run, env={}) is True
    assert (ws.venv / ".lock-sha").read_text().strip() == sha(checkout / "uv.lock")


def test_ensure_workspace_env_prebuilt_mismatch_raises(tmp_path, checkout):
    prebuilt = tmp_path / "prebuilt"
    prebuilt.mkdir()
    ws = layout.from_env(
        {"IDE_WORKSPACE": str(tmp_path / "ws"), "IDE_WORKSPACE_ENV": str(prebuilt)}
    )
    fake = FakeHermes()

    with pytest.raises(prepare.PrepareError) as info:
        prepare.ensure_workspace_env(checkout, ws, run=fake.run, env={})
    assert str(prebuilt) in str(info.value) and str(checkout / "uv.lock") in str(info.value)
    assert fake.calls == [], "a prebuilt environment is never synced"

    (prebuilt / ".lock-sha").write_text("0" * 64 + "\n")
    with pytest.raises(prepare.PrepareError):
        prepare.ensure_workspace_env(checkout, ws, run=fake.run, env={})

    (prebuilt / ".lock-sha").write_text(sha(checkout / "uv.lock") + "\n")
    assert prepare.ensure_workspace_env(checkout, ws, run=fake.run, env={}) is False
    assert fake.calls == []


def test_ensure_workspace_env_symlinked_venv_is_prebuilt(tmp_path, checkout):
    """Hosting may link tools/venv to a read-only image environment (Figure 2)."""
    image_env = tmp_path / "image-env"
    image_env.mkdir()
    (image_env / ".lock-sha").write_text(sha(checkout / "uv.lock") + "\n")
    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path / "ws")})
    ws.ensure_dirs()
    ws.venv.symlink_to(image_env)
    fake = FakeHermes()
    assert prepare.ensure_workspace_env(checkout, ws, run=fake.run, env={}) is False
    assert fake.calls == []


def test_ensure_workspace_env_reports_uv_failure_with_output(tmp_path, checkout):
    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path / "ws")})
    fake = FakeHermes()
    fake.uv_result = CompletedProcess(["uv"], 2, "resolving...\n", "error: No solution found\n")
    with pytest.raises(prepare.PrepareError) as info:
        prepare.ensure_workspace_env(checkout, ws, run=fake.run, env={})
    message = str(info.value)
    assert "exit code 2" in message and "No solution found" in message and "resolving" in message
    assert not (ws.venv / ".lock-sha").exists()


def test_ensure_workspace_env_requires_uv(tmp_path, checkout):
    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path / "ws")})

    def no_uv(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    with pytest.raises(prepare.PrepareError, match="uv"):
        prepare.ensure_workspace_env(checkout, ws, run=no_uv, env={})


def test_prepare_passes_the_release_checkout_to_uv(homes):
    profile, env = homes
    fake = FakeHermes()
    run_prepare(fake, env)
    (uv_call,) = [argv for argv, _ in fake.calls if argv[0] == "uv"]
    assert uv_call[-1] == str(REPO)
    venv = Path(env["IDE_WORKSPACE"]) / "tools" / "venv"
    assert (venv / ".lock-sha").read_text().strip() == sha(REPO / "uv.lock")


# --- the final preparation rules (plan task 7.8, amendments A5.9, A6.4, A7.4) ---------------


def _prepared(homes, fake, role="orchestrator", **extra_env):
    profile, env = homes
    env = {**env, **extra_env}
    (REPO / role / "cron").exists()
    prepare.prepare(
        REPO / role,
        prepare.HermesCli("hermes", profile, env, fake.run),
        env=env,
        run=fake.run,
    )
    return profile, env


def test_prepare_points_every_profile_at_the_active_coworld(homes):
    from research_core.store import coworlds
    from research_core.store.actors import Actor
    from research_core.store.db import Store

    profile, env = homes
    ws = layout.from_env(env)
    ws.ensure_dirs()
    store = Store.open(ws.store_path, kind="workspace")
    harness = Actor.parse("harness")
    coworlds.register(store, harness, "cow_fake", folder="cow_fake")
    coworlds.activate(store, harness, "cow_fake")
    store.close()
    fake = FakeHermes()
    prepare.prepare(
        REPO / "orchestrator",
        prepare.HermesCli("hermes", profile, env, fake.run),
        env=env,
        run=fake.run,
    )
    code = str(ws.coworld("cow_fake").code)
    assert str(ws.coworld("cow_fake").mixin / "skills") not in fake.config["skills.external_dirs"]
    assert fake.config["skills.trusted_project_dirs"] == [code]
    assert fake.config["terminal.cwd"] == str(ws.root)
    assert ws.coworld("cow_fake").store_path.exists()  # ensure_stores migrated it


def test_prepare_applies_shipped_values_only_over_earlier_defaults(homes):
    fake = FakeHermes({"terminal.timeout": 180})
    _prepared(homes, fake)
    assert fake.config["terminal.timeout"] == 1800  # an earlier default is replaced
    assert "IDE_RESEARCHER_PROFILES" in fake.config["terminal.env_passthrough"]


def test_prepare_adds_the_kimi_host_pins_beside_the_owners_models(homes):
    fresh = FakeHermes()
    _prepared(homes, fresh)
    assert fresh.config["provider_routing.models"] == prepare.PROVIDER_ROUTING_MODELS

    empty = FakeHermes({"provider_routing": {"models": None}})  # a bare "models:" in YAML
    _prepared(homes, empty)
    assert empty.config["provider_routing.models"] == prepare.PROVIDER_ROUTING_MODELS

    owner = FakeHermes({"provider_routing": {"models": {"some/model": {"order": ["x"]}}}})
    _prepared(homes, owner)
    assert owner.config["provider_routing.models"] == {
        "some/model": {"order": ["x"]},
        **prepare.PROVIDER_ROUTING_MODELS,
    }


@pytest.mark.parametrize(
    "routing",
    [
        {"models": {m: {"ignore": ["y"]} for m in prepare.PROVIDER_ROUTING_MODELS}},
        {"only": ["deepinfra"]},  # the owner's rule for every model
        {"order": ["deepinfra"]},
        {"ignore": ["deepinfra"]},
        {"sort": "latency"},
        {"only": []},  # written by the owner, even if it restricts nothing
        {"models": {m: {} for m in prepare.PROVIDER_ROUTING_MODELS}},  # an owner's "no pin"
    ],
)
def test_the_owners_routing_wins_over_the_shipped_pins(homes, routing):
    # Spellings Hermes also applies are covered against a real Hermes (test_prepare_real).
    assert prepare.unpinned_routing(routing) == {}
    fake = FakeHermes({"provider_routing": routing})
    _prepared(homes, fake)
    assert "provider_routing.models" not in fake.config


GUARDRAIL_KEYS = {
    "tool_loop_guardrails.hard_stop_enabled": True,
    "tool_loop_guardrails.hard_stop_after.same_tool_failure": 5,
    "tool_loop_guardrails.hard_stop_after.idempotent_no_progress": 10,
}


@pytest.mark.parametrize("role", ["orchestrator", "researcher"])
@pytest.mark.parametrize(
    "existing",
    [
        {},  # a fresh profile
        {  # an install from before the hard stop, with Hermes's defaults spelled out
            "tool_loop_guardrails.hard_stop_enabled": False,
            "tool_loop_guardrails.hard_stop_after.same_tool_failure": 8,
            "tool_loop_guardrails.hard_stop_after.idempotent_no_progress": 5,
            "tool_loop_guardrails.warn_after.same_tool_failure": 3,
        },
    ],
)
def test_prepare_turns_on_the_loop_guard_hard_stop(homes, role, existing):
    profile, env = homes
    fake = FakeHermes(existing)
    run_prepare(fake, env, role=role)
    for key, value in GUARDRAIL_KEYS.items():
        assert fake.config[key] == value, key
    assert fake.sets()["tool_loop_guardrails.hard_stop_enabled"] == "true"
    assert fake.sets()["tool_loop_guardrails.hard_stop_after.same_tool_failure"] == "5"
    assert fake.sets()["tool_loop_guardrails.hard_stop_after.idempotent_no_progress"] == "10"
    if existing:
        assert fake.config["tool_loop_guardrails.warn_after.same_tool_failure"] == 3, "kept"


def test_shipped_configs_carry_the_loop_guard_hard_stop():
    import yaml

    for role in ("orchestrator", "researcher"):
        shipped = yaml.safe_load((REPO / role / "config.yaml").read_text())
        assert shipped["tool_loop_guardrails"] == {
            "hard_stop_enabled": True,
            "hard_stop_after": {"same_tool_failure": 5, "idempotent_no_progress": 10},
        }


def test_shipped_configs_carry_the_kimi_host_pins():
    import yaml

    for role in ("orchestrator", "researcher"):
        shipped = yaml.safe_load((REPO / role / "config.yaml").read_text())
        assert shipped["provider_routing"]["models"] == prepare.PROVIDER_ROUTING_MODELS


def test_prepare_never_touches_the_model_settings(homes):
    fake = FakeHermes(
        {"model.default": "owner/choice", "terminal.env_passthrough": ["OPENROUTER_MODEL"]}
    )
    _prepared(homes, fake, OPENROUTER_MODEL="ignored/model")
    assert fake.config["model.default"] == "owner/choice"
    assert "fallback_providers" not in fake.config and "model.provider" not in fake.config
    touched = [
        argv for argv, _ in fake.calls if "config" in argv and ("set" in argv or "unset" in argv)
    ]
    assert not [argv for argv in touched if any(a.startswith(("model", "fallback")) for a in argv)]
    assert "OPENROUTER_MODEL" not in fake.config["terminal.env_passthrough"]  # retired


def _model_events(env):
    path = layout.from_env(env).store_path
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as store:
        rows = store.execute(
            "SELECT actor, subject, payload FROM events WHERE kind='profile.model_updated'"
        ).fetchall()
    return [(actor, subject, json.loads(payload)) for actor, subject, payload in rows]


KIMI_K3_ORCHESTRATOR = {
    "model.default": "moonshotai/kimi-k3",
    "model.provider": "openrouter",
    "fallback_providers": [{"provider": "openrouter", "model": "z-ai/glm-5.3"}],
}


def test_prepare_moves_an_orchestrator_off_the_superseded_default_once(homes):
    fake = FakeHermes(dict(KIMI_K3_ORCHESTRATOR))
    profile, env = _prepared(homes, fake)
    assert fake.config["model.default"] == "moonshotai/kimi-k3.1-pro"
    assert fake.config["fallback_providers"] == KIMI_K3_ORCHESTRATOR["fallback_providers"]
    [(actor, subject, payload)] = _model_events(env)
    assert (actor, subject) == ("harness", profile.name)
    assert payload["model"] == "moonshotai/kimi-k3.1-pro"
    assert payload["previous"]["model"] == "moonshotai/kimi-k3"
    assert payload["fallbacks"] == [{"provider": "openrouter", "model": "z-ai/glm-5.3"}]
    assert payload["changed"] == ["model.default"]

    # A user who moves back to K3 afterwards stays there, and no second event appears.
    fake.config["model.default"] = "moonshotai/kimi-k3"
    _prepared(homes, fake)
    assert fake.config["model.default"] == "moonshotai/kimi-k3"
    assert len(_model_events(env)) == 1


def test_prepare_moves_a_researcher_fallback_off_kimi_k3_once(homes):
    fake = FakeHermes(
        {
            "model.default": "recursive/flint-v1.0:free",
            "model.provider": "openrouter",
            "fallback_providers": [{"provider": "openrouter", "model": "moonshotai/kimi-k3"}],
        }
    )
    profile, env = _prepared(homes, fake, role="researcher")
    k31 = [{"provider": "openrouter", "model": "moonshotai/kimi-k3.1-pro"}]
    assert fake.config["fallback_providers"] == k31
    assert fake.config["model.default"] == "recursive/flint-v1.0:free"
    [(actor, subject, payload)] = _model_events(env)
    assert (actor, subject) == ("harness", profile.name)
    assert payload["fallbacks"] == k31
    assert payload["previous"]["fallbacks"] == [
        {"provider": "openrouter", "model": "moonshotai/kimi-k3"}
    ]
    assert payload["changed"] == ["fallback_providers"]


def test_prepare_keeps_a_kimi_k3_the_user_chose(homes):
    from research_core.store.actors import Actor
    from research_core.store.db import Store

    profile, env = homes
    store = Store.open(layout.from_env(env).store_path, kind="workspace")
    with store.tx(Actor.user("james")) as tx:
        tx.event("profile.model_updated", profile.name, {"model": "moonshotai/kimi-k3"})
    store.close()
    fake = FakeHermes(dict(KIMI_K3_ORCHESTRATOR))
    _prepared(homes, fake)
    assert fake.config["model.default"] == "moonshotai/kimi-k3"


@pytest.mark.parametrize(
    "config, role",
    [
        (KIMI_K3_ORCHESTRATOR, "researcher"),
        ({**KIMI_K3_ORCHESTRATOR, "model.provider": "custom"}, "orchestrator"),
        ({**KIMI_K3_ORCHESTRATOR, "model.default": "owner/choice"}, "orchestrator"),
    ],
)
def test_prepare_moves_only_an_orchestrator_on_the_superseded_default(homes, config, role):
    fake = FakeHermes(dict(config))
    _, env = _prepared(homes, fake, role=role)
    assert fake.config["model.default"] == config["model.default"]
    assert _model_events(env) == []


def test_prepare_check_mode_plans_the_model_move_without_making_it(homes, capsys):
    profile, env = homes
    fake = FakeHermes(dict(KIMI_K3_ORCHESTRATOR))
    summary = prepare.prepare(
        REPO / "orchestrator",
        prepare.HermesCli("hermes", profile, env, fake.run),
        env=env,
        run=fake.run,
        check=True,
    )
    assert any("moonshotai/kimi-k3.1-pro" in action for action in summary["planned"])
    assert fake.config["model.default"] == "moonshotai/kimi-k3"
    assert not layout.from_env(env).store_path.exists()


def test_prepare_keeps_the_owners_passthrough_entries(homes):
    fake = FakeHermes({"terminal.env_passthrough": ["MY_VAR"]})
    _prepared(homes, fake)
    passthrough = fake.config["terminal.env_passthrough"]
    assert passthrough[0] == "MY_VAR" and "IDE_WORKSPACE" in passthrough


def test_prepare_bootstraps_the_default_coworld_through_the_workspace_env(homes):
    fake = FakeHermes()
    profile, env = _prepared(homes, fake, IDE_DEFAULT_COWORLD="cow_first")
    switches = [argv for argv, _ in fake.calls if "switch" in argv]
    assert switches == []  # A host default cannot bind the user's sessions.


def test_prepare_check_mode_plans_without_writing(homes, capsys):
    profile, env = homes
    fake = FakeHermes()
    summary = prepare.prepare(
        REPO / "orchestrator",
        prepare.HermesCli("hermes", profile, env, fake.run),
        env=env,
        run=fake.run,
        check=True,
    )
    assert summary["check"] is True
    assert any(a.startswith("config: set approvals.mode") for a in summary["planned"])
    assert not any("cron: resume" in action for action in summary["planned"])
    assert fake.sets() == {} and fake.resumed == []
    assert not (profile / prepare.PREPARED_MARKER).exists()
    assert not (profile / prepare.CORE_PATH_FILE).exists()


def test_apply_skill_dirs_reports_missing_profiles(tmp_path):
    profiles = tmp_path / "profiles"
    (profiles / "orchestrator").mkdir(parents=True)
    (profiles / "researcher").mkdir()
    fake = FakeHermes()
    env = {
        "IDE_RESEARCHER_PROFILES": "researcher,missing",
        "IDE_CORE_SRC": str(CORE_SRC),
        "IDE_WORKSPACE": str(tmp_path / "ws"),
    }
    result = prepare.apply_skill_dirs(
        profiles / "orchestrator",
        "hermes",
        env=env,
        run=fake.run,
    )
    assert result["problems"] == [f"profile missing: no profile home at {profiles / 'missing'}"]
    assert {a.split(":")[0] for a in result["applied"]} == {"orchestrator", "researcher"}
    assert fake.config["terminal.cwd"] == str(tmp_path / "ws")
