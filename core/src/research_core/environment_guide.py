"""The environment guide (design B.17, the fallback the profile generates itself).

``render`` writes the one page an agent reads when unsure about its surroundings: where it is,
which commits it runs, the two Python environments, what is writable, which credentials and
CLIs are present, how to reach the user, how sleep and wake work, resource limits and the last
capability check. It names credentials, never their values, and never a store path (B.10).

The small fact readers at the bottom (``core_checkout``, ``image_info``, ``source_commit``,
``cli_versions``, ``lock_versions``) are shared with ``workspace.doctor``. Stdlib only.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import research_core
from research_core import gitops
from research_core.prepare import CORE_PATH_FILE

if TYPE_CHECKING:  # pragma: no cover
    from research_core.tools.base import ToolContext

# The CLIs the workspace environment provides, with the distribution each version comes from.
CLIS: tuple[tuple[str, str], ...] = (
    ("coworld", "coworld"),
    ("softmax", "softmax-cli"),
    ("research", "research-core"),
)
WATCHER_TICK_S = 60
WORKSPACE_TOOL_BUDGET_S = 400
MIXIN_COMMAND_BUDGET_S = 240


def render(ctx: ToolContext) -> str:
    """The guide as Markdown for the calling role."""
    from research_core.store import capabilities

    ws = ctx.workspace
    env = ctx.env
    coworld = ctx.coworld_or_none()
    checkout = core_checkout(env)
    lines: list[str] = ["# Environment guide", ""]
    lines += _where_you_are(ws.root, coworld)
    lines += _commits(env, checkout)
    lines += _environments(ws.venv_python, ws.tools)
    lines += _paths(checkout, coworld)
    lines += _credentials(env, ctx.player_id())
    lines += _clis(ws.venv)
    lines += _reaching_the_user(ctx.role)
    lines += _sleep_and_wake()
    lines += _limits(env)
    status = capabilities.status(ctx.store) if coworld is not None else []
    lines += _capability_check(status)
    return "\n".join(lines).rstrip() + "\n"


# --- sections ---------------------------------------------------------------------------------


def _where_you_are(root: Path, coworld) -> list[str]:
    lines = ["## Where you are", "", f"- Workspace root: `{root}` (shared by every profile)."]
    if coworld is None:
        lines.append("- This session has no Coworld. Open a new session for the selected game.")
    else:
        lines += [
            f"- Session Coworld: `{coworld.coworld_id}`, folder `{coworld.root}`:",
            "  - `code/`: the policy repository (git; `main` holds the policy and `mixin/`).",
            "  - `source/`: the Coworld's public repository at the release-pinned commit.",
            "  - `worktrees/task-<n>/` and `worktrees/orch-<n>/`: one worktree per piece of work.",
            "  - `documents/`: your own notes and files (command logs under `documents/logs/`). "
            "It is in no git repository, so nothing there is committed; it stays through sleep "
            "and restarts.",
        ]
    lines.append("")
    return lines


def _commits(env: Mapping[str, str], checkout: Path | None) -> list[str]:
    image = image_info(env)
    source = source_commit(checkout) if checkout else None
    return [
        "## Image and source",
        "",
        f"- Hosted image: {image or 'unknown (not a hosted profile, or no runtime marker)'}.",
        f"- Profile source checkout: `{checkout}` at commit {source or 'unknown'}."
        if checkout
        else "- Profile source checkout: unknown (research_core is not running from a checkout).",
        "",
    ]


def _environments(venv_python: Path, tools: Path) -> list[str]:
    return [
        "## Two Python environments",
        "",
        "- Hermes's own interpreter runs the plugin and the store-only tools (goal, task, claim, "
        "directive, settings, ui). Nothing else runs there.",
        f"- The workspace environment `{venv_python}` runs this tool, the `research` CLI, the "
        "platform tools, mixin commands and the watcher. It holds `coworld`, `softmax`, "
        "`numpy` and `PyYAML`.",
        f"- Mixin-declared toolchains live under `{tools}/<kind>-<version>`: `python` ones are "
        "installed for you, any other kind (Nim) by hand.",
        "",
    ]


def _paths(checkout: Path | None, coworld) -> list[str]:
    lines = ["## Writable and read-only paths", ""]
    if coworld is not None:
        lines.append(
            f"- Writable: everything under `{coworld.root}` except its bookkeeping folder: "
            "`code/` (after setup, commit on a branch in a worktree, never on `main` directly), "
            "`documents/` and `worktrees/`. `source/` is a clone to read, not to edit."
        )
    else:
        lines.append("- Writable: the session Coworld's `code/`, `documents/` and `worktrees/`.")
    lines += [
        f"- Read-only: the profile source checkout `{checkout or '(unknown)'}` (plugin, store "
        "library and schema, tools, prompts). Report a bug there instead of "
        "patching it; `config.yaml` is writable but `prepare.py` re-applies the protected keys.",
        "- Shipped skills live in that checkout, whatever Hermes's skill index says about "
        "fixing them. Where it is read-only, as in hosting, `skill_manage` `patch`, `write_file` "
        "and `delete` on one fail with a file system error and change nothing. On a writable "
        "checkout (a local install) they change the shipped files for every profile using it: "
        "do not. `create` under a shipped name is refused. Record a fix as a claim tagged "
        "`lesson` and report the fault; game steps go in a mixin skill, committed on a branch. "
        "`create` with a new name makes a skill in this profile's folder only, which `patch` "
        "can change; it stays through sleep, restarts and releases.",
        "- Records (Goals, Tasks, Claims, Experiments, Versions, settings) live in the store and "
        "change only through the record tools. Direct edits are detected and reported.",
        "",
    ]
    return lines


def _credentials(env: Mapping[str, str], player_id: str | None) -> list[str]:
    player = "present" if env.get("SOFTMAX_PLAYER_CREDENTIAL") else "absent"
    user = "provisioned" if env.get("SOFTMAX_USER_CREDENTIAL") else "not provisioned"
    api = env.get("COGAMES_API_URL") or "https://softmax.com/api"
    return [
        "## Credentials and identities (names only; values are never shown)",
        "",
        f"- Player identity: `{player_id or 'unknown'}` (the workspace's Player); "
        f"credential {player}.",
        f"- User identity (the user's own account, Orchestrator only): {user}."
        + (
            ""
            if env.get("SOFTMAX_USER_CREDENTIAL")
            else " User-identity actions report 'User identity not provisioned'."
        ),
        f"- Platform API: `{api}`.",
        f"- Hermes API server key: {'present' if env.get('API_SERVER_KEY') else 'absent'} "
        "(the watcher reads it from the profile's `.env`).",
        "",
    ]


def _clis(venv: Path) -> list[str]:
    lines = ["## CLIs available", ""]
    for entry in cli_versions(venv):
        where = f"`{entry['path']}`" if entry["path"] else "not on the workspace environment path"
        version = entry["version"] or "not installed"
        lines.append(f"- `{entry['name']}` {version} ({where}).")
    lines.append(
        f"- `git` {'present' if shutil.which('git') else 'absent'}, `uv` "
        f"{'present' if shutil.which('uv') else 'absent'}."
    )
    lines.append("")
    return lines


def _reaching_the_user(role: str) -> list[str]:
    lines = ["## Reaching the user", ""]
    if role == "orchestrator":
        lines += [
            "- In a chat Session, answer in the conversation.",
            "- From a wake or any background Session, nobody reads the chat: `ui.notify` informs "
            "and never asks, and `ui.ask` asks what only the user can answer.",
        ]
    else:
        lines += [
            "- Researchers never talk to the user directly: `task_ask` puts a question on the "
            "Task for the Orchestrator, and the Task's result reports the outcome.",
        ]
    lines.append("")
    return lines


def _sleep_and_wake() -> list[str]:
    return [
        "## Sleep and wake",
        "",
        f"- The watcher runs every {WATCHER_TICK_S} seconds (`research serve`, with no model). "
        "It starts Task runs, resolves waits, recovers dead runs and runs capability checks.",
        "- After `task_wait`, end your run; the watcher continues the Task when the wait resolves, "
        "usually in a new Session with the Task's record, worktree and recent messages. Nothing "
        "you started in the foreground survives the end of a run.",
        "- Long mixin commands run detached with their output in a log file; a result with "
        "`background` means the command outlived the tool budget and is still running.",
        "- The sandbox may sleep while nothing runs; the watcher's heartbeat says when it last "
        "ticked (`workspace.doctor`).",
        "",
    ]


def _limits(env: Mapping[str, str]) -> list[str]:
    lines = [
        "## Limits",
        "",
        f"- A workspace tool call has {WORKSPACE_TOOL_BUDGET_S} s; a mixin command "
        f"{MIXIN_COMMAND_BUDGET_S} s before it continues in the background.",
        "- Claim summaries are at most 40 words and bodies 500; briefings are short on purpose.",
    ]
    if env.get("TMPDIR"):
        lines.append(f"- Temporary files: `{env['TMPDIR']}` (`TMPDIR`).")
    lines.append("")
    return lines


def _capability_check(status: list[dict[str, Any]]) -> list[str]:
    lines = ["## Last capability check", ""]
    if not status:
        lines.append(
            "- No capability check is recorded yet; setup runs the first one. "
            "`workspace.capabilities {check: true}` asks the watcher for a new one."
        )
    else:
        counts: dict[str, int] = {}
        newest = ""
        for row in status:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            newest = max(newest, row.get("last_check") or "")
        summary = ", ".join(f"{n} {name}" for name, n in sorted(counts.items()))
        lines.append(f"- {summary}; newest check at {newest or 'unknown'}.")
        broken = [row["name"] for row in status if row["status"] == "broken"]
        if broken:
            lines.append(f"- Broken: {', '.join(broken)} (each has a repair Task).")
    lines.append("")
    return lines


# --- runtime facts (shared with workspace.doctor) ---------------------------------------------


def core_checkout(env: Mapping[str, str] = os.environ) -> Path | None:
    """The release checkout: from ``<HERMES_HOME>/local/core-path.txt`` when a profile wrote it,
    else the checkout ``research_core`` is imported from; None when it runs from a wheel."""
    if env.get("HERMES_HOME"):
        marker = Path(env["HERMES_HOME"]) / CORE_PATH_FILE
        try:
            core_src = Path(marker.read_text().strip())
        except OSError:
            core_src = None
        if core_src is not None and core_src.name == "src":
            return core_src.parent.parent
    candidate = Path(research_core.__file__).resolve().parents[3]
    if (candidate / "core" / "pyproject.toml").is_file():
        return candidate
    return None


def image_info(env: Mapping[str, str] = os.environ) -> str | None:
    """The hosted image from ``<HERMES_HOME>/.softmax-runtime.json`` (``image``), compactly."""
    if not env.get("HERMES_HOME"):
        return None
    marker = Path(env["HERMES_HOME"]) / ".softmax-runtime.json"
    try:
        image = json.loads(marker.read_text()).get("image")
    except (OSError, ValueError, AttributeError):
        return None
    if image is None:
        return None
    if isinstance(image, dict):
        for key in ("digest", "ref", "tag", "commit", "name"):
            if image.get(key):
                return str(image[key])
        return json.dumps(image, sort_keys=True)[:200]
    return str(image)


def source_commit(checkout: Path | None) -> str | None:
    """``git rev-parse --short HEAD`` of the checkout, or None when it is not a repository."""
    if checkout is None:
        return None
    # The release checkout is read-only in hosting and often owned by another user (root in
    # the image, the runner in CI); git refuses such a repository as "dubious ownership" unless
    # it is marked safe. Reading HEAD runs nothing from the repository, so mark it for this call.
    result = gitops.git(
        checkout,
        "-c",
        "safe.directory=*",
        "rev-parse",
        "--short",
        "HEAD",
        check=False,
        timeout_s=10,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def cli_versions(venv: Path | None = None) -> list[dict[str, Any]]:
    """Each CLI with its installed distribution version and its path on the venv, if any."""
    entries = []
    for name, distribution in CLIS:
        try:
            version: str | None = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = None
        path = venv / "bin" / name if venv is not None else None
        entries.append(
            {
                "name": name,
                "distribution": distribution,
                "version": version,
                "path": str(path) if path is not None and path.exists() else None,
            }
        )
    return entries


def lock_versions(checkout: Path | None) -> dict[str, str]:
    """``{distribution: version}`` from the checkout's ``uv.lock``; empty when unreadable."""
    if checkout is None:
        return {}
    try:
        lock = tomllib.loads((checkout / "uv.lock").read_text())
    except (OSError, ValueError):
        return {}
    return {
        package["name"]: package["version"]
        for package in lock.get("package", [])
        if isinstance(package, dict) and "name" in package and "version" in package
    }


def lock_refreshed(checkout: Path | None) -> str | None:
    """The date of the last commit that touched ``uv.lock`` (A7.8), or None."""
    if checkout is None:
        return None
    result = gitops.git(
        checkout, "log", "-1", "--format=%cs", "--", "uv.lock", check=False, timeout_s=10
    )
    return result.stdout.strip() or None if result.returncode == 0 else None
