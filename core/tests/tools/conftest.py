"""Fixtures for tool tests: a workspace with one active Coworld and contexts per role."""

import json

import pytest

from research_core.store import coworlds, sessions
from research_core.store.actors import Actor
from research_core.tools.base import ToolContext, run_tool

COWORLD = "cow_fake"


@pytest.fixture
def workspace_env(tmp_path):
    return {
        "IDE_WORKSPACE": str(tmp_path / "ws"),
        "IDE_PLATFORM": "fake",
        "IDE_COWORLD_ID": COWORLD,
    }


@pytest.fixture
def ctx_factory(workspace_env):
    """A ToolContext factory on a workspace where cow_fake is the active Coworld."""
    created = {}

    def make(role="orchestrator", session_id="s1", coworld=COWORLD, *, registered=True):
        env = dict(workspace_env, IDE_SESSION_ID=session_id, IDE_COWORLD_ID=coworld or "")
        ctx = ToolContext.from_env(role, env)
        ctx.coordinator_instance = "coordinator:unit"
        ctx.workspace.ensure_dirs()
        if registered and session_id and sessions.read(ctx.wstore, session_id) is None:
            sessions.register(
                ctx.wstore,
                Actor.harness(),
                session_id=session_id,
                profile=role,
                kind="chat",
                coworld_id=coworld,
            )
        if coworld and coworld not in created:
            harness = Actor.parse("harness")
            coworlds.register(
                ctx.wstore, harness, coworld, folder=ctx.workspace.coworld(coworld).folder_name
            )
            coworlds.activate(ctx.wstore, harness, coworld)
            ctx.workspace.coworld(coworld).ensure_dirs()
            created[coworld] = True
        return ctx

    return make


@pytest.fixture
def call(ctx_factory):
    """``call(tool, args, role="orchestrator")`` → the parsed JSON result of a tool call."""

    def run(tool, args, role="orchestrator", session_id="s1", *, registered=True):
        return json.loads(
            run_tool(tool, args, ctx_factory(role, session_id, registered=registered))
        )

    return run
