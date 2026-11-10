"""Claims: structural rules, typed evidence, computed freshness, search and lint (B.7)."""

from __future__ import annotations

import pytest

from research_core.store import claims
from research_core.store.errors import NotFound, StoreError

SHA = "0123abcd"
CODE = {"type": "code", "ref": f"policy/agent.py:10-20@{SHA}", "stance": "supports", "note": "n"}
DOC = {"type": "doc", "ref": "README.md#setup", "stance": "supports", "note": "the setup section"}
CONTRA = {"type": "doc", "ref": "README.md#limits", "stance": "contradicts", "note": "limits"}
T0 = "2026-10-28T21:04:05.123456Z"


def write(store, actor, summary="Opening with scouts wins more often.", **overrides):
    fields = {
        "summary": summary,
        "body": "Detail: 12 of 20 episodes, 2026-10-28. Changes our mind: a losing streak.",
        "tags": ["opening", "scouts"],
        "evidence": [CODE],
    }
    fields.update(overrides)
    return claims.write(store, actor, **fields)


def events_of(store, kind):
    return [e for e in store.events_after(0) if e.kind == kind]


# --- write ----------------------------------------------------------------------------------


def test_write_returns_the_record_with_computed_fields_and_an_event(coworld_store, session_actor):
    record = write(coworld_store, session_actor, see_also=())
    assert record["id"] == "C-1"
    assert record["status"] == "active"
    assert record["tags"] == ["opening", "scouts"]
    assert record["see_also"] == []
    assert record["superseded_by"] is None
    assert record["created"] == T0 and record["updated"] == T0
    assert record["created_by"] == str(session_actor)
    assert record["evidence"] == [
        {
            "id": 1,
            "type": "code",
            "ref": f"policy/agent.py:10-20@{SHA}",
            "stance": "supports",
            "note": "n",
            "added": T0,
            "by": str(session_actor),
        }
    ]
    assert record["freshness"] == T0
    assert record["contested"] is False
    assert record["counts"] == {"supports": 1, "contradicts": 0, "qualifies": 0}
    assert record["cited_by"] == {"claims": [], "experiments": []}
    # Both tags are new to the store and nothing is near them yet.
    assert record["new_tags"] == {"opening": [], "scouts": []}
    created = events_of(coworld_store, "claim.created")
    assert len(created) == 1
    assert created[0].subject == "C-1"
    assert created[0].actor == str(session_actor)
    assert created[0].payload["summary"] == record["summary"]
    assert coworld_store.unmarked_changes() == []
    assert claims.read(coworld_store, "C-1") == {k: v for k, v in record.items() if k != "new_tags"}
    assert claims.read(coworld_store, 1)["id"] == "C-1"


def test_word_limits_are_reported_together(coworld_store, harness):
    with pytest.raises(StoreError) as info:
        write(coworld_store, harness, summary="word " * 41, body="word " * 501)
    problems = info.value.problems
    assert len(problems) == 2
    assert any("summary" in p and "41" in p and "40" in p for p in problems)
    assert any("body" in p and "501" in p and "500" in p for p in problems)
    assert coworld_store.q("SELECT id FROM claims") == []
    assert coworld_store.events_after(0) == []
    # Exactly at the limits is fine.
    write(coworld_store, harness, summary="word " * 40, body="word " * 500)


def test_empty_summary_is_refused(coworld_store, harness):
    with pytest.raises(StoreError) as info:
        write(coworld_store, harness, summary="   ")
    assert any("summary" in p for p in info.value.problems)


def test_evidence_is_required_and_every_bad_link_is_named(coworld_store, harness):
    with pytest.raises(StoreError) as info:
        write(coworld_store, harness, evidence=[])
    assert info.value.problems == ["at least one evidence link is required"]
    with pytest.raises(StoreError) as info:
        write(
            coworld_store,
            harness,
            evidence=[
                {"type": "screenshot", "ref": "x", "stance": "supports"},
                {"type": "code", "ref": "agent.py", "stance": "supports"},
                {"type": "doc", "ref": "README.md#a", "stance": "maybe"},
                {"type": "claim", "ref": "C-9", "stance": "supports"},
                "not a link",
                {"ref": "README.md#a", "stance": "supports"},
            ],
        )
    problems = info.value.problems
    assert any(p.startswith("evidence[0]") and "screenshot" in p for p in problems)
    assert any(p.startswith("evidence[1]") and "<path>:<a>-<b>@<sha>" in p for p in problems)
    assert any(p.startswith("evidence[2]") and "maybe" in p for p in problems)
    assert any(p.startswith("evidence[3]") and "C-9 does not exist" in p for p in problems)
    assert any(p.startswith("evidence[4]") for p in problems)
    assert any(p.startswith("evidence[5]") and "type" in p for p in problems)
    assert coworld_store.q("SELECT id FROM claims") == []


