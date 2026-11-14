"""Source recovery (issue #106, cases 1 and 2): a published Version's source is found and
edited from the persistent repository alone, resolved from the platform's policy version id,
after the Task's worktree is gone and the whole workspace has moved (a pod replaced onto a
relocated volume).

The scenario runs the real ``version`` and ``upload`` tools on the fake platform with the
fakegame fixture; no model, no seam. One test walks the steps in order with short helpers, so a
failure names the step. The seam route that serves such an edit is a follow-up; here the
information it needs (the tag, the base commit, the retained bytes) is shown to exist.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import research_core.tools.upload  # noqa: F401  (registers the tool)
import research_core.tools.version  # noqa: F401  (registers the tool)
from research_core import gitops
from research_core.platform import submission
from research_core.store import coworlds, sessions, tasks, versions
from research_core.store._common import parse_display_id
from research_core.store.actors import Actor
from research_core.tools.base import ToolContext, run_tool
from tests.fixtures.fakegame_repo import COWORLD_ID
from tests.tools.test_version_tool import HARNESS, commit_policy, policy_bytes, setup_code_repo

PUBLISHED = (3, 7)  # the policy the Task publishes: aggression 3, patience 7
EDITED = (3, 8)  # the edit made after the move
RESEARCHER = {"role": "researcher", "session_id": "s2"}


class Pod:
    """Tool calls against one workspace root. A new Pod on a moved root is a replaced pod."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = {
            "IDE_WORKSPACE": str(root),
            "IDE_PLATFORM": "fake",
            "IDE_COWORLD_ID": COWORLD_ID,
        }
        self.contexts: list[ToolContext] = []

    def ctx(self, role: str = "orchestrator", session_id: str = "s1") -> ToolContext:
        ctx = ToolContext.from_env(role, {**self.env, "IDE_SESSION_ID": session_id})
        self.contexts.append(ctx)
        return ctx

    def run(self, tool: str, args: dict, *, role: str = "orchestrator", session_id: str = "s1"):
        submission.invalidate()  # each real tool call is its own process
        result = json.loads(run_tool(tool, args, self.ctx(role, session_id)))
        assert result["ok"] is True, result
        return result

    def code(self) -> Path:
        return self.ctx().coworld().code

    def retained_artifact(self, record: dict) -> Path:
        return self.ctx().coworld().documents / "artifacts" / record["artifact"]["digest"]

    def close(self) -> None:
        """Close every store this pod opened: a replaced pod's processes are gone."""
        for ctx in self.contexts:
            for name in ("store", "wstore"):
                opened = vars(ctx).get(name)  # a cached_property, set only once used
                if opened is not None:
                    opened.close()
        self.contexts.clear()


def new_pod(root: Path) -> Pod:
    """A workspace with cow_fake active and its code repository as the setup wake leaves it."""
    pod = Pod(root)
    ctx = pod.ctx()
    ctx.workspace.ensure_dirs()
    folder = ctx.workspace.coworld(COWORLD_ID).folder_name
    coworlds.register(ctx.wstore, HARNESS, COWORLD_ID, folder=folder)
    coworlds.activate(ctx.wstore, HARNESS, COWORLD_ID)
    sessions.register(
        ctx.wstore,
        HARNESS,
        session_id="s1",
        profile="orchestrator",
        kind="wake",
        coworld_id=COWORLD_ID,
    )
    ctx.coworld().ensure_dirs()
    setup_code_repo(ctx)
    return pod


def publish_from_task_worktree(pod: Pod) -> tuple[dict, str, Path]:
    """T-1's Researcher commits a policy change in its worktree, builds V-1 and uploads it.

    Returns the uploaded record, the platform's policy version id and the worktree path.
    """
    ctx = pod.ctx()
    coworld = ctx.coworld()
    worktree = gitops.worktree_add(coworld.code, coworld.worktree_for_task(1), branch="task/1")
    commit_policy(worktree, *PUBLISHED, "Raise patience to 7")
    task = tasks.create(
        ctx.store, HARNESS, type="maintenance", title="Tune patience", description="Raise it."
    )
    sessions.register(
        ctx.wstore,
        HARNESS,
        session_id="s2",
        profile="researcher",
        kind="task",
        task_id=1,
        coworld_id=COWORLD_ID,
    )
    tasks.claim(
        ctx.store,
        Actor.session("s2"),
        task["id"],
        run_id=None,
        worktree=str(worktree),
        branch="task/1",
    )
    created = pod.run(
        "version",
        {"action": "create", "summary": "Patience 7", "task": task["id"]},
        **RESEARCHER,
    )
    uploaded = pod.run("upload", {"version": created["record"]["id"]}, **RESEARCHER)
    return uploaded["record"], uploaded["policy_version"]["id"], worktree


