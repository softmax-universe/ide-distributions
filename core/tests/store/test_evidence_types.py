"""The evidence type registry: reference formats, validators, resolution and inspectors (B.7)."""

from __future__ import annotations

import pytest

from research_core.store import evidence_types
from research_core.store.evidence_types import EVIDENCE_TYPES, InspectEnv

SHA = "0123abcd"
SHA256 = "a" * 64

VALID_REFS = [
    ("claim", "C-3"),
    ("claim", "C-12#evidence-3"),
    ("experiment", "X-5"),
    ("experiment", "X-5#methods"),
    ("version", "V-7"),
    ("code", f"policy/agent.py:10-20@{SHA}"),
    ("code", f"policy/agent.py:1-1@{'f' * 40}"),
    ("doc", "README.md#setup"),
    ("doc", "https://softmax.com/docs/leagues#rounds"),
    ("doc", "https://example.org/page (retrieved 2026-10-28): the quoted passage"),
    ("transcript", f"session:api_1761690245_0badcafe/msg-7#{SHA256}"),
    ("measurement", f"python -m tools.measure --seed 1 @{SHA} -> documents/measure-1.txt"),
    ("measurement", f"make bench @{SHA} -> pin:4"),
    ("replay", "episode:ereq_abc xp:xreq_123"),
    ("replay", "episode:ereq_abc/seat 2 xp:xreq_123"),
    ("replay", "episode:ereq_abc/phase S1901M xp:xreq_123"),
    ("replay", "episode:ereq_abc/seat 2/phase S1901M xp:xreq_123"),
    ("annotation", "annotation:ann_42"),
]

INVALID_REFS = [
    ("claim", "X-3"),
    ("claim", "C-0"),
    ("claim", "claim 3"),
    ("experiment", "C-5"),
    ("version", "v-7"),
    ("code", "policy/agent.py:10-20"),
    ("code", "policy/agent.py:10-20@12345"),
    ("code", f"policy/agent.py:20-10@{SHA}"),
    ("code", f"policy/agent.py:0-10@{SHA}"),
    ("code", f"policy/agent.py@{SHA}"),
    ("doc", "README.md"),
    ("doc", "https://example.org/page: the quoted passage"),
    ("doc", "#setup"),
    ("transcript", "session:api_1/msg-7"),
    ("transcript", f"session:api_1/msg-7#{'a' * 63}"),
    ("transcript", f"api_1/msg-7#{SHA256}"),
    ("measurement", "make bench -> documents/out.txt"),
    ("measurement", f"make bench @{SHA}"),
    ("replay", "episode:ereq_abc"),
    ("replay", "ereq_abc xp:xreq_123"),
    ("replay", "episode:ereq_abc/seat two xp:xreq_123"),
    ("annotation", "ann_42"),
    ("annotation", "annotation:"),
]


def test_every_profile_type_is_registered_with_all_four_parts():
    assert set(EVIDENCE_TYPES) >= {
        "claim",
        "code",
        "doc",
        "transcript",
        "measurement",
        "experiment",
        "version",
        "replay",
        "annotation",
    }
    for name, kind in EVIDENCE_TYPES.items():
        assert kind.name == name
        assert kind.ref_format.strip()
        assert callable(kind.validate)


@pytest.mark.parametrize(("type_name", "ref"), VALID_REFS)
def test_validators_accept_well_formed_refs(type_name, ref):
    assert EVIDENCE_TYPES[type_name].validate(ref) == []


@pytest.mark.parametrize(("type_name", "ref"), INVALID_REFS)
def test_validators_name_the_format_for_malformed_refs(type_name, ref):
    problems = EVIDENCE_TYPES[type_name].validate(ref)
    assert problems, f"{type_name} accepted {ref!r}"
    assert any(EVIDENCE_TYPES[type_name].ref_format in p or ref in p for p in problems)


def test_blank_ref_is_a_problem_for_every_type():
    for kind in EVIDENCE_TYPES.values():
        assert kind.validate("") != []
        assert kind.validate("   ") != []


