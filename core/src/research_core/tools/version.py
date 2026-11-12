"""The ``version`` tool (design B.6, C.2, Q13): build, adopt, read and list Versions.

``create`` runs the mixin ``build`` command in the caller's working tree (a Researcher: the
worktree of the Task it holds; the Orchestrator: ``worktrees/orch-<n>`` when ``worktree`` is
given, else ``code/``), hashes the artifact the build produced and records the Version at the
tree's exact commit, tagged ``v/<n>`` in the code repository. The tree must be clean apart from
the artifact itself, and it must hold no build input git does not track (a git-ignored file,
a nested repository that is not a declared submodule), caches aside: a Version is a commit,
not a working copy, and its commit must be enough to rebuild it. The build runs detached with a
budget (A3.2): past it the tool answers ``background`` and the agent either waits for the
process or calls ``create`` again with ``resume``. A failed build reports the ``build``
capability (A4.1) and the refusal carries the log path and tail (B.15).

The exact artifact bytes are also kept under ``<coworld>/documents/artifacts/<digest>`` so the
upload tool sends what the build produced even after the tree moved on.

``adopt`` makes an adopted Version from a policy version uploaded outside the workspace.
``read`` and ``list`` carry the computed submission status (platform/submission.py).

Workspace executor: it needs PyYAML (the mixin front matter) and the platform.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from research_core import gitops, mixin
from research_core.platform import submission
from research_core.platform.errors import PlatformError
from research_core.platform.protocol import PolicyVersion, with_retries
from research_core.platform.wait import tool_error
from research_core.store import sessions, tasks, versions
from research_core.store._common import parse_display_id
from research_core.tools import _args as a
from research_core.tools.base import (
    ActionSpec,
    ToolCall,
    ToolContext,
    ToolError,
    ToolSpec,
    player_policy_name,
    tool,
)
from research_core.transitions import report_capability

ORCHESTRATOR = frozenset({"orchestrator"})
BOTH = frozenset({"orchestrator", "researcher"})

BUILD_BUDGET_S = 240
RUNTIME_MARKER = ".softmax-runtime.json"
# A build's resume token is the commit it started at, this, then the mixin command's token.
RESUME_SEPARATOR = ":"

# Git-ignored paths that are caches or OS droppings, never build inputs: a path with one of
# these as a component does not stop a build, nor is it searched for nested repositories.
CACHE_NAMES = frozenset(
    {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".DS_Store",
    }
)

VERSION = a.string("The Version, such as V-7.")

# Tests replace this to skip the retry backoff; platform_call reads it on every call.
sleep: Callable[[float], None] = time.sleep


# --- shared by the upload and submit tools ------------------------------------------------


def platform_call[T](ctx: ToolContext, route: str, fn: Callable[[], T]) -> T:
    """Run a platform call with retries; an error that escapes is a refusal with its context."""
    try:
        return with_retries(fn, sleep=sleep)
    except PlatformError as error:
        raise platform_refusal(ctx, route, error) from error


def platform_refusal(ctx: ToolContext, route: str, error: PlatformError) -> ToolError:
    """The refusal every platform tool gives: route, status and request id (B.15), the login report
    and the Task's wait on the platform."""
    return tool_error(ctx, route, error)


def intent_number(intent: dict[str, Any]) -> int:
    """The integer id behind an intent record's ``I-<n>``."""
    return parse_display_id(intent["id"])[1]


def with_status(ctx: ToolContext, record: dict[str, Any]) -> dict[str, Any]:
    """The Version record with the full submission-status ladder filled in."""
    record["submission_status"] = submission.status(ctx.store, ctx.platform(), record)
    return record


def version_number(value: Any) -> int:
    return a.record_number(value, "version", "version")


def policy_version_dict(found: PolicyVersion) -> dict[str, Any]:
    return {**asdict(found), "label": found.label}


def artifact_copy_path(ctx: ToolContext, digest: str) -> Path:
    """Where the exact built bytes of a Version live, by their sha256."""
    return ctx.coworld().documents / "artifacts" / digest


