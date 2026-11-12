"""The ``workspace`` tool (design C.2, B.11, B.15, B.17): the Coworld switch, change worktrees,
``doctor``, the capability list and the environment guide.

Executor ``workspace`` (A3.1): it clones repositories, reads the platform manifest and parses
YAML, so it runs in the workspace environment. ``capabilities {check: true}`` never runs the
checks here (A3.2, A3.4): it marks them due in the Coworld store's ``meta`` and the watcher runs
them on its next tick.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from research_core import coworld_setup, environment_guide, gitops
from research_core.store import capabilities
from research_core.store._common import now_iso, parse_iso
from research_core.store.errors import SchemaMismatch, StoreError
from research_core.tools import _args as a
from research_core.tools.base import ActionSpec, ToolCall, ToolError, ToolSpec
from research_core.transitions import durable_tool

if TYPE_CHECKING:  # pragma: no cover
    from research_core.store.db import Store
    from research_core.tools.base import ToolContext

ORCHESTRATOR = frozenset({"orchestrator"})
BOTH = frozenset({"orchestrator", "researcher"})
CHECKS_DUE_KEY = "checks_due"
HEARTBEAT_KEY = "heartbeat"
HEARTBEAT_STALE_S = 180
_CHANGE_WORKTREE = re.compile(r"^orch-([1-9][0-9]*)$")


def handle(call: ToolCall) -> dict:
    ctx = call.ctx
    if call.action == "coworld_switch":
        return coworld_setup.switch(
            ctx,
            call.get("coworld"),
            cites=call.get("cites"),
            source=call.get("source"),
            commit=call.get("commit"),
            pending_request=call.get("pending_request"),
        )
    if call.action == "worktree_open":
        return open_worktree(ctx, call.get("name"))
    if call.action == "change_complete":
        if ctx.coordinator_instance is None:
            from research_core.transitions import submit

            return submit(call)
        return complete_change(
            ctx, call.get("worktree"), call.get("commit"), call.get("summary") or ""
        )
    if call.action == "doctor":
        return doctor(ctx)
    if call.action == "capabilities":
        return list_capabilities(ctx, check=bool(call.get("check")))
    if call.action == "environment_guide":
        return {"guide": environment_guide.render(ctx)}
    if call.action == "identity":
        return identity_action(call)
    raise AssertionError(call.action)


def identity_action(call: ToolCall) -> dict[str, Any]:
    """Read the cogent's identity, or write it under the write-once rules of store.identity."""
    from research_core.store import identity, sessions

    ctx = call.ctx
    name, cites = call.get("name"), call.get("cites")
    met, confirm, offered = call.get("met_user"), call.get("confirm"), call.get("offer_made")
    wrote = False
    writing = bool(name is not None or confirm or met or offered)
    hermes_home = ctx.env.get("HERMES_HOME")
    state_db = Path(hermes_home).expanduser() / "state.db" if hermes_home else None
    try:
        if writing:
            # Only a registered session may write (fail closed), and a wake has no user in it:
            # its one write is the setup wake's uncited choose of the first name.
            session_id = ctx.hermes_session_id()
            session = sessions.read(ctx.wstore, session_id) if session_id else None
            if session is None:
                raise StoreError(["this session is not registered; identity writes are refused"])
            if not sessions.opened_by_human_chat(session) and session["kind"] != "wake":
                raise StoreError(
                    [
                        "identity writes are allowed only in a chat the user opened from the IDE; "
                        "this session was opened by another client"
                    ]
                )
            if session["kind"] == "wake" and (cites or confirm or met or offered or name is None):
                raise StoreError(["no user is present in a wake; do this in a chat with them"])
        if cites and (confirm or met or name is not None):
            cites = identity.verify_cite(
                ctx.wstore,
                cites,
                state_db,
                "you are acting on",
                calling_session=ctx.hermes_session_id() or "",
            )
        if name is not None and not cites and not confirm:
            wrote = True
            identity.choose(ctx.wstore, ctx.actor, name)
        elif name is not None or confirm:
            wrote = True
            identity.confirm(ctx.wstore, ctx.actor, cites=cites, name=name)
        if met:
            wrote = True
            identity.mark_met(ctx.wstore, ctx.actor, cites=cites)
        if offered:
            wrote = True
            identity.mark_offered(ctx.wstore, ctx.actor)
    except StoreError as error:
        raise ToolError(str(error)) from None
    return {"identity": identity.read(ctx.wstore), "wrote": wrote}


# --- change_complete --------------------------------------------------------------------------


