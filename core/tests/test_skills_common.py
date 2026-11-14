"""The shared skills under skills/common (plan 07 Task 7.3): front matter, budgets, vocabulary,
tool names.

Every skill directory holds a SKILL.md whose front matter names the directory and gives a
one-sentence description of at most 60 characters; the body stays under 700 words; no skill
names a store path, the retired vocabulary or a tool action the catalog does not have.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import research_core
from research_core.stats.decision_rule import REFUTING_CODES
from research_core.store.settings import SETTINGS
from research_core.tools.catalog import ROLES, TOOL_MODULES, tools_for

COMMON = Path(research_core.__file__).resolve().parent / "skills" / "common"
EXPECTED = {
    "method": {
        "experiment",
        "survey",
        "run-eval",
        "calibrate-noise",
        "candidate",
        "diagnose",
        "replay-diagnosis",
        "scouting",
        "analyze-opponents",
        "minecraft-controller",
    },
    "records": {"writing-claims"},
    "platform": {
        "submission",
        "credits",
        "softmax-api-routes",
        "coworld-source",
        "expert-consultation",
    },
    "ops": {"repair", "environment"},
}
MAX_DESCRIPTION_CHARS = 60
MAX_BODY_WORDS = 700
# Store paths never appear in a skill (B.10); the retired vocabulary never does either.
FORBIDDEN_EXACT = (".state", "store.db", "kanban_")
FORBIDDEN_WORDS = ("promotion", "kanban", "coordinator")
# Tools still being written: their planned actions count until the module lands.
PLANNED_ACTIONS: dict[str, set[str]] = {"forum_post": {"post", "comment"}, "wiki_edit": set()}
TOOL_NAMES = sorted({module.rsplit(".", 1)[1] for module in TOOL_MODULES})
# `tool.action` tokens; a name inside a URL or a dotted path (``https://github.com``) is not one.
TOOL_ACTION = re.compile(r"(?<![/.\w])(" + "|".join(TOOL_NAMES) + r")\.([a-z_]+)\b")


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """The ``---`` block as ``key: value`` pairs (stdlib only, like Hermes's fallback parser),
    and the body after it."""
    assert text.startswith("---\n"), "the file must start with a front matter block"
    end = text.find("\n---\n", 4)
    assert end > 0, "the front matter block never closes"
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        assert sep, f"front matter line without a colon: {line!r}"
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, text[end + len("\n---\n") :]


def skill_dirs() -> list[Path]:
    return sorted(p for p in COMMON.glob("*/*") if p.is_dir())


def known_actions() -> dict[str, set[str]]:
    """Tool name to the actions any role has; a flat tool maps to an empty set."""
    known: dict[str, set[str]] = {}
    for role in ROLES:
        for spec in tools_for(role):
            known.setdefault(spec.name, set()).update(a.name for a in spec.actions or ())
    for name, actions in PLANNED_ACTIONS.items():
        known.setdefault(name, set(actions))
    return known


SKILLS = skill_dirs()
SKILL_IDS = [f"{p.parent.name}/{p.name}" for p in SKILLS]


def test_the_expected_skills_exist():
    found = {
        category: {p.name for p in (COMMON / category).iterdir() if p.is_dir()}
        for category in EXPECTED
    }
    assert found == EXPECTED


@pytest.mark.parametrize("skill", SKILLS, ids=SKILL_IDS)
def test_front_matter(skill: Path):
    fields, _ = parse_front_matter((skill / "SKILL.md").read_text())
    assert fields.get("name") == skill.name
    description = fields.get("description", "")
    assert description, "a description is required"
    assert len(description) <= MAX_DESCRIPTION_CHARS, f"{len(description)} chars: {description!r}"
    assert description.endswith("."), description
    assert description.count(".") == 1, f"one sentence: {description!r}"
    assert ":" not in description, "a colon breaks YAML front matter"
    assert description.startswith("Use "), "the description says when to use the skill"


@pytest.mark.parametrize("skill", SKILLS, ids=SKILL_IDS)
def test_body_budget(skill: Path):
    _, body = parse_front_matter((skill / "SKILL.md").read_text())
    words = len(body.split())
    assert words <= MAX_BODY_WORDS, f"{words} words"


@pytest.mark.parametrize("skill", SKILLS, ids=SKILL_IDS)
def test_vocabulary_and_store_paths(skill: Path):
    for file in sorted(skill.rglob("*.md")):
        text = file.read_text()
        lowered = text.lower()
        for needle in FORBIDDEN_EXACT:
            assert needle not in text, f"{file.name} mentions {needle!r}"
        for word in FORBIDDEN_WORDS:
            assert word not in lowered, f"{file.name} mentions {word!r}"


@pytest.mark.parametrize("skill", SKILLS, ids=SKILL_IDS)
def test_tool_actions_exist(skill: Path):
    known = known_actions()
    problems: list[str] = []
    for file in sorted(skill.rglob("*.md")):
        for tool, action in TOOL_ACTION.findall(file.read_text()):
            is_setting = f"{tool}.{action}" in SETTINGS  # xp.ask_above_requests_per_experiment
            if action not in known.get(tool, set()) and not is_setting:
                problems.append(f"{file.name}: {tool}.{action}")
    assert not problems, problems


@pytest.mark.parametrize("skill", SKILLS, ids=SKILL_IDS)
def test_game_specific_steps_point_at_the_mixin(skill: Path):
    """A skill that mentions a game binding names it the way the mixin copies it."""
    text = (skill / "SKILL.md").read_text()
    if "binding" in text or "game-specific" in text:
        assert "<coworld>-" in text, (
            "game-specific steps say: see the mixin's <coworld>-<binding> skill"
        )


def test_every_named_skill_exists():
    """``the `credits` skill`` in any shipped text names a skill a Session can load."""
    root = COMMON.parent
    shipped = {path.parent.name for path in root.rglob("SKILL.md")}
    package = root.parent
    named = {}
    for folder in ("prompts", "skills", "task_types", "watcher/wakes"):
        for file in sorted((package / folder).rglob("*.md")):
            for name in re.findall(r"`([a-z][a-z-]*)` skill", file.read_text()):
                named.setdefault(name, file.name)
    assert {name: file for name, file in named.items() if name not in shipped} == {}


def test_the_experiment_skill_names_every_refuting_code():
    text = (COMMON / "method" / "experiment" / "SKILL.md").read_text()
    assert [code for code in sorted(REFUTING_CODES) if f"`{code}`" not in text] == []
