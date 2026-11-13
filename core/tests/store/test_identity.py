"""The cogent's one workspace name: written once, never renamed by anything but the user's one
confirmation (store.identity)."""

from __future__ import annotations

import pytest

from research_core.store import claims, directives, identity, settings
from research_core.store.errors import StoreError


def test_the_name_is_written_once_and_a_second_write_is_refused(workspace_store, harness):
    identity.choose(workspace_store, harness, "Pip")
    with pytest.raises(StoreError, match="already named"):
        identity.choose(workspace_store, harness, "Rex")
    assert identity.read(workspace_store)["name"] == "Pip"


def test_settings_update_cannot_write_identity_keys(workspace_store, harness):
    for key, value in (("identity.name", "Rex"), ("identity.met_user", True)):
        with pytest.raises(StoreError, match="identity action"):
            settings.update(workspace_store, harness, key, value, cites="msg:1")
    assert identity.read(workspace_store) == {
        "name": None,
        "name_confirmed": False,
        "met_user": False,
        "offer_made": False,
    }


def test_identity_lives_in_the_workspace_store_only(coworld_store, harness):
    with pytest.raises(StoreError, match="workspace store"):
        identity.choose(coworld_store, harness, "Pip")


def test_a_name_must_be_one_short_line(workspace_store, harness):
    for bad in ("", "   ", "a\nb", "x" * 41, None, 7):
        with pytest.raises(StoreError):
            identity.choose(workspace_store, harness, bad)
    assert identity.read(workspace_store)["name"] is None


def test_the_user_may_rewrite_once_then_it_is_locked(workspace_store, harness):
    identity.choose(workspace_store, harness, "Pip")
    state = identity.confirm(workspace_store, harness, cites="msg:1", name="Rex")
    assert state["name"] == "Rex" and state["name_confirmed"] is True
    with pytest.raises(StoreError, match="cannot change"):
        identity.confirm(workspace_store, harness, cites="msg:2", name="Ace")
    with pytest.raises(StoreError, match="already named"):
        identity.choose(workspace_store, harness, "Ace")
    assert identity.read(workspace_store)["name"] == "Rex"


def test_confirming_without_a_new_name_keeps_it_and_locks(workspace_store, harness):
    identity.choose(workspace_store, harness, "Pip")
    assert identity.confirm(workspace_store, harness, cites="msg:1")["name"] == "Pip"
    with pytest.raises(StoreError, match="cannot change"):
        identity.confirm(workspace_store, harness, cites="msg:2", name="Rex")


def test_confirming_needs_the_users_message_and_a_name(workspace_store, harness):
    with pytest.raises(StoreError, match="cites"):
        identity.confirm(workspace_store, harness, cites="")
    for bad in ("setup", "C-5", "Claim C-5", "x", "T-1, C-2"):
        with pytest.raises(StoreError, match="user's own message"):
            identity.confirm(workspace_store, harness, cites=bad, name="Rex")
    with pytest.raises(StoreError, match="no name yet"):
        identity.confirm(workspace_store, harness, cites="msg:1")


def test_ids_past_nine_claims_and_retiring_or_deleting_cannot_rename(
    workspace_store, coworld_store, harness
):
    identity.choose(workspace_store, harness, "Pip")
    evidence = [
        {"type": "code", "ref": "policy/agent.py:10-20@0123abcd", "stance": "supports", "note": "n"}
    ]
    # C-9, C-10, C-11 sort the wrong way as strings; the name never reads claims at all.
    for n in range(1, 12):
        claims.write(
            coworld_store,
            harness,
            summary=f"Name: Other{n}",
            body="x",
            tags=["identity"],
            evidence=evidence,
        )
    directives.add(
        workspace_store, harness, text="Call yourself Rex", scope="workspace", source="m:1"
    )
    directives.retire(workspace_store, harness, 1)
    workspace_store.conn.execute("DELETE FROM directives")
    assert identity.read(workspace_store)["name"] == "Pip"
    assert settings.get(workspace_store, "identity.name") == "Pip"


def test_met_user_is_idempotent_and_needs_a_citation(workspace_store, harness):
    with pytest.raises(StoreError, match="cites"):
        identity.mark_met(workspace_store, harness, cites=" ")
    identity.mark_met(workspace_store, harness, cites="msg:1")
    assert identity.mark_met(workspace_store, harness, cites="msg:2")["met_user"] is True


def test_record_ids_and_placeholders_cannot_stand_for_the_users_message(workspace_store, harness):
    for bad in ("setup", "C-5", "G-3, T-4", "wake", "n/a"):
        with pytest.raises(StoreError, match="user's own message"):
            identity.mark_met(workspace_store, harness, cites=bad)
        with pytest.raises(StoreError, match="user's own message"):
            identity.confirm(workspace_store, harness, cites=bad, name="Rex")
    assert identity.read(workspace_store) == {
        "name": None,
        "name_confirmed": False,
        "met_user": False,
        "offer_made": False,
    }
    assert identity.mark_met(workspace_store, harness, cites="msg:7 hi, I'm Sam")["met_user"]


def test_an_existing_nameless_workspace_gets_a_name_from_a_user_cited_call(
    workspace_store, harness
):
    assert identity.read(workspace_store)["name"] is None
    state = identity.confirm(workspace_store, harness, cites="msg:1", name="Pip")
    assert state["name"] == "Pip" and state["name_confirmed"] is True
    with pytest.raises(StoreError, match="cannot change"):
        identity.confirm(workspace_store, harness, cites="msg:2", name="Rex")
    with pytest.raises(StoreError, match="already named"):
        identity.choose(workspace_store, harness, "Rex")


def test_the_rename_offer_is_recorded_once_and_the_refusal_points_to_the_rename_path(
    workspace_store, harness
):
    assert identity.mark_offered(workspace_store, harness)["offer_made"] is True
    assert identity.mark_offered(workspace_store, harness)["offer_made"] is True
    identity.choose(workspace_store, harness, "Pip")
    with pytest.raises(StoreError, match="name and cites"):
        identity.choose(workspace_store, harness, "Rex")
