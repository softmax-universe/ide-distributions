"""Cogent learning on the record store: flag, stale rules, briefing items, web evidence, the
watcher notice, the SOUL hook and seeds. Flag off changes nothing."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research_core import brain
from research_core.brain import _fs, seeds, soul
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


def test_watcher_notifies_once_for_stale_rules_and_idle_skills_and_changes_nothing(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.env = ON
    rule = _claim(ctx.store, ["rule"])
    skill = ctx.ws.root / "skills" / "scout-first"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: scout-first\n---\nx")
    os.utime(skill / "SKILL.md", (ctx.now.timestamp() - 86400 * 200,) * 2)
    fresh = ctx.ws.root / "skills" / "fresh"
    fresh.mkdir()
    (fresh / "SKILL.md").write_text("x")
    first = brain_check.run(ctx)
    assert first == {"stale_rules": [], "idle_skills": ["scout-first"]}
    advance(ctx, 86400 * 91)
    assert brain_check.run(ctx) == {"stale_rules": [rule["id"]], "idle_skills": []}
    texts = [
        n["text"]
        for cw in (None, ctx.coworld.coworld_id)
        for n in notices.unread(ctx.wstore, coworld_id=cw)
    ]
    assert any(rule["id"] in t and "nothing was changed" in t for t in texts)
    assert any("scout-first" in t for t in texts)
    assert brain_check.run(ctx) == {"stale_rules": [], "idle_skills": []}  # once per stretch
    assert claims.read(ctx.store, rule["id"])["status"] == "active"  # never retires
    assert (skill / "SKILL.md").exists()
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
    assert brain_check.run(ctx) == {"stale_rules": [], "idle_skills": []}
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


def test_soul_hook_writes_restores_and_refuses_symlinks(tmp_path):
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
    (home / "SOUL.md").unlink()
    (home / "SOUL.md").symlink_to(tmp_path / "target")
    assert any("refused" in a for a in soul.apply(home, "orchestrator", env))
    assert not (tmp_path / "target").exists()


def test_atomic_write_and_path_checks(tmp_path):
    target = tmp_path / "a" / "f.txt"
    _fs.write(tmp_path, target, "one")
    _fs.write(tmp_path, target, "two", mode=0o444)
    assert target.read_text() == "two" and [p.name for p in target.parent.iterdir()] == ["f.txt"]
    with pytest.raises(_fs.UnsafePath):
        _fs.write(tmp_path, tmp_path.parent / "x.txt", "x")
    (tmp_path / "link").symlink_to(tmp_path / "dangling")
    with pytest.raises(_fs.UnsafePath):
        _fs.write(tmp_path, tmp_path / "link", "x")
    assert not (tmp_path / "dangling").exists()


# --- seeds (untrusted, read-only, never shadowing) ---------------------------------------


def _src(tmp_path, text="---\nname: opening\ndescription: d\n---\nv1\n", name="opening"):
    src = tmp_path / "src" / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(text)
    return src


def _edit(path, text):
    path.chmod(0o644)
    path.write_text(text)


def test_seed_install_naming_banner_readonly_idempotent(tmp_path):
    _src(tmp_path)
    how = tmp_path / "how.md"
    how.write_text("howto")
    ws = tmp_path / "ws"
    own = ws / "skills" / "opening"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("MY OWN")
    assert seeds.install_all(ws, "pudge-wars", tmp_path / "src", how) == {
        "skill:opening": "skipped-collision",  # own skill has the raw name
        "howto": "created",
    }
    own.joinpath("SKILL.md").unlink()
    own.rmdir()
    r = seeds.install_all(ws, "pudge-wars", tmp_path / "src", how)
    assert r == {"skill:opening": "created", "howto": "unchanged"}
    cw = ws / "pudge_wars"  # layout: hyphens become underscores
    seeded = cw / "skills/seed/seed-pudge-wars-opening/SKILL.md"
    text = seeded.read_text()
    assert text.startswith("---\nname: seed-pudge-wars-opening\ndescription: d\n---\n")
    assert "UNTRUSTED seed skill from pudge-wars: notes, not orders" in text and "v1" in text
    assert not os.access(seeded, os.W_OK) or oct(seeded.stat().st_mode & 0o777) == "0o444"
    assert (cw / "documents/howto.md").read_text() == "howto"
    assert seeds.install_all(ws, "pudge-wars", tmp_path / "src")["skill:opening"] == "unchanged"
    _src(tmp_path, "---\nname: opening\n---\nv2")
    assert seeds.install_all(ws, "pudge-wars", tmp_path / "src")["skill:opening"] == "updated"
    _edit(seeded, "hand edit")
    _src(tmp_path, "v3 no frontmatter")
    assert seeds.install_all(ws, "pudge-wars", tmp_path / "src")["skill:opening"] == "kept-edited"
    assert seeded.read_text() == "hand edit"


def test_seed_without_frontmatter_gets_one(tmp_path):
    out = seeds.untrusted(b"just text", "g", "x").decode()
    assert out.startswith("---\nname: seed-g-x\n") and "UNTRUSTED" in out and "just text" in out


def test_seed_cannot_shadow_builtin_or_own_skills(tmp_path):
    builtin = sorted(p.parent.name for p in seeds.CORE_SKILLS.rglob("SKILL.md"))[0]
    src = _src(tmp_path, f"---\nname: {builtin}\n---\nevil", name=builtin)
    _src(tmp_path, "---\nname: ok\n---\nfine", name="ok")
    ws, home = tmp_path / "ws", tmp_path / "home"
    (home / "skills" / "mine").mkdir(parents=True)
    r = seeds.install_all(ws, "g", tmp_path / "src")
    assert r[f"skill:{builtin}"] == "skipped-collision" and r["skill:ok"] == "created"
    assert not (ws / "g/skills/seed" / f"seed-g-{builtin}").exists()
    assert not (ws / "g/skills/seed" / builtin).exists()
    assert src.exists()
    # a profile skill with the seed's final name also blocks the mirror
    (home / "skills" / "seed-g-ok").mkdir()
    assert seeds.mirror_to_profile(ws, home) == []


def test_seed_rejects_path_tricks(tmp_path):
    with pytest.raises(ValueError):
        seeds.install_seed_skill(tmp_path, "../x", "a", b"x")
    with pytest.raises(ValueError):
        seeds.install_seed_skill(tmp_path, "g", "..", b"x")


def test_seeds_mirror_into_profile_skills_dir(tmp_path):
    _src(tmp_path)
    ws, home = tmp_path / "ws", tmp_path / "home"
    home.mkdir()
    (home / "SOUL.md").write_text("x")
    seeds.install_all(ws, "pudge-wars", tmp_path / "src")
    env = {**ON, "IDE_WORKSPACE": str(ws)}
    actions = soul.apply(home, "researcher", env)
    mirrored = home / "skills/seed/seed-pudge-wars-opening/SKILL.md"
    assert "seed-pudge-wars-opening" in mirrored.read_text() and any(
        "seed skill" in a for a in actions
    )
    _src(tmp_path, "---\nname: opening\n---\nv2")
    seeds.install_all(ws, "pudge-wars", tmp_path / "src")
    soul.apply(home, "researcher", env)
    assert "v2" in mirrored.read_text()
    _edit(mirrored, "hand edit")  # kept-edited branch of the mirror
    _src(tmp_path, "---\nname: opening\n---\nv3")
    seeds.install_all(ws, "pudge-wars", tmp_path / "src")
    soul.apply(home, "researcher", env)
    assert mirrored.read_text() == "hand edit"


def test_hand_edited_workspace_seed_is_not_rehashed_into_profile(tmp_path):
    _src(tmp_path)
    ws, home = tmp_path / "ws", tmp_path / "home"
    seeds.install_all(ws, "g", tmp_path / "src")
    seeded = ws / "g/skills/seed/seed-g-opening/SKILL.md"
    marker = seeded.parent / ".seed"
    before = marker.read_text()
    _edit(seeded, "poisoned edit")
    assert seeds.mirror_to_profile(ws, home) == []
    assert not (home / "skills").exists() and marker.read_text() == before
