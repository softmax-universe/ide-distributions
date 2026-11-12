"""Step 9: tool checks and repair Tasks (design B.15, B.14; amendments A3.2, A3.4, A4.1, A7.8).

When a check runs. A *full* check is due when none ran yet, when the last one is a day old,
when something marked them due (``workspace.capabilities {check: true}``, or a merge while a
capability is broken, writes the Coworld store's meta ``checks_due``) or when the release
changed (meta ``checks_version`` differs from ``research_core.__version__``). A *quick* check
(logins and CLIs, no builds) is due an hour after the last one or when a Session started since
it (A3.4: the briefing never runs checks, it reads what this step stored). A full run also
counts as a quick one.

What is checked, each a capability row (``capabilities.mark``):

* ``login:player`` through ``platform.whoami("player")``, which also stores the Player's id as
  the workspace store's meta ``player_id`` for the briefing and the guide; ``login:user`` only
  when ``SOFTMAX_USER_CREDENTIAL`` is set (an absent User identity is a fact, not a failure);
* ``cli:coworld`` and ``cli:softmax``: ``<venv>/bin/<cli> --help`` when the workspace
  environment has the binary, else the distribution installed in this interpreter (the watcher
  runs in that environment); the installed versions are compared with ``uv.lock`` of the core
  checkout and stored for the briefing as the workspace store's meta ``cli_versions``
  (``{"<cli>": {"installed", "lock"}, "lock_refreshed": <date>}``, A7.8: a stale CLI is reported,
  never a broken capability);
* credits: ``platform.credits`` into the Coworld store's meta ``credits`` for the briefing (a
  fact, not a capability);
* full runs only: the mixin's toolchains are installed, then its ``build``, ``check`` and
  ``evaluate`` commands run in a temporary
  worktree of ``main`` (``evaluate`` on downloaded results under ``documents/xp`` when there are
  any, else skipped with "no results to evaluate"), and ``upload``: the mixin's own ``upload``
  command if it has one, then the starter artifact is really sent through ``upload_file_policy``
  when the platform is the fake or the route was never verified; a real platform whose
  upload already passed records "skipped: real upload not attempted" and keeps its mark.

A failed check goes through ``capabilities.report_failure``: the capability is broken and one
``high`` maintenance Task repairs it (repeats join it). The ``capability.failed`` event then
reaches :func:`handle_capability_failed`, which adds ``task_deps`` from every open Task whose
type needs the capability (improvement needs build, check, evaluate and upload; research needs
evaluate; ideation and maintenance need nothing). :func:`handle_task_failed` counts failed
repair Tasks per capability (meta ``repair_failures:<name>``, reset when the check passes) and
emits ``repair.failed_twice`` at the second; :func:`handle_repair_failed_twice` writes the
notice. After a full run with build, check and upload ok, a Coworld not yet set up is marked
set up (``coworlds.mark_set_up``, event ``coworld.set_up``; B.14).

Bookkeeping written at the end of a run: Coworld store meta ``checks_quick_at`` (every run),
``checks_at`` and ``checks_version`` (full runs), ``checks_due`` cleared; the event
``checks.ran`` summarises the run for the log.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import research_core
from research_core import environment_guide, gitops
from research_core import mixin as mixin_module
from research_core.platform import credits as credits_module
from research_core.platform.errors import PlatformError
from research_core.platform.protocol import WhoAmI, with_retries
from research_core.store import capabilities, coworlds, notices, tasks
from research_core.store._common import now_iso, parse_display_id, parse_iso
from research_core.store.db import Store
from research_core.store.errors import StoreError
from research_core.tools.base import PLAYER_ID_META, player_policy_name
from research_core.watcher.merge import CHECKS_DUE, install_toolchains, temporary_worktree
from research_core.watcher.resume import emit_event

if TYPE_CHECKING:  # pragma: no cover
    from research_core.store.events import Event
    from research_core.watcher.context import TickContext

FULL_INTERVAL = timedelta(hours=24)
QUICK_INTERVAL = timedelta(hours=1)
CHECKS_AT = "checks_at"
CHECKS_QUICK_AT = "checks_quick_at"
CHECKS_VERSION = "checks_version"
CREDITS_KEY = "credits"
CLI_VERSIONS_KEY = "cli_versions"
REPAIR_FAILURES = "repair_failures:"
REPAIR_NOTICE_AT = 2
COMMAND_BUDGET_S = 240
CLI_TIMEOUT_S = 60
CHECK_WORKTREE = "tool-check"
RESULTS_ENV = "XP_RESULTS"
UPLOAD_NAME_SUFFIX = "tool-check"
# What each Task type needs (B.15): a broken capability blocks these types' open Tasks.
NEEDS: dict[str, tuple[str, ...]] = {
    "improvement": ("build", "check", "evaluate", "upload"),
    "research": ("evaluate",),
    "ideation": (),
    "maintenance": (),
}
SETUP_CAPABILITIES = ("build", "check", "upload")
# Retries between platform calls sleep through this; tests replace it.
_sleep: Callable[[float], None] = time.sleep


def run(ctx: TickContext) -> dict[str, Any]:
    if ctx.store is None:
        return {"skipped": "no active Coworld"}
    mode = due_mode(ctx)
    if mode is None:
        return {"skipped": "not due", "checks_at": ctx.store.get_meta(CHECKS_AT)}
    results = run_all(ctx, quick=mode == "quick")
    return {"mode": mode, **summarise(results)}


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    """``ok``, ``broken`` (capabilities), ``unavailable`` (facts such as credits), ``skipped``."""
    return {
        "ok": [r["name"] for r in results if r["ok"] is True],
        "broken": [r["name"] for r in results if r["ok"] is False and r["capability"]],
        "unavailable": [r["name"] for r in results if r["ok"] is False and not r["capability"]],
        "skipped": [r["name"] for r in results if r["ok"] is None],
    }


def due_mode(ctx: TickContext) -> str | None:
    """``"full"``, ``"quick"`` or ``None`` (see the module docstring)."""
    store = ctx.store
    last_full = store.get_meta(CHECKS_AT)
    if (
        last_full is None
        or store.get_meta(CHECKS_DUE)
        or store.get_meta(CHECKS_VERSION) != research_core.__version__
        or ctx.now - parse_iso(last_full) >= FULL_INTERVAL
    ):
        return "full"
    last_quick = store.get_meta(CHECKS_QUICK_AT) or last_full
    if ctx.now - parse_iso(last_quick) >= QUICK_INTERVAL:
        return "quick"
    if _session_started_since(ctx.wstore, last_quick):
        return "quick"
    return None


def run_all(ctx: TickContext, *, quick: bool = False) -> list[dict[str, Any]]:
    """Run the checks, record every result and the bookkeeping; return the results.

    Each result is ``{"name", "capability", "kind", "ok" (True, False or None for skipped),
    "output", "command", "note"?}`` plus ``maintenance_task`` when a failure opened or joined one.
    """
    store = ctx.store
    results = login_checks(ctx)
    cli_results, cli_versions = cli_checks(ctx)
    results.extend(cli_results)
    ctx.wstore.set_meta(CLI_VERSIONS_KEY, json.dumps(cli_versions, sort_keys=True))
    results.append(credits_check(ctx))
    loaded = None
    if not quick:
        mixin_results, loaded = mixin_checks(ctx)
        results.extend(mixin_results)
    for result in results:
        _record(ctx, result)
    if not quick:
        _mark_set_up(ctx, results, loaded)
    stamp = now_iso(lambda: ctx.now)
    store.set_meta(CHECKS_QUICK_AT, stamp)
    if not quick:
        store.set_meta(CHECKS_AT, stamp)
        store.set_meta(CHECKS_VERSION, research_core.__version__)
        store.set_meta(CHECKS_DUE, "")
    emit_event(
        store,
        ctx.actor,
        "checks.ran",
        "quick" if quick else "full",
        {
            "mode": "quick" if quick else "full",
            **summarise(results),
            "skipped": {r["name"]: r.get("note") for r in results if r["ok"] is None},
        },
    )
    return results


# --- the checks ------------------------------------------------------------------------------


def login_checks(ctx: TickContext) -> list[dict[str, Any]]:
    results = [
        _platform_check(
            "login:player",
            "GET /whoami (player)",
            lambda: _read_player(ctx),
            lambda who: f"{who.kind} {who.subject_id}",
        )
    ]
    if ctx.env.get("SOFTMAX_USER_CREDENTIAL"):
        results.append(
            _platform_check(
                "login:user",
                "GET /whoami (user)",
                lambda: ctx.platform.whoami("user"),
                lambda who: f"{who.kind} {who.subject_id} {who.email or ''}".rstrip(),
            )
        )
    return results


def _read_player(ctx: TickContext) -> WhoAmI:
    """``whoami``, keeping the Player's id for the briefing and the guide, which cannot ask."""
    who = ctx.platform.whoami("player")
    if who.kind == "player" and who.subject_id:
        ctx.wstore.set_meta(PLAYER_ID_META, who.subject_id)
    return who