def complete_change(ctx: ToolContext, worktree: Any, commit: Any, summary: str) -> dict[str, Any]:
    """Send an Orchestrator worktree change through review and merge (design B.12, A3.6).

    The change becomes a goal-less ``maintenance`` Task held by this Session, completed at the
    given commit with a merge request, so the review wake and the mechanical merge reuse the
    Task path unchanged.
    """
    from research_core import gitops
    from research_core.store import sessions, tasks

    coworld = ctx.coworld()
    if not ctx.hermes_session_id():
        raise ToolError("change_complete needs a Session (IDE_SESSION_ID is not set)")
    if not summary.strip():
        raise ToolError("summary is required: what the change does and why")
    path = Path(str(worktree or "")).expanduser()
    if not path.is_absolute():
        path = coworld.worktrees / path
    if coworld.worktrees not in path.resolve().parents or not (path / ".git").exists():
        raise ToolError(
            f"worktree must be one of this Coworld's worktrees (under {coworld.worktrees}); "
            "open one with workspace.worktree_open"
        )
    branch = gitops.current_branch(path)
    sha = str(commit or "").strip() or gitops.rev_parse(path, "HEAD")
    if not branch or not gitops.commit_on_branch(coworld.code, sha, branch):
        raise ToolError(f"commit {sha[:12]} is not on the worktree's branch {branch!r}")
    if gitops.dirty_files(path):
        raise ToolError(
            "the worktree has uncommitted changes; commit them first",
            dirty=gitops.dirty_files(path)[:20],
        )
    actor = ctx.actor
    session = sessions.read(ctx.wstore, ctx.hermes_session_id())
    if session is None:
        sessions.register(
            ctx.wstore,
            actor,
            session_id=ctx.hermes_session_id(),
            profile=str(ctx.env.get("HERMES_PROFILE_NAME") or ctx.role),
            kind="chat",
            coworld_id=coworld.coworld_id,
            client=sessions.UNKNOWN_CLIENT,
        )
    task = tasks.create(
        ctx.store,
        actor,
        type="maintenance",
        title=f"Orchestrator change: {summary.strip().splitlines()[0][:80]}",
        description=summary,
        goal_id=None,
        for_human=summary.strip().splitlines()[0][:200],
    )
    task_id = int(str(task["id"]).split("-")[-1])
    tasks.claim(
        ctx.store,
        actor,
        task_id,
        run_id=(session or {}).get("run_id"),
        worktree=str(path),
        branch=branch,
        kind="change",
    )
    record = tasks.complete(
        ctx.store,
        actor,
        task_id,
        checklist={"commit": sha, "summary": summary},
        commit=sha,
        merge_request="yes",
        result=summary,
    )
    return {
        "record": record,
        "next": "the review wake judges the change and the watcher merges it into main",
    }


# --- worktree_open ----------------------------------------------------------------------------


def open_worktree(ctx: ToolContext, name: str | None) -> dict[str, Any]:
    """``worktrees/orch-<n>`` on branch ``orch/<n>`` from ``main``; ``name`` reopens one."""
    coworld = ctx.coworld()
    if not (coworld.code / ".git").exists():
        raise ToolError(
            "the Coworld's code repository does not exist yet; workspace.coworld_switch creates it"
        )
    if name:
        match = _CHANGE_WORKTREE.match(name)
        if match is None:
            raise ToolError(f"name must be a change worktree such as orch-2, not {name!r}")
        number = int(match.group(1))
    else:
        number = _next_change_number(coworld.code, coworld.worktrees)
    branch = f"orch/{number}"
    path = gitops.worktree_add(
        coworld.code, coworld.worktree_for_change(number), branch=branch, base="main"
    )
    return {
        "name": f"orch-{number}",
        "worktree": str(path),
        "branch": branch,
        "base": "main",
        "head": gitops.rev_parse(path, "HEAD"),
        "next": "Work and commit there; then workspace.change_complete {worktree, commit, summary}",
    }


def _next_change_number(code: Path, worktrees: Path) -> int:
    numbers = [0]
    if worktrees.is_dir():
        for entry in worktrees.iterdir():
            match = _CHANGE_WORKTREE.match(entry.name)
            if match:
                numbers.append(int(match.group(1)))
    refs = gitops.git(code, "for-each-ref", "--format=%(refname:short)", "refs/heads/orch/")
    for ref in refs.stdout.split():
        suffix = ref.removeprefix("orch/")
        if suffix.isdigit():
            numbers.append(int(suffix))
    return max(numbers) + 1


# --- doctor -----------------------------------------------------------------------------------


