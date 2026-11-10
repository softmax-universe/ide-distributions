"""Open chats move to a release's tools and prompts once, and only when those changed.

``core/tests/hermes/test_frozen_sessions_real.py`` runs the same story on a real gateway. Here a
small ``state.db`` with Hermes's column names stands in, so the decisions run in the default suite.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_core.frozen_sessions import SURFACE_KEY, agent_surface, refresh_frozen_sessions

REPO = Path(__file__).resolve().parents[2]
CORE_SRC = REPO / "core" / "src"
OLD_PIN = "old-pin-hash"


@pytest.fixture
def release(tmp_path) -> Path:
    """A release's distribution folder: only the files the hash reads, plus an unrelated one."""
    package = tmp_path / "release" / "core" / "src" / "research_core"
    (package / "plugin").mkdir(parents=True)
    for module in ("briefing.py", "session_context.py", "register.py"):
        (package / "plugin" / module).write_text("# section\n")
    (package / "watcher.py").write_text("# unrelated\n")
    skill = package / "skills" / "common" / "repair"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: repair\n---\n")
    source = tmp_path / "release" / "researcher"
    source.mkdir()
    return source


@pytest.fixture
def profile(tmp_path) -> Path:
    """A profile whose one session holds a tool pin."""
    home = tmp_path / "profile"
    home.mkdir()
    (home / "SOUL.md").write_text("You are a researcher.\n")
    connection = sqlite3.connect(home / "state.db")
    connection.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, tool_names TEXT);
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    connection.execute("INSERT INTO sessions VALUES ('chat', ?)", (OLD_PIN,))
    connection.commit()
    connection.close()
    return home


class PromptRefresh:
    def __init__(self, failure: Exception | None = None) -> None:
        self.calls = 0
        self.failure = failure

    def __call__(self) -> None:
        self.calls += 1
        if self.failure:
            raise self.failure


def pin(profile: Path) -> str | None:
    connection = sqlite3.connect(profile / "state.db")
    try:
        return connection.execute("SELECT tool_names FROM sessions").fetchone()[0]
    finally:
        connection.close()


def recorded_surface(profile: Path) -> str | None:
    connection = sqlite3.connect(profile / "state.db")
    try:
        row = connection.execute(
            "SELECT value FROM state_meta WHERE key = ?", (SURFACE_KEY,)
        ).fetchone()
    finally:
        connection.close()
    return row[0] if row else None


def set_pin(profile: Path, value: str) -> None:
    connection = sqlite3.connect(profile / "state.db")
    with connection:
        connection.execute("UPDATE sessions SET tool_names = ?", (value,))
    connection.close()


def test_a_new_release_refreshes_once_and_later_boots_keep_the_pin(release, profile):
    refresh = PromptRefresh()
    assert refresh_frozen_sessions(profile, release, refresh) == [
        "open sessions: refreshed tools and system prompts"
    ]
    assert pin(profile) is None
    assert refresh.calls == 1
    assert recorded_surface(profile) == agent_surface(release, profile)

    # The next turn pins the new tools; a boot of the same release must leave that pin alone.
    set_pin(profile, "new-pin-hash")
    assert refresh_frozen_sessions(profile, release, refresh) == []
    assert pin(profile) == "new-pin-hash"
    assert refresh.calls == 1


def test_a_commit_that_changes_nothing_frozen_keeps_every_pin(release, profile):
    refresh = PromptRefresh()
    refresh_frozen_sessions(profile, release, refresh)
    set_pin(profile, "new-pin-hash")

    watcher = release.parent / "core" / "src" / "research_core" / "watcher.py"
    watcher.write_text("# changed\n")

    assert refresh_frozen_sessions(profile, release, refresh) == []
    assert pin(profile) == "new-pin-hash"
    assert refresh.calls == 1


@pytest.mark.parametrize(
    "frozen_file",
    [
        "core/src/research_core/plugin/briefing.py",
        "core/src/research_core/plugin/session_context.py",
        "core/src/research_core/plugin/register.py",
    ],
)
def test_a_change_to_a_frozen_file_refreshes(release, profile, frozen_file):
    refresh = PromptRefresh()
    refresh_frozen_sessions(profile, release, refresh)
    set_pin(profile, "new-pin-hash")

    path = release.parent / frozen_file
    path.write_text(path.read_text() + "changed\n")

    assert refresh_frozen_sessions(profile, release, refresh)
    assert pin(profile) is None
    assert refresh.calls == 2


