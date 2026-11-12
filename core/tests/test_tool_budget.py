"""The tool surface (design C.2, amendment A3.1): counts, roles and executors."""

from pathlib import Path

import pytest

from research_core.tools import catalog

ORCHESTRATOR = {
    "goal",
    "task",
    "claim",
    "experiment",
    "version",
    "directive",
    "settings",
    "chat_update",
    "workspace",
    "upload",
    "submit",
    "xp",
    "platform",
    "forum_post",
    "wiki_edit",
    "softmax_api",
    "github",
}
# Actions with a tool of their own (ActionSpec.own_tool). The two roles have different ones.
OWN_TOOLS_BOTH = {"claim_write", "claim_add_evidence", "experiment_create"}
OWN_TOOLS_ORCHESTRATOR = {
    "goal_set_status",
    "settings_update",
    "task_abandon",
}
OWN_TOOLS_RESEARCHER = {
    "task_ask",
    "task_complete",
    "task_propose",
    "task_propose_submission",
    "task_release",
    "task_wait",
}
RESEARCHER = (
    (ORCHESTRATOR - {"chat_update", "submit", "github"}) | OWN_TOOLS_BOTH | OWN_TOOLS_RESEARCHER
)
ORCHESTRATOR = ORCHESTRATOR | OWN_TOOLS_BOTH | OWN_TOOLS_ORCHESTRATOR
INPROC = {
    "goal",
    "goal_set_status",
    "task",
    "task_abandon",
    "task_complete",
    "directive",
    "claim",
    "claim_write",
    "claim_add_evidence",
    "settings",
    "settings_update",
    "chat_update",
}


def names(role):
    return {spec.name for spec in catalog.tools_for(role)}


def test_a_broken_tool_module_fails_loudly(tmp_path, monkeypatch):
    (tmp_path / "broken_tool_module.py").write_text("def f():\n        x = 1\n    y = 2\n")
    monkeypatch.syspath_prepend(tmp_path)
    monkeypatch.setattr(catalog, "TOOL_MODULES", ("broken_tool_module",))
    monkeypatch.setattr(catalog, "_loaded", False)
    with pytest.raises(IndentationError):
        catalog.tools_for("orchestrator")
    assert catalog._loaded is False


def test_tool_counts_match_the_design():
    assert names("orchestrator") == ORCHESTRATOR and len(ORCHESTRATOR) == 23
    assert names("researcher") == RESEARCHER and len(RESEARCHER) == 23
    assert len(ORCHESTRATOR) <= catalog.TOOL_BUDGET


def test_readmes_state_the_tool_counts():
    """Hermes registers each tool of an action of its own, so the counts are of the whole list."""
    repo = Path(__file__).resolve().parents[2]
    for role, tools in (("orchestrator", ORCHESTRATOR), ("researcher", RESEARCHER)):
        assert f"{len(tools)} tools, listed in" in (repo / role / "README.md").read_text()
    core = (repo / "core" / "README.md").read_text()
    assert f"{len(ORCHESTRATOR)} Orchestrator, {len(RESEARCHER)} Researcher" in core


def test_executor_table():
    for spec in catalog.tools_for("orchestrator"):
        expected = "inproc" if spec.name in INPROC else "workspace"
        assert spec.executor == expected, (spec.name, spec.executor)


def test_schemas_render_for_both_roles():
    for role in catalog.ROLES:
        for spec in catalog.tools_for(role):
            schema = spec.schema(role)
            assert set(schema) == {"name", "description", "parameters"}
            if spec.actions is not None:
                assert schema["parameters"]["properties"]["action"]["enum"]