def test_check_ref_reports_unknown_types_and_malformed_refs(coworld_store):
    problems = evidence_types.check_ref(coworld_store, "screenshot", "x")
    assert len(problems) == 1
    assert "screenshot" in problems[0] and "claim" in problems[0]
    assert evidence_types.check_ref(coworld_store, None, "x")
    assert evidence_types.check_ref(coworld_store, "code", 12) != []
    assert evidence_types.check_ref(coworld_store, "code", f"a.py:1-2@{SHA}") == []


def test_record_refs_must_exist_in_the_store(coworld_store, harness):
    assert evidence_types.check_ref(coworld_store, "claim", "C-1") == ["C-1 does not exist"]
    assert evidence_types.check_ref(coworld_store, "experiment", "X-2") == ["X-2 does not exist"]
    assert evidence_types.check_ref(coworld_store, "version", "V-3") == ["V-3 does not exist"]
    with coworld_store.tx(harness) as tx:
        tx.execute(
            "INSERT INTO claims(summary, status, created, created_by, updated)"
            " VALUES ('a claim', 'active', ?, 'harness', ?)",
            (tx.at, tx.at),
        )
        tx.execute(
            "INSERT INTO experiments(summary, status, created, created_by)"
            " VALUES ('an experiment', 'proposed', ?, 'harness')",
            (tx.at,),
        )
        tx.execute(
            "INSERT INTO versions(summary, source, created, created_by)"
            " VALUES ('a version', 'adopted', ?, 'harness')",
            (tx.at,),
        )
    assert evidence_types.check_ref(coworld_store, "claim", "C-1") == []
    assert evidence_types.check_ref(coworld_store, "claim", "C-1#evidence-1") == []
    assert evidence_types.check_ref(coworld_store, "experiment", "X-1#methods") == []
    assert evidence_types.check_ref(coworld_store, "version", "V-1") == []
    assert evidence_types.check_ref(coworld_store, "version", "V-2") == ["V-2 does not exist"]


def test_record_inspectors_return_the_record_summary(coworld_store, harness):
    with coworld_store.tx(harness) as tx:
        tx.execute(
            "INSERT INTO claims(summary, status, created, created_by, updated)"
            " VALUES ('a claim', 'active', ?, 'harness', ?)",
            (tx.at, tx.at),
        )
        tx.execute(
            "INSERT INTO experiments(summary, status, created, created_by)"
            " VALUES ('an experiment', 'running', ?, 'harness')",
            (tx.at,),
        )
        tx.execute(
            "INSERT INTO versions(summary, source, created, created_by)"
            " VALUES ('a version', 'adopted', ?, 'harness')",
            (tx.at,),
        )
    env = InspectEnv(store=coworld_store)
    assert EVIDENCE_TYPES["claim"].inspect("C-1", env) == {
        "type": "claim",
        "ref": "C-1",
        "id": "C-1",
        "summary": "a claim",
        "status": "active",
    }
    assert EVIDENCE_TYPES["experiment"].inspect("X-1#methods", env) == {
        "type": "experiment",
        "ref": "X-1#methods",
        "id": "X-1",
        "summary": "an experiment",
        "status": "running",
    }
    assert EVIDENCE_TYPES["version"].inspect("V-1", env) == {
        "type": "version",
        "ref": "V-1",
        "id": "V-1",
        "summary": "a version",
    }
    missing = evidence_types.inspect_evidence("claim", "C-9", env)
    assert missing["problem"] == "C-9 does not exist"


def test_every_type_has_an_inspector_and_only_records_resolve():
    for name, kind in EVIDENCE_TYPES.items():
        assert callable(kind.inspect), name
        assert (kind.resolve is not None) == (name in ("claim", "experiment", "version")), name


def test_inspect_evidence_never_raises(coworld_store):
    env = InspectEnv(store=coworld_store)
    unknown = evidence_types.inspect_evidence("screenshot", "x", env)
    assert unknown["type"] == "screenshot" and "not registered" in unknown["problem"]
    malformed = evidence_types.inspect_evidence("code", "agent.py", env)
    assert "<path>:<a>-<b>@<sha>" in malformed["problem"]
    for name, ref in VALID_REFS:
        result = evidence_types.inspect_evidence(name, ref, env)
        assert {"type", "ref", "summary"} <= set(result), (name, ref)


