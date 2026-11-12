"""Claims and their typed evidence (design B.7).

A claim is a short finding: a summary of at most 40 words, a body of at most 500, tags, at
least one evidence link whose reference validates and resolves (``evidence_types``), and
``see_also`` links for navigation. Freshness, contested state and evidence counts are computed
from the evidence on every read, never stored. Retired claims (superseded or refuted) are kept
and stay findable: ``search`` lists them last and marks them, and nothing deletes a claim.

Structural rules refused with every problem at once: the word limits; the evidence
requirement; every ``see_also`` and ``superseded_by`` claim exists; claim-to-claim evidence
forms no cycle (``see_also`` may, it is navigation, not support). A new claim cannot close a
cycle because nothing cites it yet, so the cycle check runs where an edge is added to an
existing claim: ``add_evidence`` and the killing evidence of ``retire``.

Identifiers: functions accept a claim as ``3`` or ``"C-3"`` (a fragment such as
``C-3#evidence-1`` still names the claim); records carry ``id`` and ``superseded_by`` in display
form and ``see_also`` as a list of display ids. Events: ``claim.created``, ``claim.updated``
(a field change or an added evidence link) and ``claim.retired``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from research_core import brain, refs
from research_core.store import evidence_types, registry
from research_core.store._common import (
    display_id,
    fetch_row,
    json_field,
    parse_iso,
    parse_json,
    require,
    word_count,
)
from research_core.store.errors import StoreError

if TYPE_CHECKING:
    from research_core.store.actors import Actor
    from research_core.store.db import Store, Tx

PREFIX = "C"
STANCES = ("supports", "contradicts", "qualifies")
RETIRED_STATUSES = ("superseded", "refuted")
SUMMARY_WORDS = 40
BODY_WORDS = 500

_WORD = re.compile(r"\w+")
_EVIDENCE_COLUMNS = 'id, type, ref, stance, note, added, "by"'


# --- writes ----------------------------------------------------------------------------------


def write(
    store: Store,
    actor: Actor,
    *,
    summary: str,
    body: str,
    tags: Sequence[str],
    evidence: Sequence[dict[str, Any]],
    see_also: Sequence[int | str] = (),
) -> dict[str, Any]:
    """Create an active claim; event ``claim.created``.

    The result is the record plus ``new_tags``: for each tag no existing claim carries, the
    nearest existing tags (so a near-duplicate tag is noticed before it spreads).
    """
    with store.tx(actor) as tx:
        problems: list[str] = []
        summary = _check_summary(summary, problems)
        body = _check_body(body, problems)
        tags = _check_tags(tags, problems)
        items = _check_evidence(store, evidence, problems)
        see_also = _check_see_also(store, see_also, problems)
        if problems:
            raise StoreError(problems)
        hints = _tag_hints(store, tags)
        cursor = tx.execute(
            "INSERT INTO claims(summary, body, tags, status, see_also, created, created_by,"
            " updated) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
            (summary, body, json_field(tags), json_field(see_also), tx.at, str(actor), tx.at),
        )
        number = cursor.lastrowid
        for item in items:
            _insert_evidence(tx, number, item)
        tx.event(
            "claim.created",
            _display(number),
            {"summary": summary, "tags": tags, "evidence": len(items)},
        )
        record = read(store, number)
    record["new_tags"] = hints
    return record


def update(
    store: Store,
    actor: Actor,
    claim_id: int | str,
    *,
    summary: str | None = None,
    body: str | None = None,
    tags: Sequence[str] | None = None,
    see_also: Sequence[int | str] | None = None,
) -> dict[str, Any]:
    """Change a claim's text, tags or navigation links; event ``claim.updated``.

    A retired claim's summary and body do not change (its text is what was superseded or
    refuted; write a new claim instead), but its tags and ``see_also`` may, so it stays findable.
    """
    number = _number(claim_id)
    with store.tx(actor) as tx:
        row = _row(store, number)
        problems: list[str] = []
        changes: dict[str, Any] = {}
        if row["status"] != "active" and (summary is not None or body is not None):
            problems.append(
                f"{_display(number)} is {row['status']}; a retired claim's summary and body do"
                " not change (write a new claim)"
            )
        if summary is not None:
            changes["summary"] = _check_summary(summary, problems)
        if body is not None:
            changes["body"] = _check_body(body, problems)
        if tags is not None:
            changes["tags"] = _check_tags(tags, problems)
        if see_also is not None:
            changes["see_also"] = _check_see_also(store, see_also, problems, self_number=number)
        require(problems, changes, "nothing to update: give summary, body, tags or see_also")
        if problems:
            raise StoreError(problems)
        hints = _tag_hints(store, changes.get("tags", []))
        assignments = ", ".join(f"{column} = ?" for column in changes)
        values = [
            json_field(value) if column in ("tags", "see_also") else value
            for column, value in changes.items()
        ]
        tx.execute(
            f"UPDATE claims SET {assignments}, updated = ? WHERE id = ?",
            (*values, tx.at, number),
        )
        tx.event("claim.updated", _display(number), {"fields": sorted(changes)})
        record = read(store, number)
    record["new_tags"] = hints
    return record


def add_evidence(
    store: Store,
    actor: Actor,
    claim_id: int | str,
    *,
    type: str,
    ref: str,
    stance: str,
    note: str = "",
) -> dict[str, Any]:
    """Attach one evidence link; event ``claim.updated`` with the link.

    Allowed on retired claims too (more killing evidence is still worth recording). Does not
    touch ``updated``: that is the claim's text, and freshness is computed from the evidence.
    """
    number = _number(claim_id)
    with store.tx(actor) as tx:
        _row(store, number)
        problems: list[str] = []
        link = {"type": type, "ref": ref, "stance": stance, "note": note}
        items = _check_evidence(store, [link], problems, citing=number, label="")
        if problems:
            raise StoreError(problems)
        evidence_id = _insert_evidence(tx, number, items[0])
        tx.event("claim.updated", _display(number), {"evidence": {**items[0], "id": evidence_id}})
        return read(store, number)


def retire(
    store: Store,
    actor: Actor,
    claim_id: int | str,
    *,
    status: str,
    superseded_by: int | str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Supersede or refute an active claim; event ``claim.retired``. The claim is kept.

    ``superseded`` needs ``superseded_by``, an existing other claim. ``refuted`` needs
    ``evidence``, the killing link; its stance defaults to ``contradicts`` and must be that.
    """
    number = _number(claim_id)
    with store.tx(actor) as tx:
        row = _row(store, number)
        problems: list[str] = []
        require(problems, status in RETIRED_STATUSES, "status is superseded or refuted")
        require(
            problems, row["status"] == "active", f"{_display(number)} is already {row['status']}"
        )
        target: int | None = None
        if status == "superseded":
            if superseded_by is None:
                problems.append("superseded needs superseded_by: the claim that replaces it")
            else:
                target = _claim_number(
                    store, superseded_by, problems, what="superseded_by", self_number=number
                )
        elif superseded_by is not None:
            problems.append("superseded_by only goes with status superseded")
        items: list[dict[str, Any]] = []
        if status == "refuted" and evidence is None:
            problems.append("refuted needs evidence: the killing link {type, ref, note}")
        if evidence is not None:
            link = {"stance": "contradicts", **evidence} if isinstance(evidence, dict) else evidence
            items = _check_evidence(store, [link], problems, citing=number, label="evidence")
            if status == "refuted" and items and items[0]["stance"] != "contradicts":
                problems.append("killing evidence contradicts the claim")
        if problems:
            raise StoreError(problems)
        tx.execute(
            "UPDATE claims SET status = ?, superseded_by = ?, updated = ? WHERE id = ?",
            (status, target, tx.at, number),
        )
        for item in items:
            _insert_evidence(tx, number, item)
        tx.event(
            "claim.retired",
            _display(number),
            {
                "status": status,
                "superseded_by": None if target is None else _display(target),
                "evidence": items[0] if items else None,
            },
        )
        return read(store, number)


