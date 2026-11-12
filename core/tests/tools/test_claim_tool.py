"""The ``claim`` tool: every action through run_tool, and the evidence inspectors."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

import research_core.tools.claim  # noqa: F401  (registers the tool)
from research_core import gitops
from research_core.store import pins
from research_core.store.actors import Actor

FILE_LINES = [f"line {n}" for n in range(1, 11)]
SESSION = "20261028_210405_0badcafe"
MESSAGE_CONTENT = "The scouts opened on the left flank and won 12 of 20 episodes."

# Hermes v0.22.6 state.db, the columns the transcript inspector reads (reads/4.md §4).
HERMES_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    timestamp REAL NOT NULL
);
"""


@pytest.fixture
def workspace_env(tmp_path):
    """The conftest workspace plus a HERMES_HOME, so the transcript inspector finds state.db."""
    return {
        "IDE_WORKSPACE": str(tmp_path / "ws"),
        "IDE_PLATFORM": "fake",
        "HERMES_HOME": str(tmp_path / "hermes"),
    }


@pytest.fixture
def code_repo(ctx_factory):
    """The active Coworld's code repository with one committed file; returns (repo, sha)."""
    repo = gitops.init_repo(ctx_factory().coworld().code)
    (repo / "file.txt").write_text("".join(f"{line}\n" for line in FILE_LINES))
    sha = gitops.commit_all(repo, "Add file.txt")
    return repo, sha


@pytest.fixture
def state_db(tmp_path):
    """A Hermes state.db with one session and one message; returns the message id."""
    home = tmp_path / "hermes"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.executescript(HERMES_SCHEMA)
    conn.execute("INSERT INTO sessions VALUES (?, 'api', 1761690245.0)", (SESSION,))
    cursor = conn.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (SESSION, "assistant", MESSAGE_CONTENT, 1761690300.0),
    )
    conn.commit()
    conn.close()
    return cursor.lastrowid


def code_ref(sha, start=2, end=4):
    return f"file.txt:{start}-{end}@{sha}"


def write_claim(call, role="orchestrator", **overrides):
    args = {
        "action": "write",
        "summary": "Opening with scouts wins more often.",
        "body": "12 of 20 episodes on 2026-10-28. Changes our mind: a losing streak.",
        "tags": ["opening", "scouts"],
        "evidence": [{"type": "doc", "ref": "README.md#setup", "stance": "supports", "note": "n"}],
    }
    args.update(overrides)
    return call("claim", args, role=role)


def test_write_read_search_and_retire_through_the_tool(call):
    created = write_claim(call)
    assert created["ok"] is True
    record = created["record"]
    assert record["id"] == "C-1" and record["status"] == "active"
    assert record["counts"] == {"supports": 1, "contradicts": 0, "qualifies": 0}
    assert record["freshness"] is not None and record["contested"] is False
    assert record["new_tags"] == {"opening": [], "scouts": []}

    read = call("claim", {"action": "read", "claim": 1})
    assert read["record"]["summary"] == "Opening with scouts wins more often."

    found = call("claim", {"action": "search", "query": "scouts"})
    assert [c["id"] for c in found["records"]] == ["C-1"]
    assert "snippet" in found["records"][0]
    assert (
        call("claim", {"action": "search", "query": "scouts", "tags": ["cache"]})["records"] == []
    )

    replacement = write_claim(call, summary="Opening with scouts wins 60% of episodes.")
    retired = call(
        "claim",
        {"action": "retire", "claim": "C-1", "status": "superseded", "superseded_by": "C-2"},
    )
    assert retired["record"]["status"] == "superseded"
    assert retired["record"]["superseded_by"] == replacement["record"]["id"]
    active_only = call("claim", {"action": "search", "query": "scouts", "include_retired": False})
    assert [c["id"] for c in active_only["records"]] == ["C-2"]
    everything = call("claim", {"action": "search", "query": "scouts"})
    assert [c["retired"] for c in everything["records"]] == [False, True]
    # No query lists claims, so tags alone find a Goal's or a topic's.
    listed = call("claim", {"action": "search", "tags": ["opening"]})
    assert listed["ok"] is True
    assert [c["id"] for c in listed["records"]] == ["C-2", "C-1"]
    assert call("claim", {"action": "search"})["ok"] is True


def test_add_evidence_and_refute(call):
    write_claim(call)
    added = call(
        "claim",
        {
            "action": "add_evidence",
            "claim": "C-1",
            "type": "doc",
            "ref": "README.md#limits",
            "stance": "qualifies",
        },
    )
    assert added["record"]["counts"]["qualifies"] == 1
    assert added["record"]["evidence"][1]["note"] == ""

    refuted = call(
        "claim",
        {
            "action": "retire",
            "claim": "C-1",
            "status": "refuted",
            "type": "doc",
            "ref": "README.md#results",
            "note": "20 losses",
        },
    )
    assert refuted["record"]["status"] == "refuted"
    assert refuted["record"]["evidence"][-1]["stance"] == "contradicts"


