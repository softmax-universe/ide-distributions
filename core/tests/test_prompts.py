"""The generated SOULs: budgets, the Records section, the leak test, content rules, the CLI."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from research_core.prompts import build
from research_core.tools.catalog import ROLES, tools_for

REPO = Path(__file__).resolve().parents[2]
BUILD_PROMPTS = REPO / "tools" / "build_prompts.py"
SUMMARY_LINE = "Directives and records are stored and survive it"
FORBIDDEN = ("promotion", "kanban", "coordinator", ".state", "store.db")

# Every tool name the catalog knows, so a backticked `name` or `name.action` in a SOUL is checked
# against the role's real catalog.
KNOWN_TOOLS = {spec.name for role in ROLES for spec in tools_for(role)}
TOOL_REFERENCE = re.compile(r"`([a-z_]+)(?:\.([a-z_]+))?[^`]*`")


def paragraphs(text: str) -> list[str]:
    """Blocks of a rendered SOUL without HTML comments and headings."""
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    blocks = [block.strip() for block in re.split(r"\n\s*\n", stripped)]
    return [block for block in blocks if block and not block.startswith("#")]


@pytest.mark.parametrize("role", ROLES)
def test_word_budget(role):
    assert build.word_count(role) <= build.BUDGETS[role]
    assert build.check(role) == []


@pytest.mark.parametrize("role", ROLES)
def test_records_section_is_about_four_hundred_words(role):
    low, high = build.RECORDS_RANGE
    assert low <= build.records_words(role) <= high
    titles = [title for title, _ in build.sections(build.render(role))]
    assert build.RECORDS_TITLE in titles


def test_vocabulary_is_compact_and_complete():
    vocabulary = build.render_vocabulary()
    assert build.words(vocabulary) <= build.VOCABULARY_BUDGET
    for term in ("Coworld", "League", "Division", "Round", "Episode", "Champion", "Player"):
        assert f"- **{term}**:" in vocabulary
    assert "**Don't use**" in vocabulary
    assert "Notes for these profiles" not in vocabulary


@pytest.mark.parametrize("role", ROLES)
def test_no_part_of_the_other_role_leaks(role):
    text = build.render(role)
    assert "<!-- role:" not in text
    for part in build.load_parts():
        if part.role == "common":
            assert build.render_part(part, role) in text
        elif part.role == role:
            assert build.render_part(part, role) in text
        else:
            assert build.render_part(part, part.role) not in text


def test_check_reports_a_leak_and_a_budget_overrun(monkeypatch):
    monkeypatch.setitem(build.BUDGETS, "researcher", 10)
    monkeypatch.setattr(build, "RECORDS_RANGE", (1, 2))
    problems = build.check("researcher")
    assert any("over the budget" in problem for problem in problems)
    assert any("Records section" in problem for problem in problems)

    orchestrator_part = next(p for p in build.load_parts() if p.role == "orchestrator")
    original = build.render

    def leaky(role):
        return original(role) + "\n" + build.render_part(orchestrator_part, "orchestrator")

    monkeypatch.setattr(build, "render", leaky)
    assert any("leaks in" in problem for problem in build.check("researcher"))


@pytest.mark.parametrize("role", ROLES)
def test_authority_is_the_first_paragraph_and_the_guide_is_near_the_top(role):
    text = build.render(role)
    first = paragraphs(text)[0]
    assert first.startswith("The user's explicit request comes first")
    assert "refuse only what the platform refuses" in first
    assert build.sections(text)[0][0] == "Order of authority"
    assert "workspace.environment_guide" in " ".join(text.split()[:400])


@pytest.mark.parametrize("role", ROLES)
def test_section_order_follows_the_design(role):
    titles = [title for title, _ in build.sections(build.render(role))]
    expected = [
        "Order of authority",
        "Who you are",
        "Vocabulary",
        "Your role: the Orchestrator" if role == "orchestrator" else "Your role: a Researcher",
        build.RECORDS_TITLE,
        *(["Context and the UI"] if role == "orchestrator" else []),
        "How to act as your Player",
        *(["The User identity"] if role == "orchestrator" else []),
        "When something breaks",
        "If this conversation is summarised",
        "Where to find the rest",
    ]
    assert titles == expected


@pytest.mark.parametrize("role", ROLES)
def test_forbidden_words_and_the_summary_line(role):
    text = build.render(role)
    lowered = text.lower()
    for word in FORBIDDEN:
        assert word not in lowered, word
    assert SUMMARY_LINE in text


def test_the_tool_list_shows_a_role_only_the_fields_it_can_use():
    rendered = build.render_tools("researcher")
    for spec in tools_for("researcher"):
        if spec.actions is None and spec.split_from is None:
            line = next(ln for ln in rendered.splitlines() if ln.startswith(f"- `{spec.name}`"))
            for name in spec.orchestrator_fields:
                assert name not in line, f"{spec.name} shows the Researcher {name}"


@pytest.mark.parametrize("role", ROLES)
def test_every_tool_named_exists_for_the_role(role):
    catalog = {spec.name: spec for spec in tools_for(role)}
    text = build.render(role)
    for name, action in TOOL_REFERENCE.findall(text):
        if name not in KNOWN_TOOLS:
            continue  # a skill, a CLI or a setting, not a tool
        assert name in catalog, f"{role} SOUL names `{name}`, not in its catalog"
        if action:
            actions = {a.name for a in catalog[name].actions_for(role)}
            assert action in actions, f"{role} SOUL names `{name}.{action}`"


def test_records_section_names_ids_statuses_and_writers():
    text = build.render_records("orchestrator")
    assert "**Goal** (G-n)" in text and "**Task** (T-n)" in text
    assert "Statuses: proposed, unclaimed, in-progress, complete, abandoned" in text
    assert "**Session** (one per Hermes Session)" in text and "Written by the workspace." in text
    assert "**Champion** (one per League)" in text
    assert "Written by `chat_update`" in text
    researcher = build.render_records("researcher")
    assert "`chat_update`" not in researcher and "`submit`" not in researcher
    assert "Written by the Orchestrator" in researcher


def test_role_sections_carry_the_binding_rules():
    orchestrator = build.render("orchestrator")
    for needle in ("chat_update", "`decision`", "user_requested: true", "`mode`", "`command`"):
        assert needle in orchestrator
    researcher = build.render("researcher")
    for needle in ("task.claim", "task_wait", "task_complete", "outcome: succeeded"):
        assert needle in researcher
    assert "`what_failed`" in researcher and "`next_step`" in researcher
    assert "`chat_update`" not in researcher and "`submit`" not in researcher


def test_manifests_equal_the_catalog():
    manifests = build.manifests()
    for role in ROLES:
        assert manifests[role] == [spec.name for spec in tools_for(role)]
        path = REPO / build.MANIFEST_PATH.format(role=role)
        assert yaml.safe_load(path.read_text())["provides_tools"] == manifests[role]


def test_set_provides_tools_keeps_the_rest_of_the_manifest():
    original = "name: x\n# a comment\nprovides_tools: []\nprovides_hooks:\n  - post_tool_call\n"
    rewritten = build.set_provides_tools(original, ["goal", "task"])
    assert rewritten == (
        "name: x\n# a comment\nprovides_tools:\n  - goal\n  - task\nprovides_hooks:\n"
        "  - post_tool_call\n"
    )
    assert build.set_provides_tools(rewritten, []) == original
    with pytest.raises(ValueError, match="provides_tools"):
        build.set_provides_tools("name: x\n", ["goal"])


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BUILD_PROMPTS), *args], capture_output=True, text=True, check=False
    )


def test_check_passes_after_write_and_fails_on_a_stale_file(tmp_path):
    for role in ROLES:
        shutil.copytree(REPO / role, tmp_path / role)
    root = str(tmp_path)
    written = run_cli("--write", "--manifests", "--root", root)
    assert written.returncode == 0, written.stderr
    checked = run_cli("--check", "--root", root)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    for role in ROLES:
        assert (tmp_path / build.SOUL_PATH.format(role=role)).read_text() == build.render(role)

    soul = tmp_path / build.SOUL_PATH.format(role="researcher")
    soul.write_text(soul.read_text() + "\nstale\n")
    stale = run_cli("--check", "--root", root)
    assert stale.returncode == 1
    assert "researcher/SOUL.md differs" in stale.stdout


def test_committed_souls_and_manifests_are_current():
    result = run_cli("--check")
    assert result.returncode == 0, (
        "run `uv run python tools/build_prompts.py --write --manifests`\n" + result.stdout
    )