def cli_checks(ctx: TickContext) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Each CLI's result, and the ``cli_versions`` document for the briefing."""
    checkout = environment_guide.core_checkout(ctx.env)
    lock = environment_guide.lock_versions(checkout)
    versions: dict[str, Any] = {"lock_refreshed": environment_guide.lock_refreshed(checkout)}
    results = []
    for name, distribution in environment_guide.CLIS:
        installed = _installed_version(distribution)
        binary = ctx.ws.venv / "bin" / name
        command = f"{binary} --help"
        if binary.exists():
            ok, output = _run_version(binary)
        elif installed is not None:
            ok, output = True, f"{distribution} {installed} (importlib.metadata; {binary} absent)"
        else:
            ok, output = False, f"{distribution} is not installed and {binary} does not exist"
        results.append(
            _result(f"cli:{name}", "cli", ok, output, command=command, capability=f"cli:{name}")
        )
        versions[name] = {"installed": installed, "lock": lock.get(distribution)}
    return results, versions


def credits_check(ctx: TickContext) -> dict[str, Any]:
    """The Player's credits into meta ``credits``; a fact for the briefing, not a capability."""
    try:
        credits = with_retries(ctx.platform.credits, sleep=_sleep)
    except PlatformError as error:
        return _result("credits", "info", False, _platform_error_text(error), capability=None)
    ctx.store.set_meta(
        CREDITS_KEY,
        json.dumps(
            {
                "balance": credits.balance,
                "max": credits.max,
                "next_refill": credits.next_refill_at,
                "spent_this_period": credits.spent_this_period,
                "checked": now_iso(lambda: ctx.now),
            }
        ),
    )
    return _result("credits", "info", True, credits_module.describe(credits), capability=None)