def test_see_also_must_name_existing_claims(coworld_store, harness):
    write(coworld_store, harness)
    with pytest.raises(StoreError) as info:
        write(coworld_store, harness, see_also=["C-1", "C-7", "X-1", 3])
    problems = info.value.problems
    assert any("C-7 does not exist" in p for p in problems)
    assert any("C-3 does not exist" in p for p in problems)
    assert any("X-1" in p and "claim" in p for p in problems)
    record = write(coworld_store, harness, see_also=["C-1", 1])
    assert record["see_also"] == ["C-1"]


def test_tags_are_cleaned_and_new_tags_get_nearest_hints(coworld_store, harness):
    write(coworld_store, harness, tags=["opening", "scouting", "G-1"])
    record = write(coworld_store, harness, tags=[" opening ", "scouts", "scouts", "G-1"])
    assert record["tags"] == ["opening", "scouts", "G-1"]
    assert record["new_tags"] == {"scouts": ["scouting"]}
    with pytest.raises(StoreError) as info:
        write(coworld_store, harness, tags=["ok", "", 3])
    assert len([p for p in info.value.problems if "tags" in p]) == 2
    with pytest.raises(StoreError):
        write(coworld_store, harness, tags="opening")


def test_claim_ids_are_validated(coworld_store, harness):
    write(coworld_store, harness)
    with pytest.raises(NotFound) as info:
        claims.read(coworld_store, "C-2")
    assert info.value.problems == ["C-2 does not exist"]
    for bad in ("G-1", "c-1", 0, "one"):
        with pytest.raises(StoreError):
            claims.read(coworld_store, bad)


# --- evidence, cycles, freshness ----------------------------------------------------------------


def test_add_evidence_resolves_refs_and_updates_counts(coworld_store, session_actor, clock):
    write(coworld_store, session_actor)
    clock.advance(60)
    record = claims.add_evidence(
        coworld_store, session_actor, "C-1", type="doc", ref=DOC["ref"], stance="qualifies"
    )
    assert record["counts"] == {"supports": 1, "contradicts": 0, "qualifies": 1}
    assert record["evidence"][1]["note"] == ""
    assert record["evidence"][1]["added"] == "2026-10-28T21:05:05.123456Z"
    assert record["freshness"] == "2026-10-28T21:05:05.123456Z"
    assert record["updated"] == T0, "adding evidence does not edit the claim's text"
    updated = events_of(coworld_store, "claim.updated")
    assert len(updated) == 1 and updated[0].payload["evidence"]["ref"] == DOC["ref"]
    with pytest.raises(StoreError) as info:
        claims.add_evidence(
            coworld_store, session_actor, "C-1", type="claim", ref="C-5", stance="x"
        )
    assert len(info.value.problems) == 2
    with pytest.raises(NotFound):
        claims.add_evidence(
            coworld_store, session_actor, "C-4", type="doc", ref="a#b", stance="supports"
        )


def test_claim_evidence_cannot_form_a_cycle(coworld_store, harness):
    write(coworld_store, harness, summary="A")
    write(coworld_store, harness, summary="B", evidence=[{**DOC, "type": "claim", "ref": "C-1"}])
    write(coworld_store, harness, summary="C", evidence=[{**DOC, "type": "claim", "ref": "C-2"}])
    record = claims.read(coworld_store, "C-1")
    assert record["cited_by"]["claims"] == ["C-2"]
    with pytest.raises(StoreError) as info:
        claims.add_evidence(
            coworld_store, harness, "C-1", type="claim", ref="C-3", stance="supports"
        )
    assert info.value.problems[0].startswith(
        "citing C-3 from C-1 would form a cycle: C-1 → C-3 → C-2 → C-1; "
    )
    with pytest.raises(StoreError) as info:
        claims.add_evidence(
            coworld_store, harness, "C-1", type="claim", ref="C-1", stance="supports"
        )
    assert info.value.problems == ["a claim cannot cite itself (C-1)"]
    # see_also may point back: it is navigation, not support.
    claims.update(coworld_store, harness, "C-1", see_also=["C-3"])
    # A fragment still names the claim, so the cycle check sees through it.
    with pytest.raises(StoreError):
        claims.add_evidence(
            coworld_store, harness, "C-1", type="claim", ref="C-3#evidence-1", stance="supports"
        )


