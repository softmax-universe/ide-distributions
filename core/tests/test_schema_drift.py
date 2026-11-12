"""``tools/schema_drift.py``: a frozen tool list against the deployed validator, on a small
``state.db`` shaped like Hermes's (the two tables and columns the tool reads)."""

import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from research_core.tools import catalog

REPO = Path(__file__).resolve().parents[2]
ROLE = "orchestrator"


@pytest.fixture(scope="module")
def drift_tool():
    spec = importlib.util.spec_from_file_location(
        "schema_drift", REPO / "tools" / "schema_drift.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["schema_drift"] = module
    spec.loader.exec_module(module)
    return module


def test_a_frozen_enum_value_the_validator_refuses_is_reported(drift_tool):
    tools = copy.deepcopy(current_tools())
    goal = next(t for t in tools if t["function"]["name"] == "goal")
    goal["function"]["parameters"]["properties"]["status"]["enum"] = ["retired_value"]
    assert any("retired_value" in str(d) for d in drift_tool.compare(ROLE, tools))


def test_a_required_nested_key_missing_from_the_frozen_schema_is_reported(drift_tool):
    tools = copy.deepcopy(current_tools())
    claim = next(t for t in tools if t["function"]["name"] == "claim_write")
    del claim["function"]["parameters"]["properties"]["evidence"]["items"]["properties"]["stance"]
    assert any("evidence.stance" in str(d) for d in drift_tool.compare(ROLE, tools))


@pytest.mark.parametrize(
    "frozen_items", [{"type": "object", "additionalProperties": False}, {"type": "string"}]
)
def test_frozen_items_that_cannot_hold_the_required_keys_are_reported(drift_tool, frozen_items):
    tools = copy.deepcopy(current_tools())
    claim = next(t for t in tools if t["function"]["name"] == "claim_write")
    claim["function"]["parameters"]["properties"]["evidence"]["items"] = frozen_items
    assert any("evidence.stance" in str(d) for d in drift_tool.compare(ROLE, tools))


def current_tools() -> list[dict]:
    return [{"type": "function", "function": s.schema(ROLE)} for s in catalog.tools_for(ROLE)]


def function_named(tools: list[dict], name: str) -> dict:
    return next(t["function"] for t in tools if t["function"]["name"] == name)


def required_field(tool: str = "task") -> tuple[str, str]:
    """An action of ``tool`` and a field only its action requires, so the test names no field."""
    spec = catalog.TOOLS[tool]
    action = next(a for a in spec.actions_for(ROLE) if a.required)
    return action.name, action.required[0]


def write_state_db(path: Path, sessions: dict[str, object]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, prompt TEXT)")
    connection.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, tool_names TEXT, started_at REAL)"
    )
    for index, (session_id, pin) in enumerate(sessions.items()):
        text = json.dumps(pin)
        connection.execute("INSERT OR IGNORE INTO system_prompts VALUES (?, ?)", (text, text))
        connection.execute("INSERT INTO sessions VALUES (?, ?, ?)", (session_id, text, index))
    connection.commit()
    connection.close()
    return path


def test_the_current_tools_show_no_drift(drift_tool):
    assert drift_tool.compare(ROLE, current_tools()) == []


def test_hermes_own_tools_in_the_list_are_not_drift(drift_tool):
    other = {"type": "function", "function": {"name": "todo", "parameters": {}}}
    assert drift_tool.compare(ROLE, [*current_tools(), other]) == []


def test_a_required_field_missing_from_the_frozen_schema_is_named(drift_tool):
    action, field = required_field()
    tools = copy.deepcopy(current_tools())
    del function_named(tools, "task")["parameters"]["properties"][field]
    found = [str(item) for item in drift_tool.compare(ROLE, tools)]
    assert any(f"task.{action}: {field}: the validator requires it" in line for line in found)


def test_a_frozen_field_the_validator_does_not_know_is_named(drift_tool):
    tools = copy.deepcopy(current_tools())
    properties = function_named(tools, "task")["parameters"]["properties"]
    properties["retired_field"] = {"type": "string"}
    found = [str(item) for item in drift_tool.compare(ROLE, tools)]
    assert found == ["task: retired_field: frozen field the validator does not know"]


def test_a_changed_type_is_named(drift_tool):
    action, field = required_field()
    tools = copy.deepcopy(current_tools())
    properties = function_named(tools, "task")["parameters"]["properties"]
    properties[field] = {**properties[field], "type": "boolean"}
    found = [str(item) for item in drift_tool.compare(ROLE, tools)]
    assert len(found) == 1 and found[0].startswith(f"task: {field}: frozen as boolean")


def test_a_missing_or_extra_action_and_a_missing_tool_are_named(drift_tool):
    action = catalog.TOOLS["task"].actions_for(ROLE)[0].name
    tools = copy.deepcopy(current_tools())
    enum = function_named(tools, "task")["parameters"]["properties"]["action"]["enum"]
    enum.remove(action)
    enum.append("retired_action")
    tools = [t for t in tools if t["function"]["name"] != "goal"]
    found = [str(item) for item in drift_tool.compare(ROLE, tools)]
    assert f"task.{action}: deployed action not in the frozen enum" in found
    assert "task.retired_action: frozen action the validator refuses" in found
    assert any(line.startswith("goal: deployed, but not in the frozen list") for line in found)


def test_a_new_optional_field_is_not_drift(drift_tool):
    """Every call still passes without it, so nothing about the frozen list blocks a call."""
    spec = catalog.TOOLS["directive"]
    optional = next(
        name
        for action in spec.actions_for(ROLE)
        for name in action.params
        if not any(name in other.required for other in spec.actions_for(ROLE))
        and name in spec.properties(ROLE)
    )
    tools = copy.deepcopy(current_tools())
    del function_named(tools, "directive")["parameters"]["properties"][optional]
    assert drift_tool.compare(ROLE, tools) == []


def test_the_command_reads_a_copied_state_db(drift_tool, tmp_path, capsys):
    _, field = required_field()
    stale = copy.deepcopy(current_tools())
    del function_named(stale, "task")["parameters"]["properties"][field]
    db = write_state_db(
        tmp_path / "state.db",
        {
            "old-chat": {"version": "sha-a", "tools": stale},
            "new-chat": {"version": "sha-a", "tools": current_tools()},
            "legacy": ["task", "goal"],
        },
    )
    assert drift_tool.main([str(db), "--role", ROLE]) == 1
    output = capsys.readouterr().out
    assert "first old-chat" in output and f"{field}: the validator requires it" in output
    assert "legacy" not in output
    assert drift_tool.main([str(db), "--role", ROLE, "--session", "new-chat"]) == 0
    assert "0 disagreement(s)" in capsys.readouterr().out


def test_a_frozen_enum_still_offering_an_own_tool_action_is_checked_for_its_fields(drift_tool):
    tools = copy.deepcopy(current_tools())
    claim = next(t for t in tools if t["function"]["name"] == "claim")
    claim["function"]["parameters"]["properties"]["action"]["enum"].append("write")
    assert any("summary" in str(d) for d in drift_tool.compare(ROLE, tools))