def move_workspace(pod: Pod, new_root: Path) -> Pod:
    """Relocate the volume: the old pod is closed, the root moved, a new pod mounts it."""
    pod.close()
    new_root.parent.mkdir(parents=True)
    shutil.move(str(pod.root), str(new_root))
    assert not pod.root.exists()
    return Pod(new_root)


def version_tag(record: dict) -> str:
    return f"v/{parse_display_id(record['id'])[1]}"


def policy_text(parameters: tuple[int, int]) -> str:
    return policy_bytes(*parameters).decode()


def test_published_source_survives_checkout_removal_and_volume_move(tmp_path):
    pod = new_pod(tmp_path / "volume-a" / "workspace")

    # 1. A Task publishes a policy change: build in its worktree, upload the exact bytes.
    original, policy_version_id, worktree = publish_from_task_worktree(pod)
    assert original["uploaded"]["policy_version_id"] == policy_version_id
    retained_bytes = pod.retained_artifact(original).read_bytes()
    assert retained_bytes == policy_bytes(*PUBLISHED)

    # 2. The Task's checkout is removed entirely.
    gitops.worktree_remove(pod.code(), worktree, force=True)
    assert not worktree.exists()

    # 3. The volume is relocated and a new pod mounts it at another path.
    pod = move_workspace(pod, tmp_path / "volume-b" / "workspace")

    # 4. The platform's policy version id alone finds the Version, and the tagged commit in
    #    the persistent repository holds its source.
    found = versions.find_by_policy_version(pod.ctx().store, policy_version_id)
    assert found is not None and found["id"] == original["id"]
    assert found["commit_sha"] == original["commit_sha"]
    code = pod.code()
    tag = version_tag(found)
    assert gitops.rev_parse(code, tag) == found["commit_sha"]
    assert gitops.show_file(code, tag, "policy.json") == policy_text(PUBLISHED)

    # 5. An edit on a branch from the tag, in a fresh worktree, builds a new Version based on
    #    the original; the original's tag, source and retained bytes are unchanged.
    base_commit = gitops.rev_parse(code, tag)
    edit_branch = f"edit/{found['id']}"
    edit_tree = gitops.worktree_add(
        code, pod.ctx().coworld().worktree_for_change(1), branch=edit_branch, base=tag
    )
    assert gitops.rev_parse(edit_tree, "HEAD") == base_commit
    edit_commit = commit_policy(edit_tree, *EDITED, "Raise patience to 8")
    edited = pod.run(
        "version",
        {
            "action": "create",
            "summary": "Patience 8",
            "worktree": "orch-1",
            "based_on": [found["id"]],
        },
    )["record"]
    assert edited["id"] == "V-2" and edited["based_on"] == [found["id"]]
    assert edited["commit_sha"] == edit_commit and edited["branch"] == edit_branch
    assert gitops.show_file(code, version_tag(edited), "policy.json") == policy_text(EDITED)
    assert edited["artifact"]["digest"] != original["artifact"]["digest"]
    assert gitops.rev_parse(code, tag) == original["commit_sha"]
    assert gitops.show_file(code, tag, "policy.json") == policy_text(PUBLISHED)
    assert pod.retained_artifact(original).read_bytes() == retained_bytes

    # 6. A stale edit is detectable: the edit recorded the commit it started from; the tag
    #    still names it (a retry against the tag is exact), while the edit branch has moved
    #    on, so a second edit claiming the same base no longer matches the branch tip.
    assert gitops.rev_parse(code, tag) == base_commit
    assert gitops.rev_parse(code, edit_branch) == edit_commit
    assert edit_commit != base_commit
    assert gitops.is_ancestor(code, base_commit, edit_commit)
