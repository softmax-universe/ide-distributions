"""Each profile's model: what the bridge shows the client and changes on the user's request
(``research_core/contract/CONTRACT.md``, "Profile models").

The model is the profile's own Hermes setting: ``model.default`` and ``model.provider``, with
``fallback_providers`` tried in order when the primary fails, all in the profile's
``config.yaml``. A new install starts with what the profile's shipped ``config.yaml`` names;
``prepare.py`` leaves it alone (but for ``migrate_orchestrator_model``), and the host neither
sets nor reads it. The bridge reads the file directly (fast enough for every page load) and
writes through ``hermes config set`` (Hermes validates the value and keeps the file's comments;
about 1.5 seconds per key). The gateway
reads the file at the start of every run, so a change applies to the next run of every
conversation of that profile; a run already going finishes on the model it started with.

:class:`Catalog` is OpenRouter's public model list, narrowed to the models that take tool calls
(the profiles cannot work without them). A change naming an OpenRouter model that is not on
that list is refused while a list is held (the last one read, when a refresh fails), and
accepted as unverified when none ever was.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_core.prepare import HermesCli, PrepareError, Runner

ROLE_ORCHESTRATOR = "orchestrator"
ROLE_RESEARCHER = "researcher"
OPENROUTER = "openrouter"
MODEL_ID = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+$")
PROVIDER_ID = re.compile(r"^[a-z0-9._-]{2,64}$")
MAX_MODEL_ID = 200
MAX_FALLBACKS = 4
CATALOG_URL = "https://openrouter.ai/api/v1/models"
CATALOG_TTL_S = 600.0
CATALOG_TIMEOUT_S = 10.0


class ModelError(Exception):
    """A request the bridge refuses (``problems`` say why) or a profile it cannot find."""

    def __init__(self, error: str, problems: Iterable[str] = (), *, not_found: bool = False):
        super().__init__(error)
        self.error = error
        self.problems = list(problems)
        self.not_found = not_found


@dataclass(frozen=True)
class Profile:
    name: str
    role: str
    home: Path


def sandbox_profiles(
    profiles_root: Path, orchestrator: str, researchers: Iterable[str]
) -> list[Profile]:
    """The Orchestrator and each Researcher profile, as homes under ``profiles_root``."""
    found = [Profile(orchestrator, ROLE_ORCHESTRATOR, profiles_root / orchestrator)]
    for name in researchers:
        if name and name != orchestrator:
            found.append(Profile(name, ROLE_RESEARCHER, profiles_root / name))
    return found


def find(profiles: Iterable[Profile], name: str) -> Profile:
    for profile in profiles:
        if profile.name == name:
            return profile
    known = ", ".join(p.name for p in profiles)
    raise ModelError(f"no profile {name!r}; the profiles are {known}", not_found=True)


# --- reading ----------------------------------------------------------------------------------


def read(profile: Profile) -> dict[str, Any]:
    """``{profile, role, model, provider, fallbacks: [{provider, model}]}`` from the config."""
    import yaml  # the workspace environment has PyYAML; the Hermes plugin never imports this

    path = profile.home / "config.yaml"
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ModelError(
            f"profile {profile.name} has no config.yaml at {path}", not_found=True
        ) from None
    model = config.get("model")
    if isinstance(model, dict):
        model_id = model.get("default") or model.get("model") or ""
        provider = model.get("provider") or ""
    else:
        model_id, provider = (model if isinstance(model, str) else ""), ""
    fallbacks = [
        {"provider": entry.get("provider") or "", "model": entry.get("model") or ""}
        for entry in config.get("fallback_providers") or []
        if isinstance(entry, dict)
    ]
    return {
        "profile": profile.name,
        "role": profile.role,
        "model": model_id,
        "provider": provider,
        "fallbacks": fallbacks,
    }


# --- a change ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    model: str
    provider: str
    fallbacks: list[dict[str, str]] | None  # None leaves the fallbacks as they are


def parse_change(body: Mapping[str, Any], current: Mapping[str, Any]) -> Change:
    """The requested model, provider and fallbacks, checked; the provider defaults to the
    profile's current one, and an entry of ``fallbacks`` to OpenRouter."""
    problems: list[str] = []
    model = body.get("model")
    if not _model_id(model):
        problems.append("model is a model id such as moonshotai/kimi-k3 (vendor/name)")
    provider = body.get("provider") or current.get("provider") or OPENROUTER
    if not (isinstance(provider, str) and PROVIDER_ID.match(provider)):
        problems.append("provider is a Hermes provider name such as openrouter")
    fallbacks = body.get("fallbacks")
    parsed: list[dict[str, str]] | None = None
    if fallbacks is not None:
        if not isinstance(fallbacks, list) or len(fallbacks) > MAX_FALLBACKS:
            problems.append(f"fallbacks is a list of at most {MAX_FALLBACKS} {{model, provider?}}")
        else:
            parsed = []
            for index, entry in enumerate(fallbacks):
                entry = {"model": entry} if isinstance(entry, str) else entry
                if not isinstance(entry, dict) or not _model_id(entry.get("model")):
                    problems.append(f"fallbacks[{index}].model is a model id (vendor/name)")
                    continue
                entry_provider = entry.get("provider") or OPENROUTER
                if not (isinstance(entry_provider, str) and PROVIDER_ID.match(entry_provider)):
                    problems.append(f"fallbacks[{index}].provider is a Hermes provider name")
                    continue
                parsed.append({"provider": entry_provider, "model": entry["model"]})
    if problems:
        raise ModelError("model change refused", problems)
    return Change(model=model, provider=provider, fallbacks=parsed)