# --- create --------------------------------------------------------------------------------


def handle_create(call: ToolCall) -> dict:
    ctx = call.ctx
    coworld = ctx.coworld()
    tree, task_id = working_tree(ctx, call)
    loaded = load_mixin(tree, coworld.mixin)
    artifact_path = str(loaded.artifact["path"])
    untracked = untracked_inputs(tree, artifact_path)
    if any(untracked.values()):
        raise untracked_inputs_refusal(coworld, tree, artifact_path, untracked)
    dirty = dirty_files_excluding(tree, artifact_path)
    if dirty:
        raise ToolError(
            f"{tree_name(coworld, tree)} has uncommitted changes; commit them first"
            " (a Version is an exact commit)",
            dirty_files=dirty,
        )
    branch = gitops.current_branch(tree)
    if branch is None:
        raise ToolError(f"{tree_name(coworld, tree)} is on a detached HEAD; check out a branch")
    commit = gitops.rev_parse(tree, "HEAD")
    result = run_build(call, loaded, tree, coworld.documents / "logs", commit)
    if not result.finished:
        return background_result(ctx.role, result, commit)
    if result.exit_code != 0:
        raise build_failure(ctx, result, f"exit code {result.exit_code}")
    artifact_file = tree / artifact_path
    if not artifact_file.is_file():
        raise build_failure(ctx, result, f"no artifact at {artifact_path} after it")
    with artifact_file.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    keep_artifact_copy(ctx, digest, artifact_file)
    record = versions.create(
        ctx.store,
        ctx.actor,
        summary=call.get("summary"),
        commit_sha=commit,
        branch=branch,
        artifact={
            "kind": loaded.artifact["kind"],
            "path": artifact_path,
            "digest": digest,
            "size": artifact_file.stat().st_size,
        },
        based_on=[f"V-{n}" for n in a.record_numbers(call.get("based_on"), "version", "based_on")],
        task_id=task_id,
        experiment_id=(
            a.record_number(call.get("experiment"), "experiment", "experiment")
            if call.get("experiment")
            else None
        ),
        image=runtime_image(ctx),
        policy_name=player_policy_name(
            loaded.artifact.get("policy_name") or default_policy_name(coworld, branch),
            ctx.player_id(),
        ),
    )
    gitops.tag(coworld.code, f"v/{parse_display_id(record['id'])[1]}", commit)
    return {
        "record": with_status(ctx, record),
        "build": {"log": str(result.log_path), "duration_s": round(result.duration_s, 3)},
    }


def working_tree(ctx: ToolContext, call: ToolCall) -> tuple[Path, int | None]:
    """The tree to build in and the Task it belongs to (A3.1, design B.4).

    A Researcher builds in the worktree of the Task it holds (``task``, else the Session's
    Task) and must be its lease holder. The Orchestrator builds in ``worktrees/orch-<n>`` when
    ``worktree`` names one, else in ``code/``.
    """
    coworld = ctx.coworld()
    task_arg = call.get("task")
    task_id = a.record_number(task_arg, "task", "task") if task_arg else None
    if ctx.role == "researcher":
        if task_id is None:
            task_id = session_task_id(ctx)
        if task_id is None:
            raise ToolError(
                "a Researcher builds in its Task's worktree; this Session holds no Task"
                " (give task, or claim one first)"
            )
        tasks.require_holder(ctx.store, task_id, ctx.actor)
        worktree = tasks.read(ctx.store, task_id)["worktree"]
        if not worktree:
            raise ToolError(f"T-{task_id} records no worktree; claim it to get one")
        path = Path(worktree)
        return (path if path.is_absolute() else coworld.root / path), task_id
    name = call.get("worktree")
    if not name:
        return coworld.code, task_id
    path = coworld.worktrees / orch_worktree_name(str(name))
    if not path.is_dir():
        existing = sorted(p.name for p in coworld.worktrees.glob("orch-*") if p.is_dir())
        raise ToolError(
            f"no worktree {path.name}; open one with workspace.worktree_open"
            + (f" (existing: {', '.join(existing)})" if existing else ""),
        )
    return path, task_id