def mixin_checks(ctx: TickContext) -> tuple[list[dict[str, Any]], mixin_module.Mixin | None]:
    """Real runs of the mixin's commands at ``main`` in a temporary worktree, then the upload."""
    coworld = ctx.coworld
    code = coworld.code
    if (
        not (code / ".git").exists()
        or not (coworld.mixin / mixin_module.FRONT_MATTER_FILE).is_file()
    ):
        note = "no mixin under code/ yet; the setup wake provides it"
        return [_result("mixin", "command", None, note, capability=None, note=note)], None
    try:
        loaded = mixin_module.load(coworld.mixin)
    except mixin_module.MixinError as error:
        output = "MIXIN.md is not usable: " + "; ".join(error.problems)
        return [_result("build", "command", False, output, capability="build")], None
    results: list[dict[str, Any]] = []
    install_toolchains(ctx, loaded)
    main_sha = _main_sha(code)
    with temporary_worktree(code, coworld.worktrees / CHECK_WORKTREE, main_sha) as tree:
        for name in ("build", "check"):
            results.append(_command_check(ctx, loaded, name, tree))
        results.append(_evaluate_check(ctx, loaded, tree))
        built = next(r for r in results if r["name"] == "build")
        results.append(_upload_check(ctx, loaded, tree, built_ok=built["ok"] is True))
    return results, loaded


