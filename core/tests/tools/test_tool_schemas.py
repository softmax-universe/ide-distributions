"""The tool schemas the models see: object fields spell out their keys, every field is
described, and a field several actions share has one shape."""

import json
import re

import pytest

from research_core.tools import catalog
from research_core.tools.base import ActionSpec, ToolSpec, field_description, usage, validate_args

ROLES = catalog.ROLES
# A value of each JSON type, to ask the validator about a field.
VALUES = {
    "string": "x",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "array": ["x"],
    "object": {"x": "x"},
    None: "x",
}


def specs():
    seen = {}
    for role in ROLES:
        for spec in catalog.tools_for(role):
            seen[spec.name] = spec
    return list(seen.values())


def declared_fields(spec: ToolSpec):
    """(where, name, schema) for every field as its action (or the tool) declares it."""
    for name, schema in spec.params.items():
        yield spec.name, name, schema
    for action in spec.actions or ():
        for name, schema in action.params.items():
            yield f"{spec.name}.{action.name}", name, schema


def nested(path: str, schema: dict):
    """Every schema under ``schema`` (itself included), with a dotted path."""
    yield path, schema
    for key, sub in (schema.get("properties") or {}).items():
        yield from nested(f"{path}.{key}", sub)
    if isinstance(schema.get("items"), dict):
        yield from nested(f"{path}[]", schema["items"])
    for index, branch in enumerate(schema.get("anyOf") or ()):
        yield from nested(f"{path}|{index}", branch)


@pytest.mark.parametrize("role", ROLES)
def test_every_object_declares_its_keys_and_every_array_its_items(role):
    """Hermes's sanitizer turns an object without properties into an empty one, and an untyped
    field into a string for Kimi models; neither may reach a model."""
    problems = []
    for spec in catalog.tools_for(role):
        for name, schema in spec.properties(role).items():
            for path, node in nested(f"{spec.name}.{name}", schema):
                if node.get("type") == "object" and "properties" not in node:
                    problems.append(f"{path}: object without properties")
                if node.get("type") == "array" and not isinstance(node.get("items"), dict):
                    problems.append(f"{path}: array without items")
                if "type" not in node and "anyOf" not in node:
                    problems.append(f"{path}: no type")
    assert problems == []


def test_every_field_and_key_is_described():
    problems = []
    for spec in specs():
        for where, name, schema in declared_fields(spec):
            for path, node in nested(f"{where}:{name}", schema):
                if "|" in path:
                    continue  # anyOf branches are types; the field carries the description
                if path.endswith("[]"):
                    continue  # list items are described by the list
                if not str(node.get("description", "")).strip():
                    problems.append(path)
    assert problems == []


def test_a_field_shared_by_actions_has_one_shape():
    """A grouped tool shows a shared field once, shaped as its first action declares it. An action
    with a tool of its own is not in the group's schema, so its shape may differ."""
    problems = []
    for spec in specs():
        shapes: dict[str, tuple[str, dict]] = {}
        for action in spec.actions or ():
            if action.own_tool:
                continue
            for name, schema in action.params.items():
                shape = {k: v for k, v in schema.items() if k != "description"}
                first = shapes.setdefault(name, (action.name, shape))
                if first[1] != shape:
                    problems.append(f"{spec.name}.{name}: {first[0]} and {action.name} differ")
    assert problems == []


def test_a_researcher_is_not_offered_what_only_the_orchestrator_can_use():
    def offered(tool):
        return catalog.TOOLS[tool].schema("researcher")["parameters"]["properties"]

    for tool, field in (
        ("platform", "exclude_user"),
        ("platform", "identity"),
        ("forum_post", "identity"),
        ("forum_post", "cites"),
        ("wiki_edit", "identity"),
        ("softmax_api", "identity"),
        ("version", "worktree"),
    ):
        assert field not in offered(tool), (tool, field)
        schema = catalog.TOOLS[tool].schema("orchestrator")["parameters"]["properties"]
        assert field in schema, (tool, field)


def test_an_update_shaped_action_has_an_example_that_changes_something():
    """Everything an update takes is optional, so an example of the required fields alone would be
    refused as "nothing to update" when a model copies it."""
    for spec in specs():
        for action in spec.actions or ():
            if action.name in ("update", "update_run"):
                assert action.example_fields, (spec.name, action.name)


def test_claim_evidence_items_carry_the_handler_rules():
    schema = catalog.TOOLS["claim_write"].schema("researcher")
    write = schema["parameters"]["properties"]["evidence"]
    assert write["minItems"] == 1
    item = write["items"]
    assert set(item["properties"]) == {"type", "ref", "stance", "note"}
    assert item["required"] == ["type", "ref", "stance"]
    assert "additionalProperties" not in item  # the store ignores extra keys, it does not refuse
    assert "for code, <path>:<a>-<b>@<sha>" in item["properties"]["ref"]["description"]


def test_task_failure_matches_the_store():
    from research_core.store import tasks

    schema = catalog.TOOLS["task_complete"].schema("researcher")
    failure = schema["parameters"]["properties"]["failure"]
    assert failure["required"] == list(tasks.FAILURE_REQUIRED)
    assert set(failure["properties"]) == set(tasks.FAILURE_REQUIRED + tasks.FAILURE_OPTIONAL)
    assert failure["additionalProperties"] is False  # unknown keys are refused