def session_task_id(ctx: ToolContext) -> int | None:
    session = sessions.read(ctx.wstore, ctx.hermes_session_id())
    if session is None or not session.get("task_id"):
        return None
    return int(session["task_id"])


def orch_worktree_name(name: str) -> str:
    """``orch-3``, ``3`` or a path ending in ``orch-3`` all name worktree ``orch-3``."""
    base = Path(name).name
    return base if base.startswith("orch-") else f"orch-{base}"


def tree_name(coworld, tree: Path) -> str:
    try:
        return str(tree.relative_to(coworld.root))
    except ValueError:
        return tree.name


def load_mixin(tree: Path, fallback: Path) -> mixin.Mixin:
    try:
        return submission.load_mixin_for(tree, fallback)
    except mixin.MixinError as error:
        problems = list(error.problems)
        if not (fallback / mixin.FRONT_MATTER_FILE).is_file():
            problems.append(
                "setup has not run: the Orchestrator's workspace.coworld_switch does it"
            )
        raise ToolError(problems, mixin=str(tree / "mixin")) from error


def dirty_files_excluding(tree: Path, artifact_path: str) -> list[str]:
    """Uncommitted paths, ignoring what the build writes: the artifact and untracked files in its
    folder. A tracked source file beside the artifact still counts: the build may have used it,
    and the Version's commit would not hold the change.
    """
    tracked = gitops.tracked_files(tree)
    artifact = artifact_path.strip("/")
    return [
        entry
        for entry in gitops.dirty_files(tree)
        if not is_build_output(entry, artifact_path) or (entry in tracked and entry != artifact)
    ]


def untracked_inputs(tree: Path, artifact_path: str) -> dict[str, list[str]]:
    """Inputs a commit cannot rebuild: git-ignored paths and nested repositories.

    Neither shows in ``dirty_files``. Caches (``CACHE_NAMES``), the artifact's folder and
    submodules declared in ``.gitmodules`` are not inputs. The keys are the refusal's context.
    """

    def is_input(entry: str) -> bool:
        return not (is_cache(entry) or is_build_output(entry, artifact_path))

    nested = gitops.nested_repositories(tree, skip_names=CACHE_NAMES)
    return {
        "ignored_files": [e for e in gitops.ignored_files(tree) if is_input(e)],
        "nested_repositories": [e for e in nested if is_input(e)],
    }


def untracked_inputs_refusal(
    coworld, tree: Path, artifact_path: str, found: dict[str, list[str]]
) -> ToolError:
    """The refusal names each input and says what to do about its kind."""
    listed, how = [], []
    if found["ignored_files"]:
        listed.append("git-ignored: " + ", ".join(found["ignored_files"]))
        how.append("un-ignore them")
    if found["nested_repositories"]:
        listed.append("nested repository: " + ", ".join(found["nested_repositories"]))
        how.append(
            "for a nested repository, remove its .git and commit its files, or declare it as a"
            " submodule in .gitmodules"
        )
    folder = artifact_path.strip("/").rpartition("/")[0]
    outputs = f", or write them under {folder}/ if they are build outputs" if folder else ""
    return ToolError(
        f"{tree_name(coworld, tree)} has inputs git does not track ({'; '.join(listed)}); a"
        f" Version is an exact commit, so commit them ({'; '.join(how)}){outputs}",
        **found,
    )


def is_cache(entry: str) -> bool:
    """Whether any component of the path is a cache name (``CACHE_NAMES``)."""
    return any(part in CACHE_NAMES for part in entry.strip("/").split("/"))


