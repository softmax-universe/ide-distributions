"""Task types: front matter loads, checklists refuse missing or malformed deliverables (B.4)."""

from __future__ import annotations

import pytest

from research_core import task_types
from research_core.task_types import TYPES, ChecklistItem, TaskType

SHA = "0123abc4567def890123abc4567def8901234567"


def test_every_type_loads_with_front_matter_and_a_body():
    assert TYPES == ("research", "improvement", "ideation", "maintenance")
    for name in TYPES:
        loaded = task_types.load(name)
        assert isinstance(loaded, TaskType)
        assert loaded.name == name
        assert isinstance(loaded.front, dict)
        assert isinstance(loaded.required, list)
        assert loaded.checklist and all(isinstance(i, ChecklistItem) for i in loaded.checklist)
        assert loaded.body.strip()
        assert "```" not in loaded.body


def test_unknown_type_is_refused():
    with pytest.raises(ValueError, match="bogus"):
        task_types.load("bogus")
    with pytest.raises(ValueError, match="bogus"):
        task_types.check("bogus", {})


def test_research_needs_claims_or_none_with_a_reason():
    claims = task_types.load("research").checklist[0]
    assert claims.key == "claims" and claims.none_ok and claims.min_items == 1
    label = f"claims: {claims.text}"
    assert task_types.check("research", {}) == [label]
    assert task_types.check("research", {"claims": []}) == [label]
    assert task_types.check("research", {"claims": ["C-1", "C-2"]}) == []
    assert task_types.check("research", {"claims": "none: the lever had no effect"}) == []
    assert task_types.check("research", {"claims": "none:"})[0].startswith(label)
    assert task_types.check("research", {"claims": "none"})[0].startswith(label)
    bad = task_types.check("research", {"claims": ["C-1", "X-2"]})
    assert len(bad) == 1 and bad[0].startswith(label) and "X-2" in bad[0]
    assert task_types.check("research", {"claims": "C-1"})[0].startswith(label)


def test_improvement_names_every_missing_item_at_once():
    keys = [item.key for item in task_types.load("improvement").checklist]
    assert keys == ["claims", "experiment", "versions", "submission_candidate", "submission_reason"]
    missing = task_types.check("improvement", {})
    assert len(missing) == 5
    complete = {
        "claims": ["C-3"],
        "experiment": "X-1",
        "versions": ["V-1", "V-2"],
        "submission_candidate": "yes",
        "submission_reason": "beats the champion by 3 points",
    }
    assert task_types.check("improvement", complete) == []
    # Claims are not optional for an improvement Task.
    assert len(task_types.check("improvement", {**complete, "claims": "none: nothing"})) == 1
    assert len(task_types.check("improvement", {**complete, "experiment": ["X-1"]})) == 1
    assert len(task_types.check("improvement", {**complete, "submission_candidate": "maybe"})) == 1
    assert len(task_types.check("improvement", {**complete, "submission_reason": "  "})) == 1


def test_ideation_accepts_one_to_five_proposals_or_an_explicit_empty_result():
    item = task_types.load("ideation").checklist[0]
    assert (item.key, item.min_items, item.max_items) == ("proposed_tasks", 1, 5)
    assert task_types.check("ideation", {"proposed_tasks": ["T-2", "T-3"]}) == []
    assert task_types.check("ideation", {"proposed_tasks": [f"T-{n}" for n in range(2, 7)]}) == []
    assert task_types.check("ideation", {"proposed_tasks": ["T-2"]}) == []
    six = task_types.check("ideation", {"proposed_tasks": [f"T-{n}" for n in range(2, 8)]})
    assert len(six) == 1 and "at most 5" in six[0]
    assert task_types.check("ideation", {"proposed_tasks": "none: no ideas"}) == []
    assert task_types.check("ideation", {"proposed_tasks": []})


def test_maintenance_needs_commit_and_summary_and_capability_for_repairs():
    items = {item.key: item for item in task_types.load("maintenance").checklist}
    assert set(items) == {"commit", "summary", "capability"}
    assert items["capability"].required_when == "capability"
    done = {"commit": SHA, "summary": "pinned the build toolchain"}
    assert task_types.check("maintenance", done) == []
    assert task_types.check("maintenance", done, task_fields={"capability": None}) == []
    repair = task_types.check("maintenance", done, task_fields={"capability": "build"})
    assert repair == [f"capability: {items['capability'].text}"]
    repaired = {**done, "capability": "build"}
    assert task_types.check("maintenance", repaired, task_fields={"capability": "build"}) == []
    short = task_types.check("maintenance", {"commit": "abc", "summary": "x"})
    assert len(short) == 1 and short[0].startswith(f"commit: {items['commit'].text}")
    assert task_types.check("maintenance", {"commit": SHA[:7], "summary": "x"}) == []


def test_required_task_fields_are_checked_when_fields_are_given(monkeypatch):
    loaded = task_types.load("research")
    with_required = TaskType(
        loaded.name, {**loaded.front, "required": ["experiment_id"]}, loaded.body
    )
    monkeypatch.setattr(task_types, "load", lambda name: with_required)
    ok = {"claims": ["C-1"]}
    assert task_types.check("research", ok) == []
    assert task_types.check("research", ok, task_fields={"experiment_id": 4}) == []
    missing = task_types.check("research", ok, task_fields={"experiment_id": None})
    assert missing == ["Task field experiment_id must be set"]