def test_the_installed_soul_is_what_counts(release, profile):
    """Hermes reads the profile's copy: a checkout ahead of the profile records nothing new."""
    refresh = PromptRefresh()
    refresh_frozen_sessions(profile, release, refresh)
    set_pin(profile, "new-pin-hash")

    (release / "SOUL.md").write_text("A newer checkout, not installed.\n")
    assert refresh_frozen_sessions(profile, release, refresh) == []
    assert pin(profile) == "new-pin-hash"

    (profile / "SOUL.md").write_text("A newer checkout, installed.\n")
    assert refresh_frozen_sessions(profile, release, refresh)
    assert pin(profile) is None


def test_only_a_skills_frontmatter_is_frozen(release, profile):
    """The prompt's skill index lists the frontmatter; the model reads the body when it runs."""
    refresh = PromptRefresh()
    refresh_frozen_sessions(profile, release, refresh)
    set_pin(profile, "new-pin-hash")
    skill = release.parent / "core/src/research_core/skills/common/repair/SKILL.md"

    skill.write_text(skill.read_text() + "A new step in the body.\n")
    assert refresh_frozen_sessions(profile, release, refresh) == []
    assert pin(profile) == "new-pin-hash"

    skill.write_text("---\nname: repair\ndescription: Fix things.\n---\n")
    assert refresh_frozen_sessions(profile, release, refresh)
    assert pin(profile) is None


def test_a_tool_schema_change_refreshes(release, profile, monkeypatch):
    class Spec:
        def __init__(self, parameters: dict) -> None:
            self.parameters = parameters

        def schema(self, role: str) -> dict:
            return {"name": "task", "parameters": self.parameters}

    monkeypatch.setattr(
        "research_core.frozen_sessions.tools_for", lambda role: [Spec({"type": "object"})]
    )
    refresh = PromptRefresh()
    refresh_frozen_sessions(profile, release, refresh)
    set_pin(profile, "new-pin-hash")

    monkeypatch.setattr(
        "research_core.frozen_sessions.tools_for",
        lambda role: [Spec({"type": "object", "properties": {"operation_id": {}}})],
    )
    assert refresh_frozen_sessions(profile, release, refresh)
    assert pin(profile) is None


def test_the_hash_is_the_same_in_every_process(tmp_path):
    """Boots are separate processes: a hash that moved between them would refresh on every boot.

    Nothing about the machine or the workspace may enter it either: the seeds come with a different
    directory and platform each.
    """
    script = (
        "from pathlib import Path\n"
        "from research_core.frozen_sessions import agent_surface\n"
        f"source = Path({str(REPO / 'orchestrator')!r})\n"
        "print(agent_surface(source, source))\n"
    )
    hashes = {
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=tmp_path if seed == "2" else REPO,
            env={
                **os.environ,
                "PYTHONPATH": str(CORE_SRC),
                "PYTHONHASHSEED": seed,
                "HOME": f"/home/boot-{seed}",
                "HERMES_HOME": f"/profiles/boot-{seed}",
                "IDE_WORKSPACE": f"/workspace/boot-{seed}",
                "IDE_PLATFORM": "fake" if seed == "2" else "real",
                "IDE_DEFAULT_COWORLD": f"cow_{seed}",
                "IDE_ROLE": "researcher" if seed == "3" else "orchestrator",
            },
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("1", "2", "3")
    }
    assert len(hashes) == 1


def test_the_real_release_hash_differs_by_role():
    orchestrator, researcher = REPO / "orchestrator", REPO / "researcher"
    assert agent_surface(orchestrator, orchestrator) != agent_surface(researcher, researcher)


