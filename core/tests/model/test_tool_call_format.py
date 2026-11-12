"""Tool-call format against the models in use (design C.4, amendment A7.2).

Runs only with ``-m model`` and ``OPENROUTER_API_KEY``. For each role, each model its shipped
``config.yaml`` names (the primary and the fallbacks) and each of the role's tools, ask for exactly
one call from a one-line instruction and check that the returned arguments validate.
"""

import os
from pathlib import Path

import pytest
import yaml

from research_core.tools import catalog
from research_core.tools.base import validate_args
from tests.model import harness

REPO = Path(__file__).resolve().parents[3]


def shipped_models(role: str) -> list[str]:
    config = yaml.safe_load((REPO / role / "config.yaml").read_text())
    return [config["model"]["default"], *(f["model"] for f in config["fallback_providers"])]


# IDE_TOOL_CALL_MODELS (comma-separated) runs the Orchestrator's tools on other models instead.
CASES = (
    [("orchestrator", model) for model in os.environ["IDE_TOOL_CALL_MODELS"].split(",")]
    if os.environ.get("IDE_TOOL_CALL_MODELS")
    else [(role, m) for role in ("orchestrator", "researcher") for m in shipped_models(role)]
)
INSTRUCTIONS = {
    "goal": "Create a Goal titled 'Beat the house' with end state 'rank first' because I asked.",
    "task": "Read Task T-3.",
    "claim": "Search claims about aggression.",
    "experiment": "Read experiment X-2.",
    "version": "List the last 3 Versions.",
    "directive": "List the active directives.",
    "settings": "Read the setting max_researchers.",
    "chat_update": "From this background wake, tell the user that V-1 was submitted.",
    "workspace": "Show the environment guide.",
    "upload": "Upload Version V-2.",
    "submit": "Submit V-2 to league_x because the user asked in message m-1.",
    "xp": "Report the status of experiment X-2.",
    "platform": "List the leagues.",
    "forum_post": "Post 'Hello' with body 'First post' to the forum.",
    "wiki_edit": "Edit wiki w page p: title T, body B, base revision r1.",
    "softmax_api": "GET /whoami.",
    "github": "List my GitHub repositories.",
}
# A Researcher reads Goals; it cannot create one.
ROLE_INSTRUCTIONS = {"researcher": {"goal": "Read Goal G-1."}}

pytestmark = [
    pytest.mark.model,
    pytest.mark.skipif(not harness.KEY, reason="OPENROUTER_API_KEY is not set"),
]


def ask(model: str, schema: dict, instruction: str) -> dict:
    reply = harness.complete(
        model,
        [{"role": "user", "content": instruction}],
        [{"type": "function", "function": schema}],
        # One tool per request, so "required" forces it; Kimi K3.1 Pro's endpoint does not
        # support naming the function.
        tool_choice="required",
        # The OpenRouter hosts the shipped config pins this model to, as production uses.
        provider=harness.provider_for(model, harness.SHIPPED),
    )
    assert reply.calls, reply
    assert reply.calls[0].arguments is not None, reply.calls[0].raw
    return reply.calls[0].arguments


@pytest.mark.parametrize(("role", "model"), CASES)
def test_models_emit_valid_tool_calls(role, model):
    instructions = {**INSTRUCTIONS, **ROLE_INSTRUCTIONS.get(role, {})}
    passes, failures = 0, []
    for spec in catalog.tools_for(role):
        args = ask(model, spec.schema(role), instructions[spec.name])
        _, problems = validate_args(spec, role, args)
        if problems:
            failures.append((spec.name, args, problems))
        else:
            passes += 1
    print(f"{role} on {model}: {passes} valid, {len(failures)} invalid")
    assert not failures, failures