def _model_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= MAX_MODEL_ID and bool(MODEL_ID.match(value))


def check_against_catalog(change: Change, catalog: Catalog) -> bool:
    """Whether every OpenRouter model of the change is on the catalog (``True``), or the catalog
    could not be read (``False``: accepted unverified). Refuses a model it does not list."""
    listed = catalog.ids()
    if listed is None:
        return False
    wanted = [(change.provider, change.model)]
    wanted += [(f["provider"], f["model"]) for f in change.fallbacks or []]
    unknown = [
        model for provider, model in wanted if provider == OPENROUTER and model not in listed
    ]
    if unknown:
        raise ModelError(
            "model change refused",
            [f"{model} is not an OpenRouter model that takes tool calls" for model in unknown],
        )
    return True


def write(
    profile: Profile,
    change: Change,
    current: Mapping[str, Any],
    *,
    hermes: str,
    env: Mapping[str, str],
    run: Runner = subprocess.run,
) -> list[str]:
    """Write what differs through ``hermes config set``; the keys written, in order."""
    cli = HermesCli(hermes, profile.home, env, run)
    written: list[str] = []
    try:
        if change.fallbacks is not None and change.fallbacks != current["fallbacks"]:
            cli.config_set("fallback_providers", change.fallbacks)
            written.append("fallback_providers")
        if change.provider != current["provider"]:
            cli.config_set("model.provider", change.provider)
            written.append("model.provider")
        if change.model != current["model"]:
            cli.config_set("model.default", change.model)
            written.append("model.default")
    except PrepareError as error:
        done = ", ".join(written) or "nothing"
        raise ModelError(f"hermes config set failed after writing {done}", [str(error)]) from None
    return written


# --- the catalog ------------------------------------------------------------------------------


def fetch_openrouter(url: str = CATALOG_URL, timeout: float = CATALOG_TIMEOUT_S) -> list[dict]:
    """OpenRouter's public model list (no key needed), as its ``data`` array."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["data"]


class Catalog:
    """OpenRouter models that take tool calls, cached for ``ttl_s``; the last good list when
    unreachable, ``None`` if there never was one."""

    def __init__(
        self,
        fetch: Callable[[], list[dict]] = fetch_openrouter,
        *,
        ttl_s: float = CATALOG_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl_s = ttl_s
        self._clock = clock
        self._models: list[dict[str, Any]] | None = None
        self._fetched_at = 0.0

    def models(self) -> list[dict[str, Any]] | None:
        now = self._clock()
        if self._models is None or now - self._fetched_at >= self._ttl_s:
            try:
                raw = self._fetch()
            except (OSError, ValueError, KeyError):
                return self._models  # the last good list, or None
            self._models = [summary(entry) for entry in raw if takes_tools(entry)]
            self._fetched_at = now
        return self._models

    def ids(self) -> set[str] | None:
        models = self.models()
        return None if models is None else {model["id"] for model in models}


def takes_tools(entry: Mapping[str, Any]) -> bool:
    return "tools" in (entry.get("supported_parameters") or [])


def summary(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The fields a model picker needs: id, name, context, prices per million tokens, free."""
    pricing = entry.get("pricing") or {}
    prompt = _per_million(pricing.get("prompt"))
    completion = _per_million(pricing.get("completion"))
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "context_length": entry.get("context_length"),
        "usd_per_million_input": prompt,
        "usd_per_million_output": completion,
        "free": prompt == 0 and completion == 0,
    }


def _per_million(value: Any) -> float | None:
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None