def test_freshness_and_contested_follow_the_evidence_times(coworld_store, harness, clock):
    write(coworld_store, harness)
    clock.advance(3600)
    record = claims.add_evidence(coworld_store, harness, "C-1", **CONTRA)
    assert record["freshness"] == T0
    assert record["contested"] is True
    assert record["counts"]["contradicts"] == 1
    clock.advance(3600)
    record = claims.add_evidence(coworld_store, harness, "C-1", **DOC)
    assert record["freshness"] == "2026-10-28T23:04:05.123456Z"
    assert record["contested"] is False
    # Contradiction and support written in the same transaction do not contest the claim.
    record = write(coworld_store, harness, evidence=[CODE, CONTRA])
    assert record["contested"] is False
    # A claim with only contradicting evidence has no freshness and is contested.
    record = write(coworld_store, harness, evidence=[CONTRA])
    assert record["freshness"] is None and record["contested"] is True


def test_cited_by_lists_experiments_that_relate_the_claim(coworld_store, harness):
    write(coworld_store, harness)
    with coworld_store.tx(harness) as tx:
        tx.execute(
            "INSERT INTO experiments(summary, status, related_claims, created, created_by)"
            " VALUES ('x', 'proposed', '[\"C-1\"]', ?, 'harness')",
            (tx.at,),
        )
        tx.execute(
            "INSERT INTO experiments(summary, status, related_claims, created, created_by)"
            " VALUES ('y', 'proposed', '[1]', ?, 'harness')",
            (tx.at,),
        )
        tx.execute(
            "INSERT INTO experiments(summary, status, related_claims, created, created_by)"
            " VALUES ('z', 'proposed', '[\"C-2\"]', ?, 'harness')",
            (tx.at,),
        )
    assert claims.read(coworld_store, "C-1")["cited_by"]["experiments"] == ["X-1", "X-2"]


# --- update and retire ---------------------------------------------------------------------------


def test_update_changes_fields_and_reports_new_tags(coworld_store, harness, clock):
    write(coworld_store, harness, tags=["opening"])
    clock.advance(10)
    record = claims.update(
        coworld_store,
        harness,
        "C-1",
        summary="Opening with scouts wins.",
        tags=["opening", "scout"],
    )
    assert record["summary"] == "Opening with scouts wins."
    assert record["tags"] == ["opening", "scout"]
    assert record["updated"] == "2026-10-28T21:04:15.123456Z"
    assert record["new_tags"] == {"scout": ["opening"]} or record["new_tags"] == {"scout": []}
    updated = events_of(coworld_store, "claim.updated")
    assert updated[-1].payload == {"fields": ["summary", "tags"]}
    with pytest.raises(StoreError) as info:
        claims.update(coworld_store, harness, "C-1", summary="word " * 41, see_also=["C-1"])
    assert len(info.value.problems) == 2
    with pytest.raises(StoreError):
        claims.update(coworld_store, harness, "C-1")


def test_retire_rules(coworld_store, harness):
    write(coworld_store, harness, summary="old")
    write(coworld_store, harness, summary="new")
    with pytest.raises(StoreError) as info:
        claims.retire(coworld_store, harness, "C-1", status="archived")
    assert any("superseded or refuted" in p for p in info.value.problems)
    with pytest.raises(StoreError) as info:
        claims.retire(coworld_store, harness, "C-1", status="superseded")
    assert any("superseded_by" in p for p in info.value.problems)
    with pytest.raises(StoreError) as info:
        claims.retire(coworld_store, harness, "C-1", status="superseded", superseded_by="C-9")
    assert any("C-9 does not exist" in p for p in info.value.problems)
    with pytest.raises(StoreError) as info:
        claims.retire(coworld_store, harness, "C-1", status="superseded", superseded_by="C-1")
    assert any("itself" in p for p in info.value.problems)
    with pytest.raises(StoreError) as info:
        claims.retire(coworld_store, harness, "C-1", status="refuted")
    assert any("evidence" in p for p in info.value.problems)
    with pytest.raises(StoreError) as info:
        claims.retire(coworld_store, harness, "C-1", status="refuted", evidence=DOC)
    assert any("contradicts" in p for p in info.value.problems)

    record = claims.retire(coworld_store, harness, "C-1", status="superseded", superseded_by=2)
    assert record["status"] == "superseded" and record["superseded_by"] == "C-2"
    retired = events_of(coworld_store, "claim.retired")
    assert retired[0].subject == "C-1"
    assert retired[0].payload == {"status": "superseded", "superseded_by": "C-2", "evidence": None}
    with pytest.raises(StoreError) as info:
        claims.retire(coworld_store, harness, "C-1", status="refuted", evidence=CONTRA)
    assert info.value.problems == ["C-1 is already superseded"]

    killing = {"type": "doc", "ref": "README.md#limits", "note": "the limits section kills it"}
    record = claims.retire(coworld_store, harness, "C-2", status="refuted", evidence=killing)
    assert record["status"] == "refuted" and record["superseded_by"] is None
    assert record["evidence"][-1]["stance"] == "contradicts"
    assert record["counts"]["contradicts"] == 1
    assert (
        events_of(coworld_store, "claim.retired")[1].payload["evidence"]["stance"] == "contradicts"
    )


