import pytest

from research_core.plugin import advisory, rules

FIRES = [
    ("terminal", {"command": "coworld submit best:v3 --league league_x"}, "raw-submit"),
    ("terminal", {"command": "cd code && coworld upload-policy policy.json"}, "raw-upload"),
    ("terminal", {"command": "softmax upload ./policy"}, "raw-upload"),
    ("terminal", {"command": "coworld xp-request create --coworld cow_x"}, "raw-xp"),
    ("terminal", {"command": "softmax login --server https://softmax.com/api"}, "identity-change"),
    ("terminal", {"command": "coworld player use ply_123"}, "identity-change"),
    ("terminal", {"command": "git push --force origin main"}, "force-push"),
    ("terminal", {"command": "git push -f origin task/3"}, "force-push"),
    ("terminal", {"command": "git push origin +main"}, "force-push"),
    ("terminal", {"command": "sqlite3 .state/store.db 'delete from tasks'"}, "store-access"),
    ("read_file", {"path": "/ws/cow_x/.state/store.db"}, "store-access"),
    ("execute_code", {"code": "import sqlite3; sqlite3.connect('store.db')"}, "store-access"),
]

QUIET = [
    ("terminal", {"command": "git push origin task/3"}),
    ("terminal", {"command": "coworld submit --help | head"}),  # by design: still a note
    ("terminal", {"command": "coworld leagues --json"}),
    ("terminal", {"command": "ls -la"}),
    ("read_file", {"path": "/ws/cow_x/code/policy.json"}),
    ("goal", {"action": "read"}),
]


@pytest.mark.parametrize("tool,args,rule", FIRES)
def test_rules_fire(tool, args, rule):
    assert [r.name for r in rules.matches(tool, args)] == [rule]


@pytest.mark.parametrize("tool,args", [q for q in QUIET if "help" not in q[1].get("command", "")])
def test_rules_stay_quiet(tool, args):
    assert rules.matches(tool, args) == []


def test_a_note_names_a_tool_the_role_has():
    submit = {"command": "coworld submit x"}
    assert "task_propose_submission" in rules.notes_for("terminal", submit, {}, "researcher")[0]
    assert "submit tool" in rules.notes_for("terminal", submit, {}, "orchestrator")[0]
    push = {"command": "git push --force origin main"}
    assert "workspace.change_complete" in rules.notes_for("terminal", push, {}, "orchestrator")[0]
    assert "task_complete" in rules.notes_for("terminal", push, {}, "researcher")[0]


def test_notes_point_at_repair_task():
    notes = rules.notes_for("terminal", {"command": "coworld submit x"}, {"upload": "T-9"})
    assert notes[0].startswith("[note]")
    assert "repair is T-9" in notes[0]
    assert rules.notes_for("terminal", {"command": "coworld submit x"}, {}) != []


def test_transform_appends_notes_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    hooks = advisory.make_hooks("researcher")
    out = hooks["transform_tool_result"](
        tool_name="terminal", args={"command": "coworld submit x"}, result="Submission: sub_1"
    )
    assert out.startswith("Submission: sub_1\n\n[note]")
    assert (
        hooks["transform_tool_result"](tool_name="terminal", args={"command": "ls"}, result="x")
        is None
    )
    assert hooks["transform_tool_result"](tool_name="terminal", args=None, result=None) is None
    assert (
        hooks["transform_tool_result"](
            tool_name="terminal", args={"command": "coworld submit"}, result=42
        )
        is None
    )


def test_failure_of():
    assert advisory.failure_of('{"ok": false, "error": "boom", "context": {"exit_code": 2}}') == {
        "error": "boom",
        "exit_code": 2,
    }
    assert advisory.failure_of('{"ok": true, "record": {}}') is None
    assert advisory.failure_of("stdout here\nExit code: 1")["exit_code"] == 1
    assert advisory.failure_of("Exit code: 0") is None
    assert advisory.failure_of(None) is None


def test_post_tool_call_never_raises_without_a_store(tmp_path, monkeypatch):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path))
    hooks = advisory.make_hooks("researcher")
    assert (
        hooks["post_tool_call"](
            tool_name="terminal", args={"command": "x"}, result="Exit code: 1", session_id="s1"
        )
        is None
    )
