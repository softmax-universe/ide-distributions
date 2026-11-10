"""The two profile shells: manifests, configs, plugin shims and the watcher job."""

import json
from pathlib import Path

import pytest
import yaml

from research_core.plugin.register import make_register
from tests.test_plugin_register import FakeContext

REPO = Path(__file__).resolve().parents[2]
PROFILES = {"orchestrator": REPO / "orchestrator", "researcher": REPO / "researcher"}


@pytest.mark.parametrize("role", sorted(PROFILES))
def test_distribution_manifest(role):
    manifest = yaml.safe_load((PROFILES[role] / "distribution.yaml").read_text())
    assert manifest["name"] == role
    assert manifest["hermes_requires"] == ">=0.22.6"
    names = {entry["name"] for entry in manifest["env_requires"]}
    assert {"SOFTMAX_PLAYER_ID", "SOFTMAX_PLAYER_CREDENTIAL", "IDE_WORKSPACE"} <= names
    assert ("SOFTMAX_USER_CREDENTIAL" in names) == (role == "orchestrator")
    for entry in manifest["env_requires"]:
        assert entry["description"].strip()


@pytest.mark.parametrize("role", sorted(PROFILES))
def test_config_has_protected_keys_and_no_gates(role):
    config = yaml.safe_load((PROFILES[role] / "config.yaml").read_text())
    assert config["approvals"]["mode"] == "off"
    assert "deny" not in config["approvals"]
    assert config["memory"] == {"memory_enabled": False, "user_profile_enabled": False}
    assert config["tools"]["tool_search"]["enabled"] == "off"
    assert config["plugins"]["enabled"] == [role]
    assert config["platform_toolsets"]["api_server"] == ["hermes-api-server", f"ide-{role}"]
    assert config["known_plugin_toolsets"]["cron"] == [f"ide-{role}"]
    assert config["agent"] == {
        "disabled_toolsets": [
            "browser",
            "connections",
            "cronjob",
            "todo",
            "session_search",
        ]
    }
    assert config["compression"]["proactive_prune_tokens"] == 64000
    assert "timeouts" not in config
    assert "kanban" not in config


def test_orchestrator_config_compaction_and_catch_up():
    config = yaml.safe_load((PROFILES["orchestrator"] / "config.yaml").read_text())
    assert config["compression"] == {
        "protect_first_n": 6,
        "protect_last_n": 30,
        "threshold": 0.6,
        "target_ratio": 0.3,
        "proactive_prune_tokens": 64000,
        "proactive_prune_min_result_chars": 2000,
    }
    assert config["cron"]["catch_up_missed"] is True


@pytest.mark.parametrize("role", sorted(PROFILES))
def test_plugin_manifest(role):
    manifest = yaml.safe_load((PROFILES[role] / "plugins" / role / "plugin.yaml").read_text())
    assert manifest["name"] == role
    assert manifest["kind"] == "standalone"
    distribution = yaml.safe_load((PROFILES[role] / "distribution.yaml").read_text())
    assert manifest["requires_hermes"] == distribution["hermes_requires"]
    ctx = FakeContext()
    make_register(role)(ctx)
    assert sorted(manifest["provides_hooks"]) == sorted({name for name, _ in ctx.hooks})


def test_profiles_ship_no_observation_cron():
    jobs = json.loads((PROFILES["orchestrator"] / "cron" / "jobs.json").read_text())["jobs"]
    assert jobs == []
    assert not (PROFILES["orchestrator"] / "scripts" / "watcher.py").exists()
    assert not (PROFILES["researcher"] / "cron").exists()


@pytest.mark.parametrize("role", sorted(PROFILES))
def test_no_bundled_skills_marker(role):
    assert (PROFILES[role] / ".no-bundled-skills").is_file()
    assert (PROFILES[role] / "SOUL.md").is_file()