def test_retired_claims_keep_their_text_but_stay_navigable(coworld_store, harness):
    write(coworld_store, harness, summary="old")
    write(coworld_store, harness, summary="new")
    claims.retire(coworld_store, harness, "C-1", status="superseded", superseded_by="C-2")
    with pytest.raises(StoreError) as info:
        claims.update(coworld_store, harness, "C-1", summary="rewritten")
    assert any("superseded" in p for p in info.value.problems)
    record = claims.update(coworld_store, harness, "C-1", tags=["old"], see_also=["C-2"])
    assert record["tags"] == ["old"] and record["see_also"] == ["C-2"]
    record = claims.add_evidence(coworld_store, harness, "C-1", **CONTRA)
    assert record["counts"]["contradicts"] == 1


# --- search and lint ----------------------------------------------------------------------------


def test_search_ranks_summary_matches_first_and_lists_retired_last(coworld_store, harness, clock):
    write(coworld_store, harness, summary="Latency drops when the cache is warm.", tags=["cache"])
    write(
        coworld_store,
        harness,
        summary="Scouts open well.",
        body="An aside: latency was not measured here.",
        tags=["opening"],
    )
    write(coworld_store, harness, summary="Latency is unaffected by seat order.", tags=["cache"])
    write(coworld_store, harness, summary="Nothing about delay here.")
    claims.retire(coworld_store, harness, "C-1", status="refuted", evidence=CONTRA)
    results = claims.search(coworld_store, "latency")
    assert [r["id"] for r in results] == ["C-3", "C-2", "C-1"]
    assert [r["retired"] for r in results] == [False, False, True]
    assert results[-1]["status"] == "refuted"
    first = results[0]
    assert set(first) >= {
        "id",
        "summary",
        "tags",
        "status",
        "retired",
        "snippet",
        "freshness",
        "contested",
        "counts",
        "see_also",
    }
    assert "[Latency]" in first["snippet"]
    assert first["freshness"] == T0 and first["counts"]["supports"] == 1
    assert [r["id"] for r in claims.search(coworld_store, "latency", include_retired=False)] == [
        "C-3",
        "C-2",
    ]
    assert [r["id"] for r in claims.search(coworld_store, "latency", tags=["cache"])] == [
        "C-3",
        "C-1",
    ]
    assert [r["id"] for r in claims.search(coworld_store, "latency", limit=1)] == ["C-3"]
    assert claims.search(coworld_store, "nonexistentword") == []
    # No query lists every claim, active first and newest first; tags alone narrow it, which
    # is how a Goal's claims are listed.
    assert [r["id"] for r in claims.search(coworld_store)] == ["C-4", "C-3", "C-2", "C-1"]
    assert [r["id"] for r in claims.search(coworld_store, None, tags=["cache"])] == ["C-3", "C-1"]
    assert [r["id"] for r in claims.search(coworld_store, "  ", tags=["opening"])] == ["C-4", "C-2"]


def test_search_survives_punctuation_and_falls_back_to_like(coworld_store, harness):
    write(coworld_store, harness, summary='foo:bar "x" is quoted', body="see C++ and 100%")
    write(coworld_store, harness, summary="unrelated", body="plain")
    assert [r["id"] for r in claims.search(coworld_store, 'foo:bar "x"')] == ["C-1"]
    for query in ('"unbalanced', "AND OR NOT", "a*b", "(x", "x NEAR y", "col:x"):
        claims.search(coworld_store, query)
    # No word characters: a plain substring match, with LIKE wildcards taken literally.
    results = claims.search(coworld_store, "++")
    assert [r["id"] for r in results] == ["C-1"]
    assert results[0]["snippet"] == "see C++ and 100%"
    assert [r["id"] for r in claims.search(coworld_store, "%")] == ["C-1"]
    assert [r["id"] for r in claims.search(coworld_store, "")] == ["C-2", "C-1"]


def test_lint_lists_single_use_tags_and_dangling_see_also(coworld_store, harness):
    write(coworld_store, harness, tags=["opening", "scouts", "G-1"])
    write(coworld_store, harness, tags=["opening", "cache", "G-2"], see_also=["C-1"])
    assert claims.lint(coworld_store) == {
        "single_use_tags": ["cache", "scouts"],
        "dangling_see_also": [],
    }
    # Claims are never deleted, so a dangling link only comes from a write outside the library.
    with coworld_store.tx(harness) as tx:
        tx.execute('UPDATE claims SET see_also = \'["C-9", "C-1"]\' WHERE id = 2')
    assert claims.lint(coworld_store)["dangling_see_also"] == [{"claim": "C-2", "see_also": "C-9"}]