def is_build_output(entry: str, artifact_path: str) -> bool:
    """Whether ``entry`` is the artifact, a folder on the way to it, or anything in its folder.

    ``git status`` names a whole untracked or ignored folder as one entry (``build/``), so a
    folder holding the artifact counts; so does a file beside the artifact, which the build
    wrote too. An artifact at the repository root has no folder: only the artifact counts.
    """
    artifact = artifact_path.strip("/")
    path = entry.rstrip("/")
    if path == artifact or artifact.startswith(path + "/"):
        return True
    folder = artifact.rpartition("/")[0]
    return bool(folder) and path.startswith(folder + "/")


def run_build(
    call: ToolCall, loaded: mixin.Mixin, tree: Path, log_dir: Path, commit: str
) -> mixin.CommandResult:
    resume = call.get("resume")
    if resume:
        built_commit, _, token = str(resume).partition(RESUME_SEPARATOR)
        if built_commit != commit:
            # The artifact on disk is the started build's: it must not be recorded against a
            # commit made since. Build again.
            raise ToolError(
                f"the resumed build started at commit {built_commit[:12]}, but the tree is now at"
                f" {commit[:12]}; call version.create again without resume"
            )
        try:
            return mixin.resume(token)
        except mixin.MixinError as error:
            raise ToolError(error.problems) from error
    return mixin.run_command(loaded, "build", cwd=tree, log_dir=log_dir, budget_s=BUILD_BUDGET_S)


def background_result(role: str, result: mixin.CommandResult, commit: str) -> dict:
    if role == "researcher":
        next_step = (
            f"the build is still running: task_wait {{task, kind: process, ref: {result.pid},"
            " detail: 'the build'} and end this run. Only after the watcher resumes you, call"
            " version.create with the same arguments plus resume: <background.resume>"
        )
    else:
        next_step = (
            f"the build is still running as pid {result.pid}: when it has ended, call"
            " version.create again with the same arguments plus resume: <background.resume>"
        )
    return {
        "ok": True,
        "background": {
            "pid": result.pid,
            "log": str(result.log_path),
            "resume": f"{commit}{RESUME_SEPARATOR}{result.resume_token}",
        },
        "output_tail": result.output_tail,
        "next": next_step,
    }


def build_failure(ctx: ToolContext, result: mixin.CommandResult, what: str) -> ToolError:
    """Mark ``build`` broken with its repair Task (A4.1) and explain with the log (B.15)."""
    report = report_capability(
        ctx,
        "build",
        output=log_text(result.log_path),
        command=result.command,
    )
    task = report.get("maintenance_task")
    return ToolError(
        [f"the build ended with {what}; full output in {result.log_path}"],
        command=result.command,
        exit_code=result.exit_code,
        log=str(result.log_path),
        output_tail=result.output_tail,
        capability="build",
        maintenance_task=None if task is None else task["id"],
    )