def test_researcher_may_use_every_action(call, code_repo):
    _, sha = code_repo
    created = write_claim(
        call,
        role="researcher",
        evidence=[{"type": "code", "ref": code_ref(sha), "stance": "supports", "note": "n"}],
    )
    assert created["ok"] is True
    write_claim(call, role="researcher", summary="A second claim.")
    for args in (
        {"action": "read", "claim": "C-1"},
        {"action": "search", "query": "claim"},
        {
            "action": "add_evidence",
            "claim": "C-1",
            "type": "claim",
            "ref": "C-2",
            "stance": "supports",
        },
        {"action": "inspect_evidence", "claim": "C-1"},
        {"action": "retire", "claim": "C-1", "status": "superseded", "superseded_by": "C-2"},
    ):
        result = call("claim", args, role="researcher")
        assert result["ok"] is True, (args, result)


def test_refusals_report_every_problem_and_no_store_path(call):
    # Argument validation first: every missing field at once.
    result = call("claim", {"action": "write", "summary": "x"})
    assert result["ok"] is False
    assert {
        "body is required for write",
        "tags is required for write",
        "evidence is required for write",
    } <= set(result["problems"])
    # Then the store's rules, also all at once.
    result = write_claim(call, summary="word " * 41, evidence=[])
    assert result["ok"] is False
    problems = " ".join(result["problems"])
    assert "41" in problems and "evidence" in problems
    assert ".state/" not in str(result)
    bad_ref = call("claim", {"action": "read", "claim": "G-1"})
    assert bad_ref["ok"] is False and "claim reference" in bad_ref["error"]
    missing = call("claim", {"action": "read", "claim": "C-9"})
    assert missing["ok"] is False and "C-9" in missing["error"]


# --- inspectors -----------------------------------------------------------------------------


def test_code_evidence_reports_the_lines_and_whether_they_are_current(call, code_repo):
    repo, sha = code_repo
    write_claim(
        call,
        evidence=[{"type": "code", "ref": code_ref(sha), "stance": "supports", "note": "n"}],
    )
    first = call("claim", {"action": "inspect_evidence", "claim": "C-1"})
    assert first["ok"] is True and first["claim"] == "C-1"
    [inspection] = first["inspections"]
    assert inspection["type"] == "code" and inspection["ref"] == code_ref(sha)
    assert inspection["evidence_id"] == 1 and inspection["stance"] == "supports"
    assert inspection["lines"] == ["line 2", "line 3", "line 4"]
    assert inspection["current"] is True
    assert "problem" not in inspection
    assert "unchanged" in inspection["summary"]

    lines = list(FILE_LINES)
    lines[2] = "line 3 changed"
    (repo / "file.txt").write_text("".join(f"{line}\n" for line in lines))
    gitops.commit_all(repo, "Change line 3")

    second = call("claim", {"action": "inspect_evidence", "claim": "C-1", "evidence_id": 1})
    [inspection] = second["inspections"]
    assert inspection["current"] is False
    assert inspection["lines"] == ["line 2", "line 3", "line 4"], "the lines at the cited sha"
    assert "changed" in inspection["summary"]


def test_code_evidence_without_a_repository_is_a_problem_not_a_crash(call):
    result = call(
        "claim",
        {"action": "inspect_evidence", "type": "code", "ref": f"file.txt:1-2@{'a' * 40}"},
    )
    assert result["ok"] is True
    [inspection] = result["inspections"]
    assert inspection["type"] == "code" and "summary" in inspection
    assert "repository" in inspection["problem"]
    assert ".state/" not in str(result)


def test_transcript_evidence_reads_hermes_state_db_and_checks_the_hash(call, state_db):
    digest = hashlib.sha256(MESSAGE_CONTENT.encode()).hexdigest()
    ref = f"session:{SESSION}/{state_db}#{digest}"
    result = call("claim", {"action": "inspect_evidence", "type": "transcript", "ref": ref})
    [inspection] = result["inspections"]
    assert inspection["role"] == "assistant"
    assert inspection["content"] == MESSAGE_CONTENT
    assert inspection["hash_matches"] is True
    assert "problem" not in inspection

    stale = f"session:{SESSION}/{state_db}#{'0' * 64}"
    [inspection] = call(
        "claim", {"action": "inspect_evidence", "type": "transcript", "ref": stale}
    )["inspections"]
    assert inspection["hash_matches"] is False
    assert "differs" in inspection["summary"]

    missing = f"session:{SESSION}/999#{digest}"
    [inspection] = call(
        "claim", {"action": "inspect_evidence", "type": "transcript", "ref": missing}
    )["inspections"]
    assert "not found" in inspection["problem"]