def doctor(ctx: ToolContext) -> dict[str, Any]:
    """Health facts with a flat ``problems`` list: commits, the watcher heartbeat, both stores'
    schema status, capabilities, CLI versions against the lock, stale skill overrides, whether
    the core path is writable (A7.7) and whether profiles' API keys agree (A5.8)."""
    env, ws = ctx.env, ctx.workspace
    checkout = environment_guide.core_checkout(env)
    coworld = ctx.coworld_or_none()
    report: dict[str, Any] = {
        "commits": {
            "image": environment_guide.image_info(env),
            "source": environment_guide.source_commit(checkout),
            "checkout": None if checkout is None else str(checkout),
        },
        "watcher": _heartbeat(ctx.wstore),
        "stores": {
            "workspace": _schema(ctx.wstore),
            "coworld": None if coworld is None else _coworld_schema(ctx),
        },
        "capabilities": [] if coworld is None else _capability_rows(ctx.store),
        "clis": _clis(ws.venv, checkout),
        "skill_overrides": [] if coworld is None else coworld_setup.skill_overrides(coworld.code),
        "core_path": None
        if checkout is None
        else {"path": str(checkout), "writable": os.access(checkout, os.W_OK)},
        "api_keys": _api_keys(env),
    }
    report["problems"] = _problems(report)
    return report


def _schema(store: Store) -> dict[str, Any]:
    status = store.schema_status()
    return {
        "ok": status.ok,
        "diff": status.diff,
        "user_version": status.user_version,
        "expected_version": status.expected_version,
    }


def _coworld_schema(ctx: ToolContext) -> dict[str, Any]:
    try:
        return _schema(ctx.store)
    except SchemaMismatch as error:
        return {"ok": False, "diff": error.problems}


def _heartbeat(wstore: Store) -> dict[str, Any]:
    value = wstore.get_meta(HEARTBEAT_KEY)
    if value is None:
        return {"heartbeat": None, "age_s": None}
    try:
        age = (parse_iso(now_iso()) - parse_iso(value)).total_seconds()
    except ValueError:
        return {"heartbeat": value, "age_s": None}
    return {"heartbeat": value, "age_s": round(age)}


def _clis(venv: Path, checkout: Path | None) -> dict[str, Any]:
    lock = environment_guide.lock_versions(checkout)
    entries = []
    for entry in environment_guide.cli_versions(venv):
        locked = lock.get(entry["distribution"])
        stale = bool(entry["version"] and locked and entry["version"] != locked)
        entries.append({**entry, "lock": locked, "stale": stale})
    return {"lock_refreshed": environment_guide.lock_refreshed(checkout), "clis": entries}


def _api_keys(env) -> dict[str, Any]:
    """Which profiles' ``.env`` carry the same ``API_SERVER_KEY`` as this one (names only)."""
    if not env.get("HERMES_HOME"):
        return {"checked": [], "differing": [], "missing": [], "note": "HERMES_HOME is not set"}
    home = Path(env["HERMES_HOME"])
    names = [home.name]
    names += [n.strip() for n in (env.get("IDE_RESEARCHER_PROFILES") or "researcher").split(",")]
    keys = {n: _env_value(home.parent / n / ".env", "API_SERVER_KEY") for n in names if n}
    reference = keys[home.name]
    return {
        "checked": list(keys),
        "differing": [
            n for n, k in keys.items() if k is not None and reference is not None and k != reference
        ],
        "missing": [n for n, k in keys.items() if k is None],
    }


def _env_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        text = line.strip().removeprefix("export ").strip()
        if text.startswith(f"{key}="):
            return text.partition("=")[2].strip().strip("'\"") or None
    return None


