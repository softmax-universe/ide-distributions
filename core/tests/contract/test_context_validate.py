import json

from research_core import contract


def test_valid_block_keeps_fields():
    block = {
        "contract_version": 1,
        "client": {"id": "coplay-1", "kind": "desktop", "guide": "client-coplay"},
        "mode": {"name": "research", "instruction": "Be brief."},
        "view": {"id": "replay", "label": "Replay"},
        "on_screen": ["G-3", "X-12"],
        "references": [{"kind": "record", "ref": "C-1"}],
    }
    kept, problems = contract.validate_context(block)
    assert problems == []
    assert kept == block


def test_unknown_fields_dropped_and_named():
    kept, problems = contract.validate_context(
        {"contract_version": 1, "bogus": 1, "client": {"id": "x", "extra": 2}}
    )
    assert kept == {"contract_version": 1, "client": {"id": "x"}}
    assert sorted(problems) == [
        "context.bogus: unknown field, dropped",
        "context.client.extra: unknown field, dropped",
    ]


def test_oversize_values_refused_not_truncated():
    kept, problems = contract.validate_context(
        {
            "contract_version": 1,
            "mode": {"name": "x" * 41, "instruction": "y" * 1001},
            "on_screen": [f"G-{i}" for i in range(21)],
        }
    )
    assert "mode" not in kept and "on_screen" not in kept
    assert any("at most 40 characters" in p for p in problems)
    assert any("at most 1000 characters" in p for p in problems)
    assert any("at most 20 items" in p for p in problems)


def test_client_data_cap_and_total_cap():
    big = {"contract_version": 1, "client_data": {"blob": "z" * 9000}}
    kept, problems = contract.validate_context(big)
    assert "client_data" not in kept and any("client_data" in p for p in problems)
    huge = {
        "contract_version": 1,
        "opening_goal": "q" * 1000,
        "client_data": {"a": "b" * 7000},
        "x": "w" * 9000,
    }
    kept, problems = contract.validate_context(huge)
    assert kept == {} and problems[0].startswith("context: at most 16384 bytes")


def test_bad_types_and_missing_version():
    kept, problems = contract.validate_context({"contract_version": 2, "league": 5})
    assert kept == {}
    assert "context.contract_version: must be 1" in problems
    assert "context.league: must be ['string', 'null']" in problems
    assert "context.contract_version: required" in problems
    assert contract.validate_context("garbage") == ({}, ["context: must be a JSON object"])
    assert contract.validate_context(None)[1] == ["context: must be a JSON object"]


def test_schema_files_are_json_with_version():
    assert contract.schema("context")["contract_version"] == contract.CONTRACT_VERSION
    json.dumps(contract.schema("context"))


def test_context_schema_command(capsys):
    from research_core import cli

    assert cli.main(["context-schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Turn context"
    assert cli.main(["context-schema", "--name", "events"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Event"
    for name in ("context", "events"):
        assert contract.schema(name)["contract_version"] == 1