def test_a_failed_prompt_request_leaves_the_session_as_it_was_and_retries(release, profile):
    failing = PromptRefresh(RuntimeError("hermes exited 1"))
    actions = refresh_frozen_sessions(profile, release, failing)

    assert "the next boot tries again" in actions[0]
    assert "hermes exited 1" in actions[0]
    assert pin(profile) == OLD_PIN
    assert recorded_surface(profile) is None

    refresh = PromptRefresh()
    assert refresh_frozen_sessions(profile, release, refresh) == [
        "open sessions: refreshed tools and system prompts"
    ]
    assert pin(profile) is None


def test_a_failure_after_the_prompt_request_records_nothing_and_retries(release, tmp_path):
    home = tmp_path / "no-sessions-table"
    home.mkdir()
    (home / "SOUL.md").write_text("You are a researcher.\n")
    connection = sqlite3.connect(home / "state.db")
    connection.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.close()
    refresh = PromptRefresh()

    assert "the next boot tries again" in refresh_frozen_sessions(home, release, refresh)[0]
    assert refresh.calls == 1
    assert recorded_surface(home) is None

    assert "the next boot tries again" in refresh_frozen_sessions(home, release, refresh)[0]
    assert refresh.calls == 2


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["hermes"], 120),
        ValueError("a tool module's schema is broken"),
    ],
)
def test_no_failure_of_the_refresh_stops_the_boot(release, profile, failure):
    actions = refresh_frozen_sessions(profile, release, PromptRefresh(failure))

    assert "the next boot tries again" in actions[0]
    assert pin(profile) == OLD_PIN
    assert recorded_surface(profile) is None


def test_a_database_without_the_expected_tables_does_not_stop_the_boot(release, tmp_path):
    home = tmp_path / "other-schema"
    home.mkdir()
    (home / "SOUL.md").write_text("You are a researcher.\n")
    sqlite3.connect(home / "state.db").close()
    refresh = PromptRefresh()

    actions = refresh_frozen_sessions(home, release, refresh)

    assert "the next boot tries again" in actions[0]
    assert refresh.calls == 0


def test_a_profile_with_no_database_stays_without_one(release, tmp_path):
    home = tmp_path / "fresh"
    home.mkdir()
    refresh = PromptRefresh()

    assert refresh_frozen_sessions(home, release, refresh) == []
    assert not (home / "state.db").exists()
    assert refresh.calls == 0


def test_check_reports_and_writes_nothing(release, profile):
    refresh = PromptRefresh()

    actions = refresh_frozen_sessions(profile, release, refresh, check=True)

    assert actions == ["open sessions: would refresh tools and system prompts"]
    assert pin(profile) == OLD_PIN
    assert recorded_surface(profile) is None
    assert refresh.calls == 0


def test_prepare_requests_refresh_from_the_native_session_store(tmp_path, monkeypatch):
    from research_core import prepare as prepare_module
    from tests.test_prepare import FakeHermes

    checkout = tmp_path / "checkout"
    shutil.copytree(REPO / "orchestrator", checkout / "orchestrator")
    shutil.copytree(CORE_SRC, checkout / "core" / "src")
    shutil.copy(REPO / "uv.lock", checkout / "uv.lock")
    home = tmp_path / "profile"
    home.mkdir()
    (home / "SOUL.md").write_text("You are an Orchestrator.\n")
    connection = sqlite3.connect(home / "state.db")
    connection.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, tool_names TEXT);
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO sessions VALUES ('chat', 'old-pin-hash');
        """
    )
    connection.commit()
    connection.close()

    calls = []

    @contextmanager
    def session_db(path):
        assert path == home / "state.db"
        yield SimpleNamespace(request_prompt_refresh=lambda: calls.append("refresh"))
        calls.append("close")

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=session_db))
    fake = FakeHermes()
    env = {
        "HERMES_HOME": str(home),
        "HERMES_REAL_HOME": str(tmp_path / "host"),
        "IDE_WORKSPACE": str(tmp_path / "workspace"),
        "PATH": os.environ.get("PATH", ""),
    }
    cli = prepare_module.HermesCli("hermes", home, env, fake.run)
    summary = prepare_module.prepare(checkout / "orchestrator", cli, env=env, run=fake.run)

    assert "open sessions: refreshed tools and system prompts" in summary["actions"]
    assert pin(home) is None
    assert calls == ["refresh", "close"]
