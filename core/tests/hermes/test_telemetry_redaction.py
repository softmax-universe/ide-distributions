"""The telemetry redactor with Hermes's real scanner, in Hermes's interpreter.

``HERMES_PYTHON`` names that interpreter. ``IDE_HERMES_SRC`` can name a Hermes source tree to run
with the current Python instead; the scanner imports only the standard library.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.telemetry.test_redaction import PLAYER_ID, PLAYER_TOKEN, USER_TOKEN

CORE_SRC = Path(__file__).resolve().parents[2] / "src"

_PROBE = r"""
import json, sys
sys.path[:0] = [path for path in sys.argv[1:] if path]
from research_core.agent_telemetry import hermes_redactor
from agent.redact import redact_for_egress, redact_sensitive_text

cases = json.load(sys.stdin)
redactor = hermes_redactor()
print(json.dumps({
    "version": redactor.version,
    "redacted": [redactor.text(case) for case in cases],
    "walked": redactor.value({"api_key": "opaque-canary-field-value", "x": [cases[0]]}),
    # What the agent sees is unchanged, so no pattern reached Hermes's own lists.
    "hermes_alone": [redact_sensitive_text(case, force=True) for case in cases],
}))
"""


OPENAI_STYLE = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
GITHUB_STYLE = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
OPAQUE = "abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture(scope="module")
def probe():
    hermes_python = os.environ.get("HERMES_PYTHON")
    hermes_src = os.environ.get("IDE_HERMES_SRC", "")
    if not hermes_python and not hermes_src:
        pytest.skip("set HERMES_PYTHON or IDE_HERMES_SRC to run against Hermes's scanner")

    def run(cases: list[str]) -> dict:
        result = subprocess.run(
            [hermes_python or sys.executable, "-c", _PROBE, str(CORE_SRC), hermes_src],
            input=json.dumps(cases),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return run


@pytest.mark.parametrize("token", [USER_TOKEN, PLAYER_TOKEN], ids=["user", "player"])
def test_softmax_tokens_are_gone_in_every_setting(probe, token):
    cases = [
        token,
        f"before {token} after",
        f"export SOFTMAX_TOKEN={token}",
        f'{{"credential": "{token}"}}',
        f"Authorization: Bearer {token}",
        f"line one\\n{token}",
        f"https://softmax.com/x?token={token}&a=1",
    ]
    output = probe(cases)
    for text in output["redacted"]:
        assert token not in text
        assert token[4:-1] not in text


def test_softmax_patterns_stay_out_of_hermes(probe):
    """Hermes alone leaves a bare token, which is why the writer adds its own patterns."""
    output = probe([USER_TOKEN, PLAYER_TOKEN])
    assert output["hermes_alone"] == [USER_TOKEN, PLAYER_TOKEN]


def test_hermes_formats_and_field_names_are_caught(probe):
    cases = [
        f"key {OPENAI_STYLE} here",
        f"token {GITHUB_STYLE} here",
        f"Authorization: Bearer {OPAQUE}",
        f'{{"api_key": "{OPAQUE}"}}',
    ]
    output = probe(cases)
    assert OPENAI_STYLE not in output["redacted"][0]
    assert GITHUB_STYLE not in output["redacted"][1]
    assert OPAQUE not in output["redacted"][2]
    assert OPAQUE not in output["redacted"][3]
    assert "opaque-canary-field-value" not in json.dumps(output["walked"])


def test_ordinary_text_and_ids_pass_through_unchanged(probe):
    cases = [PLAYER_ID, "The results of the léague_abc run: 3.5 of 4 — 日本語.\n"]
    assert probe(cases)["redacted"] == cases


def test_an_image_survives_and_ids_are_left_alone(probe):
    """Hermes alone damages this image: its JWT pattern matches ``eyJ`` inside the base64."""
    image = "data:image/png;base64," + base64.b64encode(os.urandom(300)).decode()
    image = image.replace(",", ",AAAAeyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9AAAA", 1)
    image += "A" * (-len(image.split(",")[1]) % 4)
    ids = [
        "ses_01JC9W",
        "functions.read_file:0",
        "call_00_abcdefghijklmnopqrstuvwxyz012345",
        "chatcmpl-abc123def456ghi789",
        "0199014d-55f8-7b70-a8eb-000000000002",
        "moonshot/kimi-k3.1-pro",
    ]
    output = probe([image, *ids])
    assert output["hermes_alone"][0] != image
    assert output["redacted"] == [image, *ids]


def test_the_version_names_the_rules(probe):
    version = probe(["x"])["version"]
    assert version.startswith("hermes-") and version.endswith("+softmax-1")
    assert " " not in version
