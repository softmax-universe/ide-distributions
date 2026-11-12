"""Cogent learning on the record store: flag, stale rules, briefing items, web evidence, the
watcher notice, the SOUL hook. Flag off changes nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research_core import brain
from research_core.brain import soul
from research_core.plugin import briefing
from research_core.prompts import build
from research_core.store import claims, evidence_types, notices
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.store.errors import StoreError
from research_core.watcher import brain_check
from tests.watcher._support import FakeClock, advance, make_ctx

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Actor.parse("harness")
CODE = {"type": "code", "ref": "policy/agent.py:10-20@0123abcd", "stance": "supports", "note": "n"}
WEB = {"type": "web", "ref": "https://example.com/tips (fetched 2026-11-01)", "stance": "supports"}
ON = {"IDE_BRAIN": "1"}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("IDE_BRAIN", "1")


def _claim(store, tags, n=1, evidence=None):
    return claims.write(
        store,
        HARNESS,
        summary=f"Finding {n}: scouts first win more often.",
        body=f"Detail {n}: 12 of 20 episodes.",
        tags=tags,
        evidence=[evidence or CODE],
    )


def _store(tmp_path, clock=None):
    return Store.open(tmp_path / "s.db", kind="coworld", clock=clock or FakeClock())


# --- flag off = no behaviour change ---------------------------------------------------------


def test_flag_off_soul_is_byte_identical():
    for role in build.ROLES:
        committed = (ROOT / role / "SOUL.md").read_text(encoding="utf-8")
        assert committed == build.render(role) == build.render(role, brain=False)
        assert build.render(role, brain=True).startswith(committed.rstrip("\n"))
        assert build.render(role, brain=True) != committed


def test_flag_off_reads_briefing_and_evidence_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("IDE_BRAIN", raising=False)
    store = _store(tmp_path)
    claim = _claim(store, ["rule"])
    assert "stale" not in claims.read(store, claim["id"])
    assert "web" not in evidence_types._types()
    problems = evidence_types.check_ref(store, "web", WEB["ref"])
    assert problems and "not registered" in problems[0]
    with pytest.raises(StoreError):
        _claim(store, ["unverified"], 2, WEB)


def test_flag_off_prepare_hook_writes_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "SOUL.md").write_text("installed soul\n")
    for env in ({}, {"IDE_BRAIN": "0"}, {"IDE_BRAIN": ""}):
        assert soul.apply(home, "orchestrator", {**env, "IDE_WORKSPACE": str(tmp_path / "w")}) == []
    assert (home / "SOUL.md").read_text() == "installed soul\n"
    assert not (tmp_path / "w").exists()


# --- (2) stale ------------------------------------------------------------------------------


def test_stale_uses_last_supporting_evidence_not_added_date(tmp_path, on):
    clock = FakeClock()
    store = _store(tmp_path, clock)
    rule = _claim(store, ["rule"])
    other = _claim(store, ["lesson"], 2)
    assert claims.read(store, rule["id"])["stale"] is False
    clock.advance(86400 * 91)
    assert claims.read(store, rule["id"])["stale"] is True
    assert claims.read(store, other["id"])["stale"] is False  # only rules go stale
    claims.add_evidence(store, HARNESS, rule["id"], type="code", ref=CODE["ref"], stance="supports")
    assert claims.read(store, rule["id"])["stale"] is False  # applying it again = fresh
    clock.advance(86400 * 91)
    claims.add_evidence(
        store, HARNESS, rule["id"], type="code", ref=CODE["ref"], stance="contradicts"
    )
    assert claims.read(store, rule["id"])["stale"] is True  # contradiction is not a use


def test_is_stale_boundaries():
    now = datetime(2026, 11, 12, tzinfo=UTC)
    rule = {
        "tags": ["rule"],
        "status": "active",
        "freshness": (now - timedelta(days=90)).isoformat(),
    }
    assert brain.is_stale(rule, now) is False
    rule["freshness"] = (now - timedelta(days=91)).isoformat()
    assert brain.is_stale(rule, now) is True
    assert brain.is_stale({**rule, "status": "superseded"}, now) is False
    assert brain.is_stale({"tags": ["rule"], "created": rule["freshness"]}, now) is True


# --- (3) web evidence -----------------------------------------------------------------------


def test_web_evidence_validates_and_inspects(tmp_path, on):
    store = _store(tmp_path)
    assert evidence_types.check_ref(store, "web", WEB["ref"]) == []
    for bad in (
        "not a url",
        "https://x.io",
        "https://x.io (fetched 2026-13-45)",
        "ftp://x (fetched 2026-01-01)",
    ):
        assert evidence_types.check_ref(store, "web", bad), bad
    claim = _claim(store, ["unverified"], 1, WEB)
    assert claims.read(store, claim["id"])["evidence"][0]["type"] == "web"
    env = evidence_types.InspectEnv(store=store)
    got = evidence_types.inspect_evidence("web", WEB["ref"], env)
    assert got["url"] == "https://example.com/tips" and got["fetched_at"] == "2026-11-01"


# --- (1) briefing ---------------------------------------------------------------------------


def _briefing_ctx(tmp_path):
    from tests.plugin.test_briefing import make_ctx as make_tool_ctx

    return make_tool_ctx(tmp_path)


def test_briefing_lists_rules_and_gotchas_only_with_the_flag(tmp_path, monkeypatch):
    from tests.plugin.test_briefing import NOW

    ctx = _briefing_ctx(tmp_path)
    _claim(ctx.store, ["rule"], 1)
    _claim(ctx.store, ["gotcha"], 2)
    _claim(ctx.store, ["preference"], 3)
    monkeypatch.delenv("IDE_BRAIN", raising=False)
    off = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "Rules and gotchas" not in off and "(rule)" not in off and "C-3: Finding 3" in off
    monkeypatch.setenv("IDE_BRAIN", "1")
    on_text = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert "C-1 (rule): Finding 1" in on_text and "C-2 (gotcha): Finding 2" in on_text
    assert on_text.index("C-3: Finding 3") < on_text.index("C-1 (rule)")  # beside preferences
    assert len(on_text) <= 3600


def test_briefing_marks_a_stale_rule(tmp_path, monkeypatch):
    from tests.plugin.test_briefing import NOW

    monkeypatch.setenv("IDE_BRAIN", "1")
    ctx = _briefing_ctx(tmp_path)
    _claim(ctx.store, ["rule"], 1)
    late = NOW + timedelta(days=200)
    assert "[stale: unused 90 days]" in briefing.render(ctx, "chat1", role="orchestrator", now=late)


# --- (4) watcher check ----------------------------------------------------------------------


def test_watcher_notifies_once_for_stale_rules_and_changes_nothing(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.env = ON
    rule = _claim(ctx.store, ["rule"])
    assert brain_check.run(ctx) == {"stale_rules": []}
    advance(ctx, 86400 * 91)
    assert brain_check.run(ctx) == {"stale_rules": [rule["id"]]}
    texts = [n["text"] for n in notices.unread(ctx.wstore, coworld_id=ctx.coworld.coworld_id)]
    assert any(rule["id"] in t and "nothing was changed" in t for t in texts)
    assert brain_check.run(ctx) == {"stale_rules": []}  # once per stretch
    assert claims.read(ctx.store, rule["id"])["status"] == "active"  # never retires
    claims.add_evidence(
        ctx.store, HARNESS, rule["id"], type="code", ref=CODE["ref"], stance="supports"
    )
    brain_check.run(ctx)
    advance(ctx, 86400 * 91)
    assert brain_check.run(ctx)["stale_rules"] == [rule["id"]]  # a later lapse notifies again


def test_watcher_check_is_inert_without_the_flag(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.env = {}
    _claim(ctx.store, ["rule"])
    advance(ctx, 86400 * 200)
    assert brain_check.run(ctx) == {"stale_rules": []}
    assert notices.unread(ctx.wstore) == []


# --- (5) prompt section ---------------------------------------------------------------------


def test_learning_section_fits_the_allowance_and_names_the_path():
    for role in build.ROLES:
        assert build.words(build.brain_section(role)) <= build.BRAIN_ALLOWANCE[role]
        assert build.check(role, brain=True) == []
    text = build.brain_section("orchestrator")
    for needle in (
        "lesson",
        "rule",
        "gotcha",
        "unverified",
        "claim_add_evidence",
        "supports",
        "skill_manage",
        "Experiment",
        "twice",
    ):
        assert needle in text


# --- SOUL hook ------------------------------------------------------------------------------


def test_soul_hook_writes_and_restores(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {**ON, "IDE_WORKSPACE": str(tmp_path / "w")}
    (home / "SOUL.md").write_text("installed")
    assert soul.apply(home, "orchestrator", env)
    assert (home / "SOUL.md").read_text() == build.render("orchestrator", brain=True)
    assert soul.apply(home, "orchestrator", env) == []
    off = {"IDE_WORKSPACE": str(tmp_path / "w")}
    assert soul.apply(home, "orchestrator", off)
    assert (home / "SOUL.md").read_text() == build.render("orchestrator")
    (home / "SOUL.md").write_text("custom")
    assert soul.apply(home, "orchestrator", off) == []


# --- the profile prepare scripts' guard -----------------------------------------------------


def _run_prepare_script(role, monkeypatch, home, flag):
    import runpy
    import sys

    from research_core import prepare as prepare_module
    from research_core.brain import soul as soul_module

    calls = {"prepare": 0, "apply": 0}
    real_apply = soul_module.apply

    def counting_apply(*args, **kwargs):
        calls["apply"] += 1
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(soul_module, "apply", counting_apply)
    monkeypatch.setattr(prepare_module, "HermesConfig", lambda hermes: object())
    monkeypatch.setattr(prepare_module, "load_profile_env", lambda *a: None)
    monkeypatch.setattr(prepare_module, "prepare", lambda **kw: calls.__setitem__("prepare", 1))
    for name, value in (("HERMES_HOME", home), ("IDE_BRAIN", flag)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(value))
    script = ROOT / role / "scripts" / "prepare.py"
    monkeypatch.setattr(
        sys, "argv", [str(script), "--source", str(ROOT / role), "--hermes", "hermes"]
    )
    runpy.run_path(str(script), run_name="__main__")
    return calls


@pytest.mark.parametrize("role", build.ROLES)
def test_prepare_script_guard(role, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    plain = build.render(role)
    (home / "SOUL.md").write_text(plain)
    # flag off, plain SOUL: the brain code is not even called
    calls = _run_prepare_script(role, monkeypatch, home, None)
    assert calls == {"prepare": 1, "apply": 0} and (home / "SOUL.md").read_text() == plain
    # HERMES_HOME unset: no KeyError, prepare still runs, nothing applied
    calls = _run_prepare_script(role, monkeypatch, None, "1")
    assert calls == {"prepare": 1, "apply": 0}
    # flag on: the learning section is written
    calls = _run_prepare_script(role, monkeypatch, home, "1")
    assert calls["apply"] == 1
    assert (home / "SOUL.md").read_text() == build.render(role, brain=True)
    # flag turned off later: the marker in the installed SOUL triggers the restore
    calls = _run_prepare_script(role, monkeypatch, home, None)
    assert calls["apply"] == 1 and (home / "SOUL.md").read_text() == plain


# --- briefing cap ---------------------------------------------------------------------------


def test_briefing_caps_rules_and_gotchas_and_keeps_preferences(tmp_path, monkeypatch):
    from tests.plugin.test_briefing import NOW

    monkeypatch.setenv("IDE_BRAIN", "1")
    ctx = _briefing_ctx(tmp_path)
    for n in range(1, 4):
        _claim(ctx.store, ["preference"], n)
    for n in range(4, 29):
        _claim(ctx.store, ["rule"], n)
    for n in range(29, 54):
        _claim(ctx.store, ["gotcha"], n)
    text = briefing.render(ctx, "chat1", role="orchestrator", now=NOW)
    assert len(text) <= 3600
    for n in range(1, 4):  # the preference claims are not squeezed out
        assert f"C-{n}: Finding {n}" in text
    section = text[text.index("Rules and gotchas") :]
    shown = section.count("(rule)") + section.count("(gotcha)")
    assert 0 < shown <= 2 * briefing.RULE_CLAIMS_LOADED
    assert "more, search with claim.search" in section
