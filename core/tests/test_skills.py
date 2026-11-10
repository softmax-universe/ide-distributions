"""Skills under skills/{common,orchestrator,researcher} and the task type bodies (entry 7).

Every SKILL.md keeps Hermes's front matter rules (name equals the directory, a one-sentence
description of at most 60 characters), a body of at most 700 words, no store path or retired
vocabulary, and tool references (`tool.action`) that exist in the catalog for the role the
skill root implies. The task type bodies parse and teach the `failure` fields.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import research_core
from research_core import task_types
from research_core.store.settings import SETTINGS
from research_core.tools.catalog import ROLES, tools_for

SKILLS = Path(research_core.__file__).parent / "skills"
PROMPTS = SKILLS.parent / "prompts"
TASK_TYPES = SKILLS.parent / "task_types"
WAKES = SKILLS.parent / "watcher" / "wakes"
ROOT_ROLES: dict[str, tuple[str, ...]] = {
    "common": ROLES,
    "orchestrator": ("orchestrator",),
    "researcher": ("researcher",),
}
DESCRIPTION_LIMIT = 60
BODY_WORD_LIMIT = 700
TASK_TYPE_WORD_LIMIT = 300
FORBIDDEN = (".state", "store.db", "promotion", "kanban", "coordinator")
TOOL_REFERENCE = re.compile(
    r"(?<![/.\w])(goal|task|claim|experiment|version|directive|settings|ui|workspace|upload"
    r"|submit|xp|platform|forum_post|wiki_edit|softmax_api|github)"
    r"\.([a-z_]+)\b"
)
EXPECTED_SKILLS = {
    "orchestrator": {
        "review",
        "working-with-the-ide",
        "merge-conflicts",
        "mixin-from-scratch",
        "user-identity",
        "goals-and-tasks",
        "client-coplay",
        "client-web-ide",
    },
    "researcher": {"working-a-task", "proposing"},
}

FRONT_MATTER = re.compile(r"\A---\n(?P<front>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)


def skill_files() -> list[Path]:
    return sorted(SKILLS.glob("*/**/SKILL.md"))


def skill_id(path: Path) -> str:
    return path.relative_to(SKILLS).parent.as_posix()


def parse(path: Path) -> tuple[dict, str]:
    match = FRONT_MATTER.match(path.read_text(encoding="utf-8"))
    assert match is not None, f"{skill_id(path)}: SKILL.md must open with a --- front matter block"
    front = yaml.safe_load(match.group("front"))
    assert isinstance(front, dict), f"{skill_id(path)}: front matter must be a mapping"
    return front, match.group("body")


def roles_for(path: Path) -> tuple[str, ...]:
    root = path.relative_to(SKILLS).parts[0]
    assert root in ROOT_ROLES, f"{skill_id(path)}: unknown skill root {root!r}"
    return ROOT_ROLES[root]


def catalog_actions(role: str) -> set[tuple[str, str]]:
    """Every (tool, action) a role can call; a flat tool contributes (tool, '')."""
    actions: set[tuple[str, str]] = set()
    for spec in tools_for(role):
        if spec.actions is None:
            actions.add((spec.name, ""))
        for action in spec.actions_for(role):
            actions.add((spec.name, action.name))
    return actions


def words(text: str) -> int:
    return len(text.split())


SKILL_PARAMS = pytest.mark.parametrize("path", skill_files(), ids=skill_id)


def test_skill_roots_hold_the_expected_skills():
    for root, expected in EXPECTED_SKILLS.items():
        found = {
            path.relative_to(SKILLS / root).parent.as_posix()
            for path in (SKILLS / root).rglob("SKILL.md")
        }
        assert found == expected, f"{root}: {sorted(found ^ expected)}"


@SKILL_PARAMS
def test_front_matter_names_the_directory_and_describes_when(path):
    front, _ = parse(path)
    assert front.get("name") == path.parent.name, f"{skill_id(path)}: name must equal the directory"
    description = front.get("description")
    assert isinstance(description, str) and description.strip(), f"{skill_id(path)}: description"
    assert len(description) <= DESCRIPTION_LIMIT, f"{skill_id(path)}: {len(description)} chars"
    assert description.endswith("."), f"{skill_id(path)}: description must end with a period"
    assert ". " not in description, f"{skill_id(path)}: description must be one sentence"
    assert description.startswith("Use "), f"{skill_id(path)}: description says when to use it"


@SKILL_PARAMS
def test_body_fits_the_word_budget(path):
    _, body = parse(path)
    assert body.strip(), f"{skill_id(path)}: empty body"
    assert words(body) <= BODY_WORD_LIMIT, f"{skill_id(path)}: {words(body)} words"


@SKILL_PARAMS
def test_no_store_path_or_retired_vocabulary(path):
    text = path.read_text(encoding="utf-8").lower()
    found = [word for word in FORBIDDEN if word in text]
    assert not found, f"{skill_id(path)}: {found}"


@SKILL_PARAMS
def test_every_tool_action_exists_for_the_skill_role(path):
    _, body = parse(path)
    roles = roles_for(path)
    allowed = set.union(*(catalog_actions(role) for role in roles))
    setting_keys = set(SETTINGS)
    missing = sorted(
        {
            f"{tool}.{action}"
            for tool, action in TOOL_REFERENCE.findall(body)
            if f"{tool}.{action}" not in setting_keys and (tool, action) not in allowed
        }
    )
    assert not missing, f"{skill_id(path)} ({'/'.join(roles)}): unknown tool actions {missing}"


SHIPPED_TEXTS = [
    *skill_files(),
    *sorted(TASK_TYPES.glob("*.md")),
    *sorted(WAKES.glob("*.md")),
    *(path for root in ("common", *ROLES) for path in sorted((PROMPTS / root).glob("*.md"))),
]
# A call as prose writes it: `tool.action {field, field: value}` or `tool_name {field}`.
CALL_IN_PROSE = re.compile(r"`([a-z_]+(?:\.[a-z_]+)?) \{([^`]*)\}`")


def tool_fields() -> dict[str, set[str]]:
    """Every name a call can use, as ``tool.action`` and ``split_tool``, with its fields."""
    fields: dict[str, set[str]] = {}
    for role in ROLES:
        for spec in tools_for(role):
            if spec.actions is None:
                fields.setdefault(spec.name, set()).update(spec.params)
            for action in spec.actions_for(role):
                fields.setdefault(f"{spec.name}.{action.name}", set()).update(action.params)
    return fields


def top_level_fields(text: str) -> list[str]:
    """The field names of ``a, b: {c, d}, e: [f]``: nested values are skipped."""
    names, depth, current = [], 0, ""
    for char in text + ",":
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        if char == "," and depth == 0:
            names.append(current.split(":")[0].strip())
            current = ""
        else:
            current += char
    return [name for name in names if re.fullmatch(r"[a-z_]+", name)]


@pytest.mark.parametrize(
    "path", SHIPPED_TEXTS, ids=lambda p: p.relative_to(SKILLS.parent).as_posix()
)
def test_every_call_in_prose_uses_a_real_tool_and_its_real_fields(path):
    known = tool_fields()
    tools = {name.split(".")[0] for name in known}
    problems = []
    for name, body in CALL_IN_PROSE.findall(path.read_text(encoding="utf-8")):
        if name not in known:
            # `decision_rule {...}` is a record's field. Only a tool's name or prefix is a call.
            if re.split(r"[._]", name)[0] in tools:
                problems.append(f"{name} is not a tool or action")
            continue
        problems += [
            f"{name} has no field {field}"
            for field in top_level_fields(body)
            if field not in known[name]
        ]
    assert not problems, problems


@pytest.mark.parametrize("type", task_types.TYPES)
def test_task_type_body_teaches_the_loop(type):
    body = task_types.load(type).body
    assert words(body) <= TASK_TYPE_WORD_LIMIT, f"{type}: {words(body)} words"
    for needle in ("task_complete", "task_ask"):
        assert needle in body, f"{type}: body must name {needle}"
    lowered = body.lower()
    assert not [w for w in FORBIDDEN if w in lowered], f"{type}: forbidden vocabulary"
    researcher = catalog_actions("researcher")
    missing = sorted(
        {
            f"{tool}.{action}"
            for tool, action in TOOL_REFERENCE.findall(body)
            if (tool, action) not in researcher and f"{tool}.{action}" not in SETTINGS
        }
    )
    assert not missing, f"{type}: unknown Researcher tool actions {missing}"


def test_maintenance_body_forbids_versions():
    body = task_types.load("maintenance").body
    assert "never a Version" in body
