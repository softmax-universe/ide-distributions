"""Settings: Appendix H defaults, per-key scope, typed and cited updates (design B.9, A2.4)."""

from __future__ import annotations

import pytest

from research_core.store import settings
from research_core.store.errors import StoreError
from research_core.store.settings import SETTINGS

DEFAULTS = {
    "paused": False,
    "max_researchers": 2,
    "approval_required.task_types": [],
    "submissions.mode": "autonomous",
    "submissions.until": None,
    "submissions.max_count": None,
    "submissions.quality_bar": True,
    "submissions.min_margin_over_champion": 0,
    "platform_writes.forum": "allowed",
    "platform_writes.wiki": "allowed",
    "xp.ask_above_requests_per_experiment": None,
    "ideation.park_after_rounds": 3,
    "recovery.max_attempts": 2,
    "recovery.delivery_backoff_seconds": 5,
    "recovery.receipt_timeout_seconds": 60,
    "anti_stall.hour": "04:00",
    "chat_updates.categories": ["decision", "outcome", "submission", "failure"],
    "identity.name": None,
    "identity.name_confirmed": False,
    "identity.met_user": False,
    "identity.offer_made": False,
}
WORKSPACE_KEYS = {
    "paused",
    "max_researchers",
    "recovery.max_attempts",
    "anti_stall.hour",
    "recovery.delivery_backoff_seconds",
    "recovery.receipt_timeout_seconds",
    "chat_updates.categories",
    "identity.name",
    "identity.name_confirmed",
    "identity.met_user",
    "identity.offer_made",
}


def test_registry_has_the_appendix_h_defaults_and_scopes():
    assert set(SETTINGS) == set(DEFAULTS)
    for key, default in DEFAULTS.items():
        spec = SETTINGS[key]
        assert spec.key == key
        assert spec.default == default
        assert spec.scope == ("workspace" if key in WORKSPACE_KEYS else "coworld")
        assert spec.enforced_by and spec.description


def test_get_returns_the_default_when_unset(coworld_store, workspace_store):
    assert settings.get(coworld_store, "submissions.mode") == "autonomous"
    assert settings.get(coworld_store, "approval_required.task_types") == []
    assert settings.get(workspace_store, "paused") is False
    assert settings.get(workspace_store, "max_researchers") == 2
    assert settings.get(workspace_store, "recovery.delivery_backoff_seconds") == 5
    assert settings.get(workspace_store, "recovery.receipt_timeout_seconds") == 60


def test_get_refuses_a_key_from_the_wrong_store(coworld_store, workspace_store):
    with pytest.raises(StoreError) as info:
        settings.get(workspace_store, "submissions.mode")
    assert "coworld setting" in info.value.problems[0]
    with pytest.raises(StoreError) as info:
        settings.get(coworld_store, "paused")
    assert "workspace setting" in info.value.problems[0]


def test_get_with_a_pair_picks_the_store_by_scope(coworld_store, workspace_store, harness):
    settings.update(workspace_store, harness, "paused", True, cites="message:1")
    settings.update(coworld_store, harness, "submissions.mode", "ask", cites="message:2")
    for pair in ((workspace_store, coworld_store), (coworld_store, workspace_store)):
        assert settings.get(pair, "paused") is True
        assert settings.get(pair, "submissions.mode") == "ask"
    with pytest.raises(StoreError):
        settings.get((coworld_store,), "paused")


def test_update_writes_the_value_and_an_event(workspace_store, session_actor):
    record = settings.update(workspace_store, session_actor, "max_researchers", 3, cites="m-7")
    assert record["key"] == "max_researchers"
    assert record["value"] == 3
    assert record["previous"] == 2
    assert record["default"] == 2
    assert record["scope"] == "workspace"
    assert record["cites"] == "m-7"
    assert record["updated_by"] == str(session_actor)
    assert record["updated"] == "2026-10-28T21:04:05.123456Z"
    assert settings.get(workspace_store, "max_researchers") == 3

    events = workspace_store.events_after(0)
    assert [e.kind for e in events] == ["settings.updated"]
    assert events[0].subject == "max_researchers"
    assert events[0].payload == {
        "key": "max_researchers",
        "value": 3,
        "previous": 2,
        "cites": "m-7",
    }

    again = settings.update(workspace_store, session_actor, "max_researchers", 1, cites="m-8")
    assert again["previous"] == 3
    assert settings.get(workspace_store, "max_researchers") == 1


def test_update_requires_cites_and_reports_type_problems_together(workspace_store, harness):
    with pytest.raises(StoreError) as info:
        settings.update(workspace_store, harness, "paused", "yes", cites="")
    problems = info.value.problems
    assert len(problems) == 2
    assert any("cites" in p for p in problems)
    assert any("paused" in p and "bool" in p for p in problems)
    assert settings.get(workspace_store, "paused") is False
    assert workspace_store.events_after(0) == []