# --- reads -----------------------------------------------------------------------------------


def read(store: Store, claim_id: int | str) -> dict[str, Any]:
    """The claim with its evidence and the computed freshness, contested, counts, cited_by."""
    number = _number(claim_id)
    row = _row(store, number)
    record = registry.record_from_row(registry.CLAIM, row)
    if record["superseded_by"] is not None:
        record["superseded_by"] = _display(record["superseded_by"])
    evidence = _evidence_of(store, number)
    record["evidence"] = evidence
    record.update(_computed(evidence))
    record["cited_by"] = _cited_by(store, number)
    if brain.enabled():  # IDE_BRAIN=1 only: a rule unused for 90 days is flagged stale
        record["stale"] = brain.is_stale(record, parse_iso(store.now()))
    return record


def search(
    store: Store,
    query: str | None = None,
    *,
    tags: Sequence[str] | None = None,
    include_retired: bool = True,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Claims matching ``query``: full-text when it has words, substring otherwise; with no
    query (``None`` or blank) every claim, most recently updated first, so ``tags`` alone
    lists a Goal's claims or a topic's.

    Each result carries the summary, tags, status, a matching snippet and the computed
    freshness, contested and counts. Summary matches rank above body matches; retired claims
    come last, marked ``retired``. ``tags`` keeps only claims carrying all of them.
    """
    tokens = _WORD.findall(query or "")
    if not (query or "").strip():
        rows = store.q(
            "SELECT c.*, NULL AS snippet FROM claims c"
            " ORDER BY (status <> 'active'), updated DESC, id DESC"
        )
    elif tokens:
        # Each word becomes a quoted FTS5 phrase: no user punctuation reaches the MATCH syntax.
        match = " ".join(f'"{token}"' for token in tokens)
        rows = store.q(
            "SELECT c.*, snippet(claims_fts, -1, '[', ']', '…', 12) AS snippet"
            " FROM claims_fts JOIN claims c ON c.id = claims_fts.rowid"
            " WHERE claims_fts MATCH ?"
            " ORDER BY (c.status <> 'active'), bm25(claims_fts, 10.0, 1.0, 5.0), c.id DESC",
            (match,),
        )
    else:
        like = "%" + _escape_like(query or "") + "%"
        rows = store.q(
            "SELECT c.*, NULL AS snippet FROM claims c"
            " WHERE summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\'"
            " OR tags LIKE ? ESCAPE '\\'"
            " ORDER BY (status <> 'active'), updated DESC, id DESC",
            (like, like, like),
        )
    wanted = set(tags or ())
    results: list[dict[str, Any]] = []
    for row in rows:
        if len(results) >= limit:
            break
        record = registry.record_from_row(registry.CLAIM, row)
        if not include_retired and record["status"] != "active":
            continue
        if not wanted <= set(record["tags"]):
            continue
        evidence = _evidence_of(store, row["id"])
        results.append(
            {
                "id": record["id"],
                "summary": record["summary"],
                "tags": record["tags"],
                "status": record["status"],
                "retired": record["status"] != "active",
                "see_also": record["see_also"],
                "snippet": row["snippet"] or _matching_line(row, query or ""),
                **_computed(evidence),
            }
        )
    return results


def lint(store: Store) -> dict[str, Any]:
    """Housekeeping findings: tags only one claim uses (Goal tags such as G-3 are not counted)
    and ``see_also`` links to claims that do not exist (only a write outside the library can
    leave one, since claims are never deleted)."""
    existing = {row["id"] for row in store.q("SELECT id FROM claims")}
    uses: dict[str, int] = {}
    dangling: list[dict[str, str]] = []
    for row in store.q("SELECT id, tags, see_also FROM claims ORDER BY id"):
        for tag in parse_json(row["tags"], []):
            if not refs.parse(str(tag)).is_record:
                uses[tag] = uses.get(tag, 0) + 1
        for link in parse_json(row["see_also"], []):
            parsed = refs.parse(str(link))
            if parsed.record_kind != "claim" or parsed.number not in existing:
                dangling.append({"claim": _display(row["id"]), "see_also": link})
    single = sorted(tag for tag, count in uses.items() if count == 1)
    return {"single_use_tags": single, "dangling_see_also": dangling}


# --- validation ------------------------------------------------------------------------------


def _check_summary(summary: object, problems: list[str]) -> str:
    text = summary.strip() if isinstance(summary, str) else ""
    if require(problems, text, "summary is required: the claim in one sentence"):
        words = word_count(text)
        require(
            problems,
            words <= SUMMARY_WORDS,
            f"summary has {words} words; at most {SUMMARY_WORDS}",
        )
    return text


def _check_body(body: object, problems: list[str]) -> str:
    if body is None:
        return ""
    if not require(problems, isinstance(body, str), "body is text"):
        return ""
    text = body.strip()
    words = word_count(text)
    require(problems, words <= BODY_WORDS, f"body has {words} words; at most {BODY_WORDS}")
    return text


def _check_tags(tags: object, problems: list[str]) -> list[str]:
    if not require(problems, isinstance(tags, (list, tuple)), "tags is a list of keywords"):
        return []
    cleaned: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            problems.append(f"tags: {tag!r} is not a keyword")
            continue
        if tag.strip() not in cleaned:
            cleaned.append(tag.strip())
    return cleaned


def _check_evidence(
    store: Store,
    evidence: object,
    problems: list[str],
    *,
    citing: int | None = None,
    label: str | None = None,
) -> list[dict[str, Any]]:
    """Normalised evidence links; every bad link adds its problems, prefixed ``evidence[i]:``
    for a list (``label:`` when one is given, nothing when ``label`` is empty).

    ``citing`` is the claim the links are being added to: claim-type links are then checked
    for self-citation and for closing a cycle of claim evidence.
    """
    if not require(
        problems, isinstance(evidence, (list, tuple)), "evidence is a list of {type, ref, stance}"
    ):
        return []
    if citing is None:
        require(problems, evidence, "at least one evidence link is required")
    items: list[dict[str, Any]] = []
    for index, link in enumerate(evidence):
        where = f"evidence[{index}]: " if label is None else (f"{label}: " if label else "")
        if not isinstance(link, dict):
            problems.append(f"{where}not an object with type, ref, stance and note")
            continue
        missing = [key for key in ("type", "ref", "stance") if not link.get(key)]
        if missing:
            problems.append(f"{where}missing {', '.join(missing)}")
            continue
        link_problems = evidence_types.check_ref(store, link["type"], link["ref"])
        if link["stance"] not in STANCES:
            link_problems.append(f"stance {link['stance']!r} is not one of {', '.join(STANCES)}")
        note = link.get("note") or ""
        if not isinstance(note, str):
            link_problems.append("note is text")
        if not link_problems and citing is not None and link["type"] == "claim":
            link_problems.extend(_cycle_problems(store, citing, refs.parse(link["ref"]).number))
        if link_problems:
            problems.extend(f"{where}{problem}" for problem in link_problems)
            continue
        items.append(
            {"type": link["type"], "ref": link["ref"], "stance": link["stance"], "note": note}
        )
    return items


def _check_see_also(
    store: Store, see_also: object, problems: list[str], *, self_number: int | None = None
) -> list[str]:
    if not require(problems, isinstance(see_also, (list, tuple)), "see_also is a list of claims"):
        return []
    links: list[str] = []
    for link in see_also:
        number = _claim_number(store, link, problems, what="see_also", self_number=self_number)
        if number is not None and _display(number) not in links:
            links.append(_display(number))
    return links


def _claim_number(
    store: Store, value: object, problems: list[str], *, what: str, self_number: int | None
) -> int | None:
    """The number of an existing claim named by ``value`` (``3`` or ``"C-3"``), else a problem."""
    number = _parse_number(value)
    if number is None:
        problems.append(f"{what}: {value!r} is not a claim reference (C-<n>)")
        return None
    if number == self_number:
        problems.append(f"{what}: a claim cannot point at itself ({_display(number)})")
        return None
    if store.one("SELECT id FROM claims WHERE id = ?", (number,)) is None:
        problems.append(f"{what}: {_display(number)} does not exist")
        return None
    return number


def _cycle_problems(store: Store, citing: int, target: int | None) -> list[str]:
    if target == citing:
        return [f"a claim cannot cite itself ({_display(citing)})"]
    path = _cycle_path(store, citing, target)
    if path is None:
        return []
    shown = " → ".join(_display(n) for n in path)
    return [
        f"citing {_display(target)} from {_display(citing)} would form a cycle: {shown}; claim"
        " evidence must not cite back, so leave this link out (a new claim may name it in see_also)"
    ]


def _cycle_path(store: Store, citing: int, target: int) -> list[int] | None:
    """The cycle ``citing → target → ... → citing`` that the new edge would close, if any:
    a breadth-first walk from ``target`` along existing claim-to-claim evidence."""
    edges: dict[int, list[int]] = {}
    for row in store.q("SELECT claim_id, ref FROM claim_evidence WHERE type = 'claim'"):
        cited = refs.parse(row["ref"]).number
        if cited is not None:
            edges.setdefault(row["claim_id"], []).append(cited)
    parents: dict[int, int | None] = {target: None}
    queue = [target]
    while queue:
        node = queue.pop(0)
        if node == citing:
            chain: list[int] = []
            step: int | None = node
            while step is not None:
                chain.append(step)
                step = parents[step]
            chain.reverse()
            return [citing, *chain]
        for cited in edges.get(node, ()):
            if cited not in parents:
                parents[cited] = node
                queue.append(cited)
    return None


def _tag_hints(store: Store, tags: Sequence[str]) -> dict[str, list[str]]:
    """For each tag no claim carries yet, the nearest existing tags (difflib)."""
    existing: set[str] = set()
    for row in store.q("SELECT tags FROM claims"):
        existing.update(parse_json(row["tags"], []))
    return {
        tag: difflib.get_close_matches(tag, sorted(existing), n=3, cutoff=0.6)
        for tag in tags
        if tag not in existing
    }


# --- computed fields and helpers -------------------------------------------------------------


def _computed(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Freshness (newest supporting or qualifying link), contested (a contradicting link newer
    than that) and the counts per stance (design B.7)."""
    counts = dict.fromkeys(STANCES, 0)
    freshness: str | None = None
    newest_contradiction: str | None = None
    for link in evidence:
        counts[link["stance"]] += 1
        if link["stance"] == "contradicts":
            newest_contradiction = max(newest_contradiction or "", link["added"])
        else:
            freshness = max(freshness or "", link["added"])
    contested = newest_contradiction is not None and (
        freshness is None or newest_contradiction > freshness
    )
    return {"freshness": freshness, "contested": contested, "counts": counts}


def _evidence_of(store: Store, number: int) -> list[dict[str, Any]]:
    rows = store.q(
        f"SELECT {_EVIDENCE_COLUMNS} FROM claim_evidence WHERE claim_id = ? ORDER BY id", (number,)
    )
    return [dict(row) for row in rows]


def _cited_by(store: Store, number: int) -> dict[str, list[str]]:
    display = _display(number)
    citing_claims: list[str] = []
    for row in store.q(
        "SELECT DISTINCT claim_id, ref FROM claim_evidence WHERE type = 'claim' ORDER BY claim_id"
    ):
        if (
            refs.parse(row["ref"]).number == number
            and _display(row["claim_id"]) not in citing_claims
        ):
            citing_claims.append(_display(row["claim_id"]))
    experiments: list[str] = []
    for row in store.q("SELECT id, related_claims FROM experiments ORDER BY id"):
        related = parse_json(row["related_claims"], [])
        if display in related or number in related:
            experiments.append(display_id("X", row["id"]))
    return {"claims": citing_claims, "experiments": experiments}


def _insert_evidence(tx: Tx, number: int, item: dict[str, Any]) -> int:
    cursor = tx.execute(
        'INSERT INTO claim_evidence(claim_id, type, ref, stance, note, added, "by")'
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (number, item["type"], item["ref"], item["stance"], item["note"], tx.at, str(tx.actor)),
    )
    return cursor.lastrowid


def _matching_line(row: Any, query: str) -> str:
    """The first line of the summary or body containing ``query`` (the LIKE fallback's
    snippet); the summary when none does."""
    needle = query.casefold()
    for text in (row["summary"], row["body"]):
        for line in text.splitlines():
            if needle in line.casefold():
                return line.strip()
    return row["summary"]


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str):
        parsed = refs.parse(value)
        if parsed.is_record and parsed.record_kind == "claim":
            return parsed.number
    return None


def _number(claim_id: object) -> int:
    number = _parse_number(claim_id)
    if number is None:
        raise StoreError([f"{claim_id!r} is not a claim id (C-<n>)"])
    return number


def _display(number: int) -> str:
    return display_id(PREFIX, number)


def _row(store: Store, number: int) -> Any:
    return fetch_row(store, "claims", number, display=_display(number))