def test_field_description_forms():
    def action(name, description, required=()):
        return ActionSpec(name, "", {"f": {"type": "string", "description": description}}, required)

    one = [action("a", "The thing.", ("f",))]
    assert field_description("f", one, 3) == "The thing. Required for a."
    same = [action("a", "The thing."), action("b", "The thing.", ("f",))]
    assert field_description("f", same, 2) == "The thing. Required for b."
    differ = [action("a", "A filter."), action("b", "The value.", ("f",))]
    assert field_description("f", differ, 3) == (
        "b: The value. a: A filter. Required for b; optional for a."
    )


def test_a_refusal_shows_an_object_fields_keys():
    how_to = usage(catalog.TOOLS["claim"], "researcher", "write")
    assert how_to["required"]["evidence"].endswith("Shape: [{type, ref, stance, note?}]")


GENERATED_FIELDS = ("operation_id", "assignment_id", "task_updated")


@pytest.mark.parametrize("role", ["orchestrator", "researcher"])
def test_no_schema_or_refusal_offers_a_field_code_fills_in(role):
    for spec in catalog.tools_for(role):
        assert not set(GENERATED_FIELDS) & set(spec.schema(role)["parameters"]["properties"])
        for action in spec.actions_for(role) or [None]:
            shown = json.dumps(usage(spec, role, action.name if action else None))
            assert not [name for name in GENERATED_FIELDS if name in shown]


def test_a_frozen_schema_may_still_send_them_on_the_actions_that_took_them():
    task = catalog.TOOLS["task"]
    args = {
        "action": "assign",
        "task": "T-1",
        "profile": "researcher",
        "context": "Repair",
        "rationale": "Fit",
        **dict.fromkeys(GENERATED_FIELDS, "value"),
    }
    assert validate_args(task, "orchestrator", args)[1] == []
    _, problems = validate_args(task, "orchestrator", {"action": "list", "operation_id": "value"})
    assert problems == ["operation_id is not a field of list"]


def role_actions():
    """(role, spec, action) for every action a role's grouped tools offer."""
    for role in ROLES:
        for spec in catalog.tools_for(role):
            for action in spec.grouped_actions(role):
                yield role, spec, action


def test_every_field_a_validator_requires_is_declared():
    """A closed schema that leaves out a required field makes every call to the action refused:
    Kimi models do not send undeclared fields (``core/tests/model/test_undeclared_fields.py``)."""
    problems = []
    for role in ROLES:
        for spec in catalog.tools_for(role):
            properties = spec.schema(role)["parameters"]["properties"]
            problems += [f"{role} {spec.name}: {f}" for f in spec.required if f not in properties]
    for role, spec, action in role_actions():
        accepted = spec.accepted(action)
        properties = spec.schema(role)["parameters"]["properties"]
        for name in (*action.required, *action.example_fields):
            if name not in accepted or name not in properties:
                problems.append(f"{role} {spec.name}.{action.name}: {name}")
    assert problems == []


def test_every_key_an_object_requires_is_declared():
    problems = []
    for spec in specs():
        for where, name, schema in declared_fields(spec):
            for path, node in nested(f"{where}:{name}", schema):
                missing = set(node.get("required") or ()) - set(node.get("properties") or ())
                if missing:
                    problems.append(f"{path}: {sorted(missing)}")
    assert problems == []


def named_actions(description: str) -> set[str]:
    """The actions a field description says require or take it ("Required for a, b; optional
    for c.") and those it describes by name ("write: Keywords ...")."""
    names: set[str] = set()
    for listed in re.findall(r"(?:[Rr]equired|[Oo]ptional) for ([a-z_, ]+)", description):
        names.update(part.strip() for part in listed.split(","))
    for listed in re.findall(r"(?:^|\. )([a-z_]+(?:, [a-z_]+)*): ", description):
        names.update(part.strip() for part in listed.split(","))
    return names


def test_a_field_is_advertised_for_an_action_only_if_the_validator_accepts_it():
    """What the tool list says an action takes and what ``validate_args`` lets through agree."""
    problems = []
    for role, spec, action in role_actions():
        accepted = spec.accepted(action)
        for name, schema in spec.properties(role).items():
            if name == "action":
                continue
            args = {"action": action.name, name: VALUES[schema.get("type")]}
            refused = (
                f"{name} is not a field of {action.name}" in validate_args(spec, role, args)[1]
            )
            if refused == (name in accepted):
                problems.append(f"{role} {spec.name}.{action.name}: {name}: validator disagrees")
            if refused and action.name in named_actions(schema.get("description", "")):
                problems.append(f"{role} {spec.name}.{action.name}: {name}: advertised, refused")
    assert problems == []


def test_end_condition_says_it_needs_end_detail():
    """A store check, not the validator, requires the pair, so only this text can warn a model."""
    properties = catalog.TOOLS["task"].schema("orchestrator")["parameters"]["properties"]
    assert "requires end_detail" in properties["end_condition"]["description"]
    assert "Required with end_condition" in properties["end_detail"]["description"]