@pytest.mark.parametrize(
    "store_name,key,value,expected_words",
    [
        ("workspace", "paused", 1, ("bool",)),
        ("workspace", "max_researchers", True, ("int",)),
        ("workspace", "max_researchers", "2", ("int",)),
        ("workspace", "max_researchers", -1, ("at least 0",)),
        ("workspace", "recovery.max_attempts", 1.5, ("int",)),
        ("workspace", "recovery.delivery_backoff_seconds", 0, ("at least 1",)),
        ("workspace", "recovery.delivery_backoff_seconds", True, ("int",)),
        ("workspace", "recovery.receipt_timeout_seconds", 0, ("at least 1",)),
        ("workspace", "recovery.receipt_timeout_seconds", 1.5, ("int",)),
        ("workspace", "anti_stall.hour", "25:00", ("HH:MM",)),
        ("workspace", "anti_stall.hour", "4am", ("HH:MM",)),
        ("workspace", "chat_updates.categories", "merge", ("list",)),
        ("workspace", "chat_updates.categories", ["progress"], ("decision", "progress")),
        ("coworld", "chat_updates.categories", ["merge"], ("workspace setting",)),
        ("coworld", "submissions.mode", "sometimes", ("ask", "autonomous")),
        ("coworld", "platform_writes.forum", "maybe", ("allowed", "ask", "never")),
        ("coworld", "submissions.until", "tonight", ("timestamp",)),
        ("coworld", "submissions.until", 5, ("timestamp",)),
        ("coworld", "submissions.max_count", "3", ("int",)),
        ("coworld", "submissions.quality_bar", "on", ("bool",)),
        ("coworld", "submissions.min_margin_over_champion", "0.1", ("number",)),
        ("coworld", "ideation.park_after_rounds", 0, ("at least 1",)),
        ("coworld", "approval_required.task_types", "improvement", ("list",)),
        ("coworld", "approval_required.task_types", ["bogus"], ("research", "bogus")),
        ("coworld", "max_researchers", 2, ("workspace setting",)),
        ("workspace", "submissions.mode", "ask", ("coworld setting",)),
    ],
)
def test_update_refuses_bad_values_naming_the_rule(
    coworld_store, workspace_store, harness, store_name, key, value, expected_words
):
    store = coworld_store if store_name == "coworld" else workspace_store
    with pytest.raises(StoreError) as info:
        settings.update(store, harness, key, value, cites="message:1")
    joined = " ".join(info.value.problems)
    for word in expected_words:
        assert word in joined
    assert store.one("SELECT * FROM settings WHERE key = ?", (key,)) is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("submissions.until", "2026-10-29T06:00:00Z"),
        ("submissions.until", None),
        ("submissions.max_count", 3),
        ("submissions.max_count", None),
        ("submissions.min_margin_over_champion", 0.25),
        ("submissions.min_margin_over_champion", 1),
        ("approval_required.task_types", ["improvement", "maintenance"]),
        ("platform_writes.wiki", "never"),
        ("xp.ask_above_requests_per_experiment", 40),
        ("xp.ask_above_requests_per_experiment", None),
    ],
)
def test_update_accepts_valid_values_including_null(coworld_store, harness, key, value):
    settings.update(coworld_store, harness, key, value, cites="message:1")
    assert settings.get(coworld_store, key) == value


def test_update_accepts_the_workspace_keys(workspace_store, harness):
    settings.update(workspace_store, harness, "anti_stall.hour", "23:30", cites="message:1")
    settings.update(workspace_store, harness, "paused", True, cites="message:2")
    settings.update(workspace_store, harness, "recovery.max_attempts", 0, cites="message:3")
    settings.update(
        workspace_store, harness, "recovery.delivery_backoff_seconds", 10, cites="message:4"
    )
    settings.update(
        workspace_store, harness, "recovery.receipt_timeout_seconds", 90, cites="message:5"
    )
    assert settings.all(workspace_store) == {
        "paused": True,
        "max_researchers": 2,
        "recovery.max_attempts": 0,
        "recovery.delivery_backoff_seconds": 10,
        "recovery.receipt_timeout_seconds": 90,
        "anti_stall.hour": "23:30",
        "chat_updates.categories": ["decision", "outcome", "submission", "failure"],
        "identity.name": None,
        "identity.name_confirmed": False,
        "identity.met_user": False,
        "identity.offer_made": False,
    }


def test_unknown_key_is_refused_naming_known_keys(coworld_store, harness):
    with pytest.raises(StoreError) as info:
        settings.update(coworld_store, harness, "submission.mode", "ask", cites="message:1")
    assert "submission.mode" in info.value.problems[0]
    assert "submissions.mode" in info.value.problems[0]
    with pytest.raises(StoreError):
        settings.get(coworld_store, "nope")


def test_all_lists_one_store_or_both(coworld_store, workspace_store, harness):
    settings.update(coworld_store, harness, "submissions.mode", "ask", cites="message:1")
    coworld_only = settings.all(coworld_store)
    assert set(coworld_only) == set(DEFAULTS) - WORKSPACE_KEYS
    assert coworld_only["submissions.mode"] == "ask"
    assert coworld_only["ideation.park_after_rounds"] == 3
    both = settings.all((workspace_store, coworld_store))
    assert set(both) == set(DEFAULTS)
    assert both["paused"] is False
    assert both["submissions.mode"] == "ask"
