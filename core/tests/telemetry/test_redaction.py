"""Softmax tokens, in each form they turn up in, built the way the platform builds them."""

import base64
import json

import pytest

from research_core.agent_telemetry.redaction import (
    SOFTMAX_TOKEN_MASK,
    SOFTMAX_TOKEN_PREFIXES,
    Redactor,
)

from .conftest import HERMES_STYLE_SECRET, scrub


def fake_token(prefix: str, seed: int = 7) -> str:
    """Same shape as metta's ``generate_token_pair``: 32 bytes as URL-safe base64, with padding."""
    raw = bytes((seed + index * 37) % 256 for index in range(32))
    return prefix + base64.urlsafe_b64encode(raw).decode()


USER_TOKEN = fake_token("usr_")
PLAYER_TOKEN = fake_token("ply_")
PLAYER_ID = "ply_f39295d0-ac38-4a62-ac4f-e1d62d7dc9d1"


@pytest.fixture
def plain() -> Redactor:
    return Redactor(lambda text: text, "test")


def test_a_token_body_full_of_url_safe_characters_is_caught(plain):
    token = "usr_" + "-_" * 21 + "A="
    assert plain.text(f"x {token} y") == f"x {SOFTMAX_TOKEN_MASK} y"


@pytest.mark.parametrize(
    "before",
    ["", " ", '"', "=", "Bearer ", "token_", "key-", "\n", "\\n", "\\u000a", "%20", "%3D"],
)
def test_a_real_token_is_masked_after_anything_a_token_can_follow(plain, before):
    for token in (USER_TOKEN, PLAYER_TOKEN, USER_TOKEN.rstrip("=")):
        assert plain.text(f"{before}{token}") == f"{before}{SOFTMAX_TOKEN_MASK}"
    assert plain.text(f"{before}{USER_TOKEN}, next") == f"{before}{SOFTMAX_TOKEN_MASK}, next"


@pytest.mark.parametrize(
    "word",
    [
        "apply_the_new_policy_to_every_coworld_in_the_league_now",
        "apply_" + "x" * 43,
        "usr_" + "x" * 42,
        "usr_" + "x" * 44,
        "ply_" + "x" * 60,
        PLAYER_ID,
    ],
)
def test_a_word_that_only_looks_like_a_token_stays(plain, word):
    assert plain.text(word) == word


@pytest.mark.parametrize("prefix", SOFTMAX_TOKEN_PREFIXES)
def test_every_credential_prefix_is_masked(plain, prefix):
    token = fake_token(prefix)
    assert plain.text(f"Bearer {token}") == f"Bearer {SOFTMAX_TOKEN_MASK}"


def test_a_player_credential_inside_tool_output_json_is_masked(plain):
    """Seen live: the workspace tool's output, nested as escaped JSON in a captured tool part."""
    credential = fake_token("cga_")
    output = json.dumps({"workspace_id": "w", "credential": credential})
    tool_output = f"{output}\ntokens:\n  x: {credential}"
    captured = json.dumps({"content": json.dumps({"output": tool_output})})
    assert credential.rstrip("=") not in plain.text(captured)
    assert credential.rstrip("=") not in json.dumps(plain.value(json.loads(captured)))


def test_keys_that_redact_alike_keep_both_values():
    redactor = Redactor(scrub, "test")
    redacted = redactor.value({USER_TOKEN: "first result", PLAYER_TOKEN: "second result"})
    assert sorted(redacted.values()) == ["first result", "second result"]
    assert USER_TOKEN not in json.dumps(redacted)


def test_softmax_patterns_run_before_the_scanner_and_stay_out_of_it():
    seen = []

    def recording_scrub(text: str) -> str:
        seen.append(text)
        return scrub(text)

    redactor = Redactor(recording_scrub, "test")
    redacted = redactor.text(f"{USER_TOKEN} {HERMES_STYLE_SECRET}")
    assert USER_TOKEN not in seen[0]
    assert redacted == f"{SOFTMAX_TOKEN_MASK} sk-abc...2345"


def test_values_redact_every_string_and_keep_valid_json():
    value = {
        "nested": [{"a": f"KEY={USER_TOKEN} rest"}],
        "api_key": "opaque-value-with-no-shape",
        "count": 3,
        PLAYER_TOKEN: "as a key",
    }
    redactor = Redactor(scrub, "test", lambda key: key == "api_key")
    redacted = redactor.value(value)
    text = json.dumps(redacted, allow_nan=False)
    assert USER_TOKEN not in text and PLAYER_TOKEN not in text
    assert "opaque-value-with-no-shape" not in text
    assert redacted["count"] == 3