def log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def keep_artifact_copy(ctx: ToolContext, digest: str, source: Path) -> Path:
    path = artifact_copy_path(ctx, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        shutil.copyfile(source, path)
    return path


def runtime_image(ctx: ToolContext) -> str | None:
    """The hosted runtime image recorded by metta in ``<HERMES_HOME>/.softmax-runtime.json``."""
    home = ctx.env.get("HERMES_HOME")
    if not home:
        return None
    marker = Path(home).expanduser() / RUNTIME_MARKER
    try:
        image = json.loads(marker.read_text(encoding="utf-8")).get("image")
    except (OSError, ValueError, AttributeError):
        return None
    return image if isinstance(image, str) and image else None


def default_policy_name(coworld, branch: str) -> str:
    return f"{coworld.coworld_id}-{branch.replace('/', '-')}"


# --- adopt, read, list ---------------------------------------------------------------------


def handle_adopt(call: ToolCall) -> dict:
    ctx = call.ctx
    policy_version_id, label = call.get("policy_version"), call.get("label")
    if not policy_version_id and not label:
        raise ToolError("give policy_version (the platform's id) or label (name:vN)")
    platform = ctx.platform()
    if label:
        name, _, number = str(label).rpartition(":v")
        if not name or not number.isdigit():
            raise ToolError(f"label must look like name:v3, not {label!r}")
        candidates = platform_call(
            ctx, "GET /stats/policy-versions", lambda: platform.policy_versions(name=name)
        )
        found = [p for p in candidates if p.version == int(number)]
    else:
        candidates = platform_call(
            ctx, "GET /stats/policy-versions", lambda: platform.policy_versions()
        )
        found = [p for p in candidates if p.id == policy_version_id]
    if not found:
        known = ", ".join(f"{p.label} ({p.id})" for p in candidates) or "none"
        raise ToolError(
            f"no policy version {label or policy_version_id} among this Player's uploads",
            known=known,
        )
    chosen = found[0]
    record = versions.adopt(
        ctx.store,
        ctx.actor,
        summary=call.get("summary"),
        policy_version_id=chosen.id,
        policy_name=chosen.name,
        uploaded_at=chosen.created_at or ctx.store.now(),
        digest=chosen.content_hash,
    )
    return {"record": with_status(ctx, record), "policy_version": policy_version_dict(chosen)}


def handle_read(call: ToolCall) -> dict:
    ctx = call.ctx
    record = with_status(ctx, versions.read(ctx.store, version_number(call.get("version"))))
    result: dict[str, Any] = {"record": record}
    policy_version_id = (record.get("uploaded") or {}).get("policy_version_id")
    if policy_version_id:
        try:
            states = submission.platform_states(ctx.platform(), policy_version_id)
        except PlatformError as error:
            result["platform"] = {"unavailable": error.detail}
        else:
            result["platform"] = submission.states_as_dicts(states)
    return result


def handle_list(call: ToolCall) -> dict:
    ctx = call.ctx
    limit = call.get("limit") or 20
    records = [with_status(ctx, r) for r in versions.list(ctx.store, limit=limit)]
    return {"records": records}


def dispatch(call: ToolCall) -> dict:
    if call.action == "create":
        return handle_create(call)
    if call.action == "adopt":
        return handle_adopt(call)
    if call.action == "read":
        return handle_read(call)
    if call.action == "list":
        return handle_list(call)
    raise AssertionError(call.action)


SPEC = tool(
    ToolSpec(
        name="version",
        description=(
            "Versions: policy candidates. create builds the policy at the current commit of"
            " your working tree and records the artifact; adopt records a policy version"
            " uploaded outside the workspace. Each Version shows its submission status"
            " (built, uploaded, submitted, qualifying, competing, champion). Returns the record."
        ),
        handler=dispatch,
        roles=BOTH,
        executor="workspace",
        actions=(
            ActionSpec(
                "create",
                "Build a Version from the committed state of your working tree (a Researcher:"
                " its Task's worktree; the Orchestrator: code/, or the worktree it names). The"
                " tree must be clean. A long build returns background with a resume token.",
                {
                    "summary": a.string("What this Version changes, in one sentence."),
                    "based_on": a.strings("Versions it was built from, such as V-3."),
                    "task": a.string(
                        "The Task this Version belongs to (a Researcher's held Task)."
                    ),
                    "experiment": a.string("The Experiment it is a candidate for, such as X-2."),
                    "worktree": a.string("The worktree to build in, such as orch-2; omit: code/."),
                    "resume": a.string(
                        "The resume token from an earlier background answer, once the build ended."
                    ),
                },
                ("summary",),
                orchestrator_fields=("worktree",),
            ),
            ActionSpec(
                "adopt",
                "Record a policy version this Player uploaded elsewhere as an adopted Version"
                " (already uploaded; submit accepts it). Give policy_version or label.",
                {
                    "policy_version": a.string("The platform's policy version id."),
                    "label": a.string("Or its label, name:vN."),
                    "summary": a.string("What this policy is, in one sentence."),
                },
                ("summary",),
                example_fields=("label",),
            ),
            ActionSpec(
                "read",
                "Read a Version with its submission status and the platform's view of it.",
                {"version": VERSION},
                ("version",),
            ),
            ActionSpec(
                "list",
                "List the newest Versions with their submission status.",
                {"limit": a.integer("At most this many (default 20).")},
            ),
        ),
    )
)
