import datetime as dt
from pathlib import Path

import pytest

from research_core.brain import install as bb
from research_core.brain import seeds as si
from research_core.brain import tidy as brain_tidy
from research_core.prompts import build

TODAY = dt.date(2026, 11, 12)


ROOT = Path(__file__).resolve().parents[2]
ON = {"IDE_BRAIN": "1"}


def test_flag_off_does_nothing(tmp_path):
    home, ws = tmp_path / "home", tmp_path / "ws"
    home.mkdir()
    (home / "SOUL.md").write_text("installed soul\n")
    for env in ({}, {"IDE_BRAIN": "0"}, {"IDE_BRAIN": ""}):
        env = {**env, "IDE_WORKSPACE": str(ws)}
        assert bb.apply(home, "orchestrator", env) == []
    assert not ws.exists() and (home / "SOUL.md").read_text() == "installed soul\n"


def test_flag_off_soul_is_byte_identical():
    for role in build.ROLES:
        committed = (ROOT / role / "SOUL.md").read_text(encoding="utf-8")
        assert build.render(role) == build.render(role, brain=False)
        assert committed == build.render(role)  # the committed SOULs are the flag-off render
        assert build.render(role, brain=True).startswith(committed.rstrip("\n"))
        assert build.render(role, brain=True) != committed


def test_brain_soul_within_budget_and_safe():
    for role in build.ROLES:
        assert build.words(build.render(role, brain=True)) <= build.BUDGETS[role] + 800
    text = build.brain_section("orchestrator")
    assert "skill_manage" in text and "unverified" in text and "quarantine" in text
    assert "Do not write to `brain/`" in build.brain_section("researcher")


def test_apply_installs_and_writes_soul(tmp_path):
    home, ws = tmp_path / "home", tmp_path / "ws"
    home.mkdir()
    (home / "SOUL.md").write_text("installed soul\n")
    env = {**ON, "IDE_WORKSPACE": str(ws)}
    (ws / "brain").mkdir(parents=True)
    (ws / "brain" / "rules.md").write_text("MINE")
    actions = bb.apply(home, "orchestrator", env)
    assert (ws / "brain/rules.md").read_text() == "MINE"  # never clobbered
    for f in (
        "brain/failed.md",
        "brain/claims.md",
        "brain/hypotheses.md",
        "brain/wins.md",
        "brain/log.md",
        "brain/index.md",
        "research/README.md",
        "playbooks/README.md",
        "skills/README.md",
        "brain/journal/README.md",
    ):
        assert (ws / f).exists(), f
    assert (home / "SOUL.md").read_text() == build.render("orchestrator", brain=True)
    assert any("SOUL.md" in a for a in actions)
    assert bb.apply(home, "orchestrator", env) == []  # idempotent
    bb.apply(home, "researcher", env)
    assert (home / "SOUL.md").read_text() == build.render("researcher", brain=True)


def test_seed_install_idempotent_and_safe(tmp_path):
    src = tmp_path / "src" / "opening"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: opening\n---\nv1\n")
    how = tmp_path / "how.md"
    how.write_text("howto")
    ws = tmp_path / "ws"
    own = ws / "skills" / "opening"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("MY OWN")
    r = si.install_all(ws, "pudge-wars", tmp_path / "src", how)
    assert r == {"skill:opening": "created", "howto": "created"}
    cw = ws / "pudge_wars"  # layout: hyphens become underscores
    assert (own / "SKILL.md").read_text() == "MY OWN"
    assert (cw / "skills/seed/opening/SKILL.md").exists()
    assert (cw / "documents/howto.md").read_text() == "howto"
    assert si.install_all(ws, "pudge-wars", tmp_path / "src", how) == {
        "skill:opening": "unchanged",
        "howto": "unchanged",
    }
    (src / "SKILL.md").write_text("v2")
    assert si.install_all(ws, "pudge-wars", tmp_path / "src")["skill:opening"] == "updated"
    seeded = cw / "skills/seed/opening/SKILL.md"
    seeded.write_text("hand edit")
    (src / "SKILL.md").write_text("v3")
    assert si.install_all(ws, "pudge-wars", tmp_path / "src")["skill:opening"] == "kept-edited"
    assert seeded.read_text() == "hand edit"


def test_seed_rejects_path_tricks(tmp_path):
    with pytest.raises(ValueError):
        si.install_seed_skill(tmp_path, "../x", "a", b"x")
    with pytest.raises(ValueError):
        si.install_seed_skill(tmp_path, "g", "..", b"x")


def _ws(tmp_path):
    bb.install(tmp_path)
    return tmp_path


def test_tidy_caps_decay_and_report(tmp_path):
    ws = _ws(tmp_path)
    b = ws / "brain"
    fail = (b / "failed.md").read_text()
    for i in range(30):
        fail += (
            f"\n## 2026-10-{i + 1:02d} entry {i}\nWhat happened: "
            + "x" * 400
            + "\nRule: r\nEvidence: e\n"
        )
    (b / "failed.md").write_text(fail)
    (b / "claims.md").write_text(
        "# Claims\n"
        "- old high | confidence: HIGH (said) | evidence: told | since: 2026-01-01\n"
        "- old low | confidence: LOW (guess) | evidence: x | since: 2026-01-01\n"
        "- fresh | confidence: MED (twice) | evidence: x | since: 2026-11-01\n"
    )
    (b / "journal" / "2026-09.md").write_text(
        "- 2026-09-01 stale — why: a (seen: 1) [provisional]\n"
        "- 2026-09-02 twice — why: b (seen: 2) [provisional]\n"
    )
    (ws / "research/notes").mkdir(exist_ok=True)
    (ws / "research/notes/old.md").write_text(
        "status: unverified\nsource: u   fetched: 2026-08-01\n"
    )
    out = brain_tidy.tidy(ws, TODAY)
    assert len((b / "failed.md").read_bytes()) <= 8192
    assert (b / "archive" / "failed.md").exists() and "[archived]" in (b / "failed.md").read_text()
    c = (b / "claims.md").read_text()
    assert "old high | confidence: MED" in c and "decayed: 2026-11-12" in c
    assert "old low" not in c and "old low" in (b / "archive" / "claims.md").read_text()
    assert "fresh | confidence: MED" in c
    j = (b / "journal" / "2026-09.md").read_text()
    assert "stale" not in j and "twice" in j and "twice" in out
    assert not (ws / "research/notes/old.md").exists()
    assert (ws / "tidy-report.md").exists() and "tidy" in (b / "log.md").read_text()
    # second run is a no-op on content
    before = (b / "failed.md").read_text()
    brain_tidy.tidy(ws, TODAY)
    assert (b / "failed.md").read_text() == before


def test_tidy_stop_file(tmp_path):
    ws = _ws(tmp_path)
    (ws / "STOP").write_text("")
    assert "STOP" in brain_tidy.tidy(ws, TODAY)
    assert not (ws / "tidy-report.md").exists()


def test_tidy_rule_cap_moves_oldest(tmp_path):
    ws = _ws(tmp_path)
    rules = (ws / "brain/rules.md").read_text() + "\n".join(
        f"- rule {i}. Why: w. Applies when: a. (added 2026-11-0{1 + i % 9}, source: told)"
        for i in range(45)
    )
    (ws / "brain/rules.md").write_text(rules)
    brain_tidy.tidy(ws, TODAY)
    assert (ws / "brain/rules.md").read_text().count("\n- rule") == 40
    assert "rule 44" in (ws / "brain/archive/rules.md").read_text()
