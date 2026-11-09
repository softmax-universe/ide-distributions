"""The ``claim`` tool (design B.7, C.2): short findings with typed evidence, for both roles.

The store module does the work (``claims.write`` and friends compute freshness, contested
state, counts and tag hints on every record); this module maps actions to it and adds
``inspect_evidence``, which shows what a link says through the evidence type's inspector.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from research_core.store import claims, evidence_types
from research_core.tools import _args as a
from research_core.tools.base import ActionSpec, ToolCall, ToolContext, ToolError, ToolSpec, tool

BOTH = frozenset({"orchestrator", "researcher"})
CLAIM = a.string("The claim, such as C-3.")
# No enum: the description below names each type with its reference format.
EVIDENCE_TYPE = a.string("The evidence type, such as code or claim.")
EVIDENCE_REF = a.string("The reference, in the type's format (see claim_write's evidence.ref).")
STANCE = a.string("How the evidence bears on the claim.", enum=list(claims.STANCES))
NOTE = a.string("What the evidence shows, in a few words.")
EVIDENCE_LINK = a.obj(
    "",
    {
        "type": EVIDENCE_TYPE,
        "ref": a.string(
            "The reference in the type's format, without the type name: "
            + "; ".join(
                f"for {name}, {kind.ref_format}"
                for name, kind in sorted(evidence_types.EVIDENCE_TYPES.items())
            )
            + "."
        ),
        "stance": STANCE,
        "note": NOTE,
    },
    required=("type", "ref", "stance"),
)
TAGS = a.strings("Keywords from the shared vocabulary, and the Goal's id (G-n) to count for it.")

_EVIDENCE_SELECTOR = re.compile(r"^(?:[A-Z]-[0-9]+#)?(?:evidence-)?(?P<number>[1-9][0-9]*)$")


def handle(call: ToolCall) -> dict:
    store, actor = call.ctx.store, call.ctx.actor
    if call.action == "search":
        records = claims.search(
            store,
            call.get("query"),
            tags=call.get("tags"),
            include_retired=call.get("include_retired", True),
            limit=call.get("limit") or 20,
        )
        return {"records": records}
    if call.action == "write":
        record = claims.write(
            store,
            actor,
            summary=call.get("summary"),
            body=call.get("body"),
            tags=call.get("tags"),
            evidence=call.get("evidence"),
            see_also=call.get("see_also") or (),
        )
        return {"record": record}
    if call.action == "inspect_evidence":
        return inspect(call)
    claim_id = a.record_number(call.get("claim"), "claim", "claim")
    if call.action == "read":
        return {"record": claims.read(store, claim_id)}
    if call.action == "add_evidence":
        record = claims.add_evidence(
            store,
            actor,
            claim_id,
            type=call.get("type"),
            ref=call.get("ref"),
            stance=call.get("stance"),
            note=call.get("note") or "",
        )
        return {"record": record}
    if call.action == "retire":
        # A note alone is not a link: models send one with a supersede to say why (2026-11).
        killing = {name: call.get(name) for name in ("type", "ref", "note") if call.get(name)}
        record = claims.retire(
            store,
            actor,
            claim_id,
            status=call.get("status"),
            superseded_by=call.get("superseded_by"),
            evidence=killing if call.get("type") or call.get("ref") else None,
        )
        return {"record": record}
    raise AssertionError(call.action)


def inspect(call: ToolCall) -> dict:
    """One link given as ``type`` + ``ref``, or a claim's links (all, or the one ``evidence``
    names). Each inspection is the inspector's answer; links from a claim also carry their
    ``evidence_id``, ``stance`` and ``note``."""
    env = inspect_env(call.ctx)
    type_name, ref = call.get("type"), call.get("ref")
    if type_name or ref:
        if not (type_name and ref):
            raise ToolError("inspecting a link directly needs both type and ref")
        return {"inspections": [evidence_types.inspect_evidence(type_name, ref, env)]}
    if not call.get("claim"):
        raise ToolError("give a claim (and optionally which evidence), or a type and ref")
    record = claims.read(call.ctx.store, a.record_number(call.get("claim"), "claim", "claim"))
    links = record["evidence"]
    if call.get("evidence_id") is not None:
        links = [select_link(links, call.get("evidence_id"), record["id"])]
    inspections = [
        {
            "evidence_id": link["id"],
            "stance": link["stance"],
            "note": link["note"],
            **evidence_types.inspect_evidence(link["type"], link["ref"], env),
        }
        for link in links
    ]
    return {"claim": record["id"], "inspections": inspections}


def select_link(links: list[dict[str, Any]], selector: Any, claim: str) -> dict[str, Any]:
    """The link ``selector`` names: its ``id`` as the record shows it (also as ``evidence-7`` or
    ``C-3#evidence-7``), else its 1-based position when no link has that id."""
    match = _EVIDENCE_SELECTOR.match(str(selector).strip())
    number = int(match["number"]) if match else None
    ids = ", ".join(str(link["id"]) for link in links)
    if number is None:
        raise ToolError(f"evidence_id names a link of {claim} by id ({ids}) or position")
    for link in links:
        if link["id"] == number:
            return link
    if 1 <= number <= len(links):
        return links[number - 1]
    raise ToolError(f"{claim} has no evidence {number}; its links are {ids}")


def inspect_env(ctx: ToolContext) -> evidence_types.InspectEnv:
    coworld = ctx.coworld()
    hermes_home = ctx.env.get("HERMES_HOME")
    return evidence_types.InspectEnv(
        store=ctx.store,
        code_repo=coworld.code,
        hermes_state_db=Path(hermes_home).expanduser() / "state.db" if hermes_home else None,
        documents=coworld.documents,
    )


SPEC = tool(
    ToolSpec(
        name="claim",
        description=(
            "Claims: short findings backed by typed evidence links. Freshness, contested state "
            "and counts are computed from the evidence; retired claims stay findable."
        ),
        handler=handle,
        roles=BOTH,
        executor="inproc",
        actions=(
            ActionSpec(
                "search",
                "Find claims by words in their summary, body and tags; retired claims come last.",
                {
                    "query": a.string("Words to search for."),
                    "tags": a.strings("Keep only claims carrying all of these tags."),
                    "include_retired": a.boolean(
                        "Include superseded and refuted claims (default true)."
                    ),
                    "limit": a.integer("At most this many results (default 20)."),
                },
                ("query",),
            ),
            ActionSpec(
                "read",
                "Read a claim with its evidence, freshness, contested state and citations.",
                {"claim": CLAIM},
                ("claim",),
            ),
            ActionSpec(
                "write",
                "Record a new finding with at least one evidence link.",
                {
                    "summary": a.string("The claim in one sentence of at most 40 words."),
                    "body": a.string(
                        "Detail, numbers, conditions and what would change our mind; at most "
                        "500 words."
                    ),
                    "tags": TAGS,
                    "evidence": a.objects(
                        "Evidence links, at least one; each reference must validate.",
                        EVIDENCE_LINK,
                        min_items=1,
                    ),
                    "see_also": a.strings("Claims worth reading alongside, such as C-3."),
                },
                ("summary", "body", "tags", "evidence"),
                own_tool=True,
            ),
            ActionSpec(
                "add_evidence",
                "Attach one evidence link to a claim (allowed on retired claims too).",
                {
                    "claim": CLAIM,
                    "type": EVIDENCE_TYPE,
                    "ref": EVIDENCE_REF,
                    "stance": STANCE,
                    "note": NOTE,
                },
                ("claim", "type", "ref", "stance"),
                own_tool=True,
            ),
            ActionSpec(
                "retire",
                "Supersede a claim with another (status superseded needs superseded_by), or refute "
                "it with killing evidence (status refuted needs type and ref); the claim is kept "
                "and stays findable.",
                {
                    "claim": CLAIM,
                    "status": a.string(
                        "superseded or refuted.", enum=list(claims.RETIRED_STATUSES)
                    ),
                    "superseded_by": a.string("For superseded: the claim that replaces it."),
                    "type": EVIDENCE_TYPE,
                    "ref": EVIDENCE_REF,
                    "note": NOTE,
                },
                ("claim", "status"),
                example_fields=("superseded_by",),
            ),
            ActionSpec(
                "inspect_evidence",
                "Show what evidence says: code lines at their commit and whether they changed, "
                "a transcript message and its hash check, a cited record, a stored output. "
                "Give a claim (all its links, or the one evidence_id names) or a type and ref.",
                {
                    "claim": CLAIM,
                    "evidence_id": a.string(
                        "Which link of the claim: its id as the record shows it (or its "
                        "position in the list); omit for all of them."
                    ),
                    "type": EVIDENCE_TYPE,
                    "ref": EVIDENCE_REF,
                },
                example_fields=("claim",),
            ),
        ),
    )
)
