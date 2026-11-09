"""Each profile's model through the seam: ``GET /models``, ``GET /models/catalog`` and
``POST /models`` (``profile_models.py``; contract "Profile models")."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from research_core import profile_models
from research_core.client import ScriptedClient, SeamError
from research_core.profile_models import Catalog, ModelError, Profile
from research_core.serve import SeamServer
from research_core.watcher.hermes_api import FakeHermesRuns
from tests.test_serve import make_ctx

REPO = Path(__file__).resolve().parents[2]
CATALOG = [
    {
        "id": "moonshotai/kimi-k3",
        "name": "MoonshotAI: Kimi K3",
        "context_length": 1048576,
        "pricing": {"prompt": "0.000000192", "completion": "0.000015"},
        "supported_parameters": ["tools", "temperature"],
    },
    {
        "id": "recursive/flint-v1.0:free",
        "name": "Recursive: Flint (free)",
        "context_length": 262144,
        "pricing": {"prompt": "0", "completion": "0"},
        "supported_parameters": ["tools"],
    },
    {
        "id": "moonshotai/kimi-k3.1-pro",
        "name": "MoonshotAI: Kimi K3.1 Pro",
        "context_length": 1048576,
        "pricing": {"prompt": "0.000002", "completion": "0.00001"},
        "supported_parameters": ["tools"],
    },
    {
        "id": "z-ai/glm-5.3",
        "name": "Z.ai: GLM 5.3",
        "context_length": 1048576,
        "pricing": {"prompt": "0.0000006", "completion": "0.0000022"},
        "supported_parameters": ["tools"],
    },
    {
        "id": "someone/chat-only",
        "name": "No tools",
        "pricing": {"prompt": "0", "completion": "0"},
        "supported_parameters": ["temperature"],
    },
]


class FakeHermesConfig:
    """``hermes config set <key> <value>`` against ``$HERMES_HOME/config.yaml``, as Hermes
    v0.22.6 does it: a JSON list or mapping literal is parsed, any other text kept verbatim."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv, *, env, **_kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        _hermes, command, action, key, text = argv
        assert (command, action) == ("config", "set")
        if key == self.fail_on:
            return subprocess.CompletedProcess(argv, 1, "", f"cannot set {key}")
        path = Path(env["HERMES_HOME"]) / "config.yaml"
        config = yaml.safe_load(path.read_text()) or {}
        value = json.loads(text) if text[:1] in "[{" else text
        node = config
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
        path.write_text(yaml.safe_dump(config, sort_keys=False))
        return subprocess.CompletedProcess(argv, 0, f"Set {key}", "")


def install_profiles(root: Path) -> list[Profile]:
    """The shipped configs, as ``hermes profile install`` copies them."""
    profiles = profile_models.sandbox_profiles(root, "orchestrator", ["researcher"])
    for profile in profiles:
        profile.home.mkdir(parents=True)
        shutil.copy(REPO / profile.role / "config.yaml", profile.home / "config.yaml")
    return profiles