def _problems(report: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, status in report["stores"].items():
        if status is not None and not status["ok"]:
            problems.append(
                f"{name} store schema differs from the release: {'; '.join(status['diff'])}"
            )
    watcher = report["watcher"]
    if watcher["heartbeat"] is None:
        problems.append("the watcher has not recorded a heartbeat yet")
    elif watcher["age_s"] is not None and watcher["age_s"] > HEARTBEAT_STALE_S:
        problems.append(f"the watcher's last heartbeat is {watcher['age_s']} s old")
    broken = [c["name"] for c in report["capabilities"] if c["status"] == "broken"]
    if broken:
        problems.append(f"broken capabilities: {', '.join(broken)}")
    refreshed = report["clis"]["lock_refreshed"] or "unknown date"
    for cli in report["clis"]["clis"]:
        if cli["stale"]:
            problems.append(
                f"{cli['name']}: installed {cli['version']}, lock {cli['lock']}, "
                f"lock refreshed {refreshed}"
            )
    stale = [o["name"] for o in report["skill_overrides"] if o["stale"]]
    if stale:
        problems.append(f"skill overrides whose core skill changed: {', '.join(stale)}")
    if report["core_path"] and report["core_path"]["writable"]:
        problems.append("core path writable: yes (hosting should mount the checkout read-only)")
    differing = report["api_keys"]["differing"]
    if differing:
        problems.append(f"API_SERVER_KEY differs from this profile's in: {', '.join(differing)}")
    return problems


# --- capabilities -----------------------------------------------------------------------------


def _capability_rows(store: Store) -> list[dict[str, Any]]:
    """Every capability, with the end of its last check's output only when it is not ok.

    A passing check's output (a CLI's whole ``--help``) was 92-95% of these results, 17k-37k
    characters each. The row keeps all of it.
    """
    rows = []
    for row in capabilities.status(store):
        output = row.pop("output")
        if row["status"] != "ok":
            row["output_tail"] = output[-capabilities.OUTPUT_TAIL_CHARS :]
        rows.append(row)
    return rows


def list_capabilities(ctx: ToolContext, *, check: bool) -> dict[str, Any]:
    store = ctx.store
    result: dict[str, Any] = {
        "capabilities": _capability_rows(store),
        "broken": capabilities.broken(store),
    }
    if check:
        due = store.now()
        store.set_meta(CHECKS_DUE_KEY, due)
        result["checks_due"] = due
        result["note"] = (
            "checks marked due; the watcher runs them on its next tick (within a minute), and "
            "this action or doctor shows the result"
        )
    return result


SPEC = durable_tool(
    ToolSpec(
        name="workspace",
        description=(
            "The workspace itself: set up the session Coworld, open a change worktree, run the "
            "health check (doctor), list capabilities or ask for a new check, and read the "
            "environment guide (the first thing to consult when unsure about the environment)."
        ),
        handler=handle,
        roles=BOTH,
        executor="workspace",
        actions=(
            ActionSpec(
                "coworld_switch",
                "Set up this session's Coworld; another Coworld requires a new session.",
                {
                    "coworld": a.string("The platform Coworld id (cow_...)."),
                    "cites": a.string("The user's message reference when the user asked."),
                    "pending_request": a.obj(
                        "The user's request that setup carries out when done.",
                        {
                            "text": a.string("The request, in the user's words."),
                            "cites": a.string("The user's message reference."),
                        },
                    ),
                    "source": a.string(
                        "A git URL, when the platform lists no source or the user names a fork."
                    ),
                    "commit": a.string("The commit of source to set up."),
                },
                ("coworld",),
                ORCHESTRATOR,
            ),
            ActionSpec(
                "worktree_open",
                "Open worktrees/orch-<n> on branch orch/<n> from main for a change of your own.",
                {"name": a.string("An existing change worktree to reopen, such as orch-2.")},
                (),
                ORCHESTRATOR,
            ),
            ActionSpec(
                "change_complete",
                "Send a change made in your own worktree through review and merge.",
                {
                    "worktree": a.string("The worktree path or name, such as orch-1."),
                    "commit": a.string("The commit to merge; defaults to the worktree's HEAD."),
                    "summary": a.string("What the change does and why."),
                },
                ("worktree", "summary"),
                ORCHESTRATOR,
            ),
            ActionSpec("doctor", "Health facts and problems: commits, watcher, stores, CLIs."),
            ActionSpec(
                "capabilities",
                "List capabilities; check: true asks the watcher to run the checks next tick.",
                {"check": a.boolean("Mark the checks due (they run within a minute).")},
            ),
            ActionSpec(
                "identity",
                "Read the cogent's own name and whether it has met the user. name alone "
                "(setup only) is refused once a name exists; name or confirm with cites (the "
                "user's own chat message as session:<id>/<message id>#<sha256 of its content>, "
                "checked against the stored transcript; never a record id or a wake) is the "
                "user's one rewrite, "
                "then locked, and also names a cogent that has none; met_user with cites records "
                "the first get-to-know-you chat; offer_made records the one rename offer.",
                {
                    "name": a.string("Your own short name, one line."),
                    "confirm": a.boolean("The user kept the name you have (needs cites)."),
                    "met_user": a.boolean("Record that you have met the user (needs cites)."),
                    "offer_made": a.boolean("Record that you made the one rename offer."),
                    "cites": a.string(
                        "The user's message: session:<id>/<message id>#<sha256 of its content>."
                    ),
                },
                (),
                ORCHESTRATOR,
            ),
            ActionSpec("environment_guide", "Where you are, what is writable, credentials, CLIs."),
        ),
    ),
    mutations={"change_complete"},
)