def test_transcript_without_a_state_db_is_a_problem(call, workspace_env):
    ref = f"session:{SESSION}/1#{'a' * 64}"
    [inspection] = call("claim", {"action": "inspect_evidence", "type": "transcript", "ref": ref})[
        "inspections"
    ]
    assert "problem" in inspection and inspection["type"] == "transcript"


def test_record_evidence_shows_the_cited_record(call):
    write_claim(call)
    write_claim(
        call,
        summary="Builds on the first.",
        evidence=[{"type": "claim", "ref": "C-1", "stance": "supports", "note": "n"}],
    )
    result = call(
        "claim", {"action": "inspect_evidence", "claim": "C-2", "evidence_id": "evidence-2"}
    )
    [inspection] = result["inspections"]
    assert inspection["type"] == "claim" and inspection["id"] == "C-1"
    assert inspection["summary"] == "Opening with scouts wins more often."
    assert inspection["status"] == "active"
    gone = call("claim", {"action": "inspect_evidence", "type": "experiment", "ref": "X-7"})
    assert "X-7 does not exist" in gone["inspections"][0]["problem"]


def test_evidence_selection_by_id_position_and_bad_values(call):
    write_claim(call)  # evidence id 1
    write_claim(
        call,
        summary="Two links.",
        evidence=[
            {"type": "doc", "ref": "README.md#a", "stance": "supports", "note": ""},
            {"type": "doc", "ref": "README.md#b", "stance": "qualifies", "note": ""},
        ],
    )  # evidence ids 2 and 3
    by_id = call("claim", {"action": "inspect_evidence", "claim": "C-2", "evidence_id": 3})
    assert by_id["inspections"][0]["ref"] == "README.md#b"
    by_position = call("claim", {"action": "inspect_evidence", "claim": "C-2", "evidence_id": "1"})
    assert by_position["inspections"][0]["ref"] == "README.md#a"
    both = call("claim", {"action": "inspect_evidence", "claim": "C-2"})
    assert [i["evidence_id"] for i in both["inspections"]] == [2, 3]
    bad = call("claim", {"action": "inspect_evidence", "claim": "C-2", "evidence_id": 9})
    assert bad["ok"] is False and "2, 3" in bad["error"]
    neither = call("claim", {"action": "inspect_evidence"})
    assert neither["ok"] is False
    half = call("claim", {"action": "inspect_evidence", "type": "doc"})
    assert half["ok"] is False and "ref" in half["error"]
    unknown = call("claim", {"action": "inspect_evidence", "type": "screenshot", "ref": "x"})
    assert "not registered" in unknown["inspections"][0]["problem"]


def test_doc_measurement_replay_and_annotation_inspectors(call, ctx_factory):
    ctx = ctx_factory()
    harness = Actor.harness()
    (ctx.coworld().documents / "measure-1.txt").write_text("mean 0.61\n")
    pins.add(
        ctx.store,
        harness,
        kind="replay",
        ref="episode:ereq_abc",
        download={"path": "replays/ereq_abc.json", "size": 10},
    )
    sha = "0123abcd"

    def inspect(type_name, ref):
        result = call("claim", {"action": "inspect_evidence", "type": type_name, "ref": ref})
        assert result["ok"] is True, result
        [inspection] = result["inspections"]
        assert inspection["type"] == type_name and inspection["ref"] == ref
        assert inspection["summary"]
        return inspection

    quoted = inspect("doc", "https://example.org/page (retrieved 2026-10-28): the quoted passage")
    assert quoted["quote"] == "the quoted passage" and quoted["retrieved"] == "2026-10-28"
    section = inspect("doc", "README.md#setup")
    assert section["document"] == "README.md" and section["section"] == "setup"

    measured = inspect("measurement", f"make bench @{sha} -> documents/measure-1.txt")
    assert measured["command"] == "make bench" and measured["sha"] == sha
    assert measured["excerpt"] == "mean 0.61\n"
    absent = inspect("measurement", f"make bench @{sha} -> documents/never.txt")
    assert absent["excerpt"] is None and "problem" in absent

    replay = inspect("replay", "episode:ereq_abc/seat 2 xp:xreq_123")
    assert replay["episode"] == "ereq_abc" and replay["seat"] == "2"
    assert replay["xp_request"] == "xreq_123"
    assert replay["pin"]["path"] == "replays/ereq_abc.json"
    assert "(pinned)" in replay["summary"]
    unpinned = inspect("replay", "episode:ereq_zzz xp:xreq_123")
    assert unpinned["pin"] is None

    note = inspect("annotation", "annotation:ann_42")
    assert note["annotation_id"] == "ann_42"