class Seam:
    def __init__(self, tmp_path: Path, *, catalog: Catalog, runner: FakeHermesConfig) -> None:
        self.ctx = make_ctx(tmp_path / "ws")
        self.profiles = install_profiles(tmp_path / "profiles")
        self.runner = runner
        self.server = SeamServer(
            ws=self.ctx.workspace,
            hermes=FakeHermesRuns(),
            profiles=self.profiles,
            hermes_bin="hermes",
            env={},
            config_runner=runner,
            catalog=catalog,
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = ScriptedClient(self.server.url)


@pytest.fixture
def seam(tmp_path):
    seam = Seam(tmp_path, catalog=Catalog(lambda: CATALOG), runner=FakeHermesConfig())
    yield seam
    seam.server.shutdown()


# --- the shipped defaults ---------------------------------------------------------------------


def test_the_shipped_defaults_per_role(seam):
    by_profile = {entry["profile"]: entry for entry in seam.client.models()["profiles"]}
    assert by_profile["orchestrator"] == {
        "profile": "orchestrator",
        "role": "orchestrator",
        "model": "moonshotai/kimi-k3.1-pro",
        "provider": "openrouter",
        "fallbacks": [{"provider": "openrouter", "model": "z-ai/glm-5.3"}],
    }
    assert by_profile["researcher"] == {
        "profile": "researcher",
        "role": "researcher",
        "model": "recursive/flint-v1.0:free",
        "provider": "openrouter",
        "fallbacks": [{"provider": "openrouter", "model": "moonshotai/kimi-k3.1-pro"}],
    }


def test_no_manifest_asks_the_host_for_a_model():
    for manifest in ("orchestrator", "researcher"):
        text = (REPO / manifest / "distribution.yaml").read_text()
        assert "OPENROUTER_MODEL" not in text, manifest


# --- changing a model -------------------------------------------------------------------------


def test_set_model_writes_through_hermes_and_records_an_event(seam):
    reply = seam.client.set_model("researcher", "moonshotai/kimi-k3", fallbacks=["z-ai/glm-5.3"])

    assert reply["verified"] is True
    assert reply["changed"] == ["fallback_providers", "model.default"]
    assert reply["previous"]["model"] == "recursive/flint-v1.0:free"
    assert reply["profile"]["model"] == "moonshotai/kimi-k3"
    assert reply["profile"]["fallbacks"] == [{"provider": "openrouter", "model": "z-ai/glm-5.3"}]
    written = yaml.safe_load((seam.profiles[1].home / "config.yaml").read_text())
    assert written["model"] == {"default": "moonshotai/kimi-k3", "provider": "openrouter"}
    assert all(call[:3] == ["hermes", "config", "set"] for call in seam.runner.calls)

    [event] = [
        e
        for e in seam.client.events("workspace", 0, idle=0.3)
        if e["kind"] == "profile.model_updated"
    ]
    assert event["seq"] == reply["seq"] and event["subject"] == "researcher"
    assert event["actor"] == "user:local"
    assert event["payload"]["model"] == "moonshotai/kimi-k3"
    assert event["payload"]["previous"]["model"] == "recursive/flint-v1.0:free"
    # The orchestrator is untouched.
    assert seam.client.models()["profiles"][0]["model"] == "moonshotai/kimi-k3.1-pro"


def test_setting_the_same_model_writes_nothing_and_records_no_event(seam):
    reply = seam.client.set_model("orchestrator", "moonshotai/kimi-k3.1-pro")
    assert reply["changed"] == [] and reply["seq"] is None
    assert seam.runner.calls == []


def test_refusals(seam):
    cases = [
        ({"profile": "ghost", "model": "moonshotai/kimi-k3"}, 404, "no profile 'ghost'"),
        ({"profile": "researcher", "model": "kimi"}, 400, "model change refused"),
        ({"profile": "researcher", "model": "someone/chat-only"}, 400, "model change refused"),
        (
            {"profile": "researcher", "model": "moonshotai/kimi-k3", "fallbacks": "x"},
            400,
            "refused",
        ),
        (
            {"profile": "researcher", "model": "moonshotai/kimi-k3", "actor": "harness"},
            400,
            "only a user",
        ),
    ]
    for body, status, error in cases:
        with pytest.raises(SeamError) as raised:
            seam.client._post("/models", body)
        assert raised.value.status == status, body
        assert error in raised.value.body["error"], body
    with pytest.raises(SeamError) as raised:
        seam.client._post("/models", {"profile": "researcher", "model": "someone/chat-only"})
    assert raised.value.body["problems"] == [
        "someone/chat-only is not an OpenRouter model that takes tool calls"
    ]
    assert seam.runner.calls == []


def test_an_unreachable_catalog_accepts_the_change_unverified(tmp_path):
    def offline():
        raise OSError("no network")

    seam = Seam(tmp_path, catalog=Catalog(offline), runner=FakeHermesConfig())
    try:
        reply = seam.client.set_model("researcher", "vendor/new-model")
        assert reply["verified"] is False and reply["profile"]["model"] == "vendor/new-model"
        with pytest.raises(SeamError) as raised:
            seam.client.model_catalog()
        assert raised.value.status == 503 and raised.value.headers.get("Retry-After") == "30"
    finally:
        seam.server.shutdown()


def test_a_failed_hermes_write_says_what_was_written(tmp_path):
    runner = FakeHermesConfig(fail_on="model.default")
    seam = Seam(tmp_path, catalog=Catalog(lambda: CATALOG), runner=runner)
    try:
        with pytest.raises(SeamError) as raised:
            seam.client.set_model("researcher", "moonshotai/kimi-k3", fallbacks=["z-ai/glm-5.3"])
        assert raised.value.status == 400
        assert "after writing fallback_providers" in raised.value.body["error"]
        assert "cannot set model.default" in raised.value.body["problems"][0]
    finally:
        seam.server.shutdown()


# --- the catalog ------------------------------------------------------------------------------


def test_catalog_lists_only_models_that_take_tool_calls(seam):
    reply = seam.client.model_catalog()
    ids = [model["id"] for model in reply["models"]]
    assert ids == [
        "moonshotai/kimi-k3",
        "recursive/flint-v1.0:free",
        "moonshotai/kimi-k3.1-pro",
        "z-ai/glm-5.3",
    ]
    kimi = reply["models"][0]
    assert kimi["usd_per_million_input"] == 0.192 and kimi["usd_per_million_output"] == 15.0
    assert kimi["free"] is False and reply["models"][1]["free"] is True


def test_catalog_is_cached_and_keeps_the_last_good_list():
    fetches = []
    now = [0.0]

    def fetch():
        fetches.append(now[0])
        if len(fetches) > 1:
            raise OSError("down")
        return CATALOG

    catalog = Catalog(fetch, ttl_s=600, clock=lambda: now[0])
    assert len(catalog.models()) == 4
    now[0] = 100
    catalog.models()
    assert fetches == [0.0]  # cached
    now[0] = 700
    assert len(catalog.models()) == 4  # the refetch failed: the last good list stays
    assert fetches == [0.0, 700]


# --- reading --------------------------------------------------------------------------------


def test_read_handles_a_bare_model_string_and_a_missing_config(tmp_path):
    home = tmp_path / "p"
    home.mkdir()
    (home / "config.yaml").write_text("model: vendor/solo\n")
    profile = Profile("p", "researcher", home)
    assert profile_models.read(profile)["model"] == "vendor/solo"
    assert profile_models.read(profile)["fallbacks"] == []
    with pytest.raises(ModelError) as raised:
        profile_models.read(Profile("q", "researcher", tmp_path / "q"))
    assert raised.value.not_found