def _command_check(
    ctx: TickContext,
    loaded: mixin_module.Mixin,
    name: str,
    tree: Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = mixin_module.run_command(
        loaded,
        name,
        cwd=tree,
        env=env,
        budget_s=COMMAND_BUDGET_S,
        log_dir=ctx.coworld.documents / "logs",
    )
    if not result.finished:
        _terminate(result.pid)
        ok, note = False, f"{name} did not finish within {COMMAND_BUDGET_S} s"
    elif result.ok:
        ok, note = True, None
    else:
        ok, note = False, f"{name} failed with exit code {result.exit_code}"
    output = _log_text(result.log_path) or result.output_tail
    if note:
        output = f"{note}\n{output}"
    return _result(
        name, "command", ok, output, command=result.command, capability=name, log=result.log_path
    )


def _evaluate_check(ctx: TickContext, loaded: mixin_module.Mixin, tree: Path) -> dict[str, Any]:
    if "evaluate" not in loaded.commands:
        note = "the mixin declares no evaluate command"
        return _result("evaluate", "command", None, note, capability="evaluate", note=note)
    results_dir = _results_dir(ctx.coworld.documents / "xp")
    if results_dir is None:
        note = "no results to evaluate"
        return _result("evaluate", "command", None, note, capability="evaluate", note=note)
    env = {**os.environ, **ctx.env, RESULTS_ENV: str(results_dir)}
    return _command_check(ctx, loaded, "evaluate", tree, env)


def upload_probe_name(coworld_id: str, player_id: str | None) -> str:
    """The upload check's policy name: ``<coworld>-tool-check-<player>``.

    The Player's id, which ``login:player`` stores earlier in the same tick, keeps the name to
    this account (``player_policy_name``); without one the bare name stands.
    """
    return player_policy_name(f"{coworld_id}-{UPLOAD_NAME_SUFFIX}", player_id)


def _upload_check(
    ctx: TickContext, loaded: mixin_module.Mixin, tree: Path, *, built_ok: bool
) -> dict[str, Any]:
    if not built_ok:
        note = "skipped: the build failed, nothing to upload"
        return _result("upload", "route", None, note, capability="upload", note=note)
    artifact = tree / str(loaded.artifact.get("path", ""))
    if not artifact.is_file():
        output = f"the build produced no artifact at {loaded.artifact.get('path')}"
        return _result("upload", "route", False, output, capability="upload")
    outputs = []
    if "upload" in loaded.commands:
        command_result = _command_check(ctx, loaded, "upload", tree)
        if command_result["ok"] is not True:
            return {**command_result, "name": "upload", "kind": "route"}
        outputs.append(command_result["output"])
    send = ctx.env.get("IDE_PLATFORM", "real").lower() == "fake" or not _was_ok(ctx.store, "upload")
    if not send:
        note = "skipped: real upload not attempted (the route was verified before)"
        return _result("upload", "route", None, note, capability="upload", note=note)
    name = upload_probe_name(
        ctx.coworld.coworld_id, ctx.env.get("SOFTMAX_PLAYER_ID") or ctx.meta(PLAYER_ID_META)
    )
    try:
        version = with_retries(
            lambda: (
                ctx.platform.upload_image_policy(
                    name=name, archive_path=artifact, tags={"ide": UPLOAD_NAME_SUFFIX}
                )
                if loaded.artifact["kind"] == "image"
                else ctx.platform.upload_file_policy(
                    name=name, content=artifact.read_bytes(), tags={"ide": UPLOAD_NAME_SUFFIX}
                )
            ),
            sleep=_sleep,
        )
    except PlatformError as error:
        return _result("upload", "route", False, _platform_error_text(error), capability="upload")
    outputs.append(f"uploaded {artifact.name} as policy version {version.id} ({version.label})")
    return _result(
        "upload",
        "route",
        True,
        "\n".join(outputs),
        command="POST /stats/policies/files/upload",
        capability="upload",
        policy_version_id=version.id,
    )


# --- recording ---------------------------------------------------------------------------------


def _record(ctx: TickContext, result: dict[str, Any]) -> None:
    name, store = result["capability"], ctx.store
    if name is None or result["ok"] is None:
        return
    if result["ok"]:
        capabilities.mark(
            store,
            ctx.actor,
            name,
            status="ok",
            output=result["output"],
            command=result["command"],
            kind=result["kind"],
        )
        store.set_meta(f"{REPAIR_FAILURES}{name}", "0")
        return
    report = capabilities.report_failure(
        store,
        ctx.actor,
        name,
        output=result["output"],
        command=result["command"],
        kind=result["kind"],
    )
    task = report.get("maintenance_task")
    result["maintenance_task"] = None if task is None else task["id"]


def _mark_set_up(
    ctx: TickContext, results: list[dict[str, Any]], loaded: mixin_module.Mixin | None
) -> None:
    """B.14: a Coworld counts as set up once build, check and the upload passed."""
    if loaded is None:
        return
    row = coworlds.read(ctx.wstore, ctx.coworld.coworld_id)
    if row["set_up_at"] is not None:
        return
    passed = {r["name"] for r in results if r["ok"] is True}
    if not all(name in passed for name in SETUP_CAPABILITIES):
        return
    coworlds.mark_set_up(
        ctx.wstore,
        ctx.actor,
        ctx.coworld.coworld_id,
        mixin_version=loaded.coworld_version or "unversioned",
        tool_check_status="ok",
    )


# --- handlers ----------------------------------------------------------------------------------


def handle_capability_failed(ctx: TickContext, event: Event, store: Store) -> dict[str, Any]:
    """``capability.failed``: make sure a repair Task exists and link the Tasks that need it."""
    name = event.subject
    record = capabilities.read(store, name)
    if record is None:
        return {"skipped": f"capability {name} has no row"}
    repair = record["maintenance_task_id"]
    if repair is None or not _is_open(store, repair):
        report = capabilities.report_failure(
            store,
            ctx.actor,
            name,
            output=record["output"] or "",
            command=record["command"],
            kind=record["kind"],
        )
        task = report.get("maintenance_task")
        if task is None:
            return {"capability": name, "repair": None, "linked": []}
        repair = parse_display_id(task["id"])[1]
    linked = link_dependents(store, ctx, name, repair)
    return {"capability": name, "repair": f"T-{repair}", "linked": linked}


def link_dependents(store: Store, ctx: TickContext, name: str, repair: int) -> list[str]:
    """``task_deps`` rows from every open Task whose type needs ``name`` to its repair Task."""
    linked: list[str] = []
    for task in tasks.list(store):
        n = parse_display_id(task["id"])[1]
        if (
            task["status"] not in tasks.OPEN_STATUSES
            or n == repair
            or name not in NEEDS.get(task["type"], ())
            or f"T-{repair}" in task["depends_on"]
        ):
            continue
        try:
            tasks.link(store, ctx.actor, n, [repair])
        except StoreError as error:
            ctx.note(f"{task['id']} could not depend on T-{repair}: {error}")
            continue
        linked.append(task["id"])
    return linked


def handle_task_failed(ctx: TickContext, event: Event, store: Store) -> dict[str, Any]:
    """``task.failed``: count failed repairs per capability; the second raises
    ``repair.failed_twice``. Any other failed Task raises ``task.failure_review``, so someone reads
    what its Researcher wrote, unless the watcher's own give-up already woke the Orchestrator."""
    task = tasks.read(store, event.subject)
    name = task["capability"]
    if not name:
        gave_up = ctx.wstore.one(
            "SELECT 1 FROM events WHERE kind = 'recovery.failed' AND subject = ?"
            " AND json_extract(payload, '$.coworld_id') = ?",
            (task["id"], ctx.coworld.coworld_id),
        )
        if gave_up:
            return {"skipped": f"recovery.failed covers {task['id']}"}
        failure = task["failure"] or {}
        emit_event(
            store,
            ctx.actor,
            "task.failure_review",
            task["id"],
            {
                "task": task["id"],
                "what_failed": failure.get("what_failed"),
                "next_step": failure.get("next_step"),
            },
        )
        return {"review": task["id"]}
    key = f"{REPAIR_FAILURES}{name}"
    count = int(store.get_meta(key) or 0) + 1
    store.set_meta(key, str(count))
    if count == REPAIR_NOTICE_AT:
        failure = task["failure"] or {}
        emit_event(
            store,
            ctx.actor,
            "repair.failed_twice",
            name,
            {
                "capability": name,
                "task": task["id"],
                "failures": count,
                "what_failed": failure.get("what_failed"),
                "next_step": failure.get("next_step"),
            },
        )
    return {"capability": name, "task": task["id"], "failures": count}


def handle_repair_failed_twice(ctx: TickContext, event: Event, store: Store) -> dict[str, Any]:
    """``repair.failed_twice``: the user hears about it once (B.15)."""
    payload = event.payload
    name = payload.get("capability") or event.subject
    task = payload.get("task")
    text = (
        f"Repairing the {name} capability has failed twice"
        + (f" (last attempt {task})" if task else "")
        + ". Tasks that need it wait until it is fixed; the Orchestrator may need your help."
    )
    if payload.get("what_failed"):
        text += f" Last failure: {payload['what_failed']}"
    notice = notices.add(
        ctx.wstore,
        ctx.actor,
        text=text,
        refs=[task] if task else [],
        urgency="high",
        coworld_id=ctx.coworld.coworld_id if ctx.coworld else None,
    )
    return {"notice": notice["id"], "capability": name}


# --- helpers -----------------------------------------------------------------------------------


def _result(
    name: str,
    kind: str,
    ok: bool | None,
    output: str,
    *,
    capability: str | None,
    command: str | None = None,
    note: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "capability": capability,
        "kind": kind,
        "ok": ok,
        "output": output,
        "command": command,
    }
    if note is not None:
        result["note"] = note
    result.update(
        {key: str(value) if isinstance(value, Path) else value for key, value in extra.items()}
    )
    return result


def _platform_check(
    name: str, route: str, call: Callable[[], Any], describe: Callable[[Any], str]
) -> dict[str, Any]:
    try:
        answer = with_retries(call, sleep=_sleep)
    except PlatformError as error:
        return _result(
            name, "login", False, _platform_error_text(error), capability=name, command=route
        )
    return _result(name, "login", True, describe(answer), capability=name, command=route)


def _platform_error_text(error: PlatformError) -> str:
    parts = [error.detail]
    if error.route:
        parts.append(f"route {error.route}")
    if error.status is not None:
        parts.append(f"status {error.status}")
    if error.request_id:
        parts.append(f"request {error.request_id}")
    return "; ".join(parts)


def _installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _run_version(binary: Path) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [str(binary), "--help"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=CLI_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"{binary} --help did not run: {error}"
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        return False, f"{binary} --help exited {completed.returncode}: {output}"
    return True, output


def _results_dir(xp_dir: Path) -> Path | None:
    """The downloaded-results folder when it holds at least one ``results.json``."""
    if not xp_dir.is_dir():
        return None
    return xp_dir if any(xp_dir.rglob("results.json")) else None


def _was_ok(store: Store, name: str) -> bool:
    record = capabilities.read(store, name)
    return record is not None and record["status"] == "ok"


def _is_open(store: Store, task_id: int) -> bool:
    row = store.one("SELECT status FROM tasks WHERE id = ?", (task_id,))
    return row is not None and row["status"] in tasks.OPEN_STATUSES


def _session_started_since(wstore: Store, since: str) -> bool:
    row = wstore.one(
        "SELECT 1 FROM events WHERE kind = 'session.started' AND at > ? LIMIT 1", (since,)
    )
    return row is not None


def _main_sha(code: Path) -> str:
    return gitops.rev_parse(code, "refs/heads/main")


def _log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _terminate(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
