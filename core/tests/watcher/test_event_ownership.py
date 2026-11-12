"""Every event kind has exactly one owner (design B.13 "Event ownership"; amendment A5.4)."""

from __future__ import annotations

from research_core.store import events
from research_core.watcher.handlers import (
    FUNCTION_KINDS,
    HANDLERS,
    MODEL_KINDS,
    UNWATCHED_KINDS,
    WAKE_KINDS,
)
from research_core.watcher.wakes import TEMPLATES_DIR

# The design's wake table (Appendix H) minus change.completed (A3.6), plus the kinds A5.4 and
# the recovery, wake and store events added: kind -> wake kind, or None for a no-model handler.
EXPECTED_WAKES = {
    "coworld.setup_needed": "setup",
    "task.completed": "review",
    "task.reviewed": None,
    "task.asked": "management",
    "task.proposed": "management",
    "task.proposed_submission": "management",
    "merge.failed": "management",
    "submission.candidate": "management",
    "task.wait_resolved": None,
    "capability.failed": None,
    "repair.failed_twice": None,
    "goal.needs_ideation": "management",
    "goal.park": "management",
    "workspace.resumed": None,
    "task.abandoned": None,
    "task.failed": None,
    "task.failure_review": "management",
    "champion.changed": "management",
    "store.unmarked_write": "management",
    "store.schema_rebuilt": "management",
    "store.schema_newer": "management",
    "recovery.failed": "management",
    "wake.failed": "management",
    "anti_stall": "anti_stall",
}


def test_handlers_and_unwatched_kinds_partition_the_event_kinds():
    assert set(HANDLERS) | UNWATCHED_KINDS == events.KINDS
    assert not set(HANDLERS) & UNWATCHED_KINDS
    assert "change.completed" not in HANDLERS and "change.completed" not in events.KINDS
    assert MODEL_KINDS | FUNCTION_KINDS == set(HANDLERS) and not MODEL_KINDS & FUNCTION_KINDS


def test_wake_table_matches_the_design():
    assert {kind: entry.wake for kind, entry in HANDLERS.items()} == EXPECTED_WAKES


def test_metadata_cannot_dispatch_code():
    for kind, entry in HANDLERS.items():
        assert entry.kind == kind
        assert not hasattr(entry, "resolve")
        assert entry.wake is None or entry.wake in WAKE_KINDS


def test_every_wake_kind_has_a_template_that_keeps_wakes_out_of_conversations():
    for kind in WAKE_KINDS:
        text = (TEMPLATES_DIR / f"{kind}.md").read_text(encoding="utf-8")
        for placeholder in ("{event_kind}", "{subject}", "{payload_json}", "{coworld_id}"):
            assert placeholder in text, (kind, placeholder)
        assert "never answer in this conversation" in text, kind
        assert "`chat_update {category, text, refs?}`" in text and "`progress`" in text, kind
    assert "pending_request" in (TEMPLATES_DIR / "setup.md").read_text(encoding="utf-8")
