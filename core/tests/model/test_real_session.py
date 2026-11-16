"""Tool calls through a real Session on a real Hermes gateway (``real_session.py``).

Costs real money, so it runs only on request: ``-m model``, ``OPENROUTER_API_KEY`` and
``HERMES_BIN``. ``in_image.sh`` runs it in the pinned Hermes image (README, "Develop and test").

Environment, all optional:
  IDE_TOOL_CALL_MODELS   the one model every profile runs (default Kimi K3.1 Pro)
  IDE_TOOL_CALL_CASES    comma-separated substrings of case ids
  IDE_TOOL_CALL_REPEATS  runs per case (default 10)
  IDE_TOOL_CALL_PARALLEL runs at a time (default 4)
  IDE_TOOL_CALL_CONTEXT  a path: also write the captured real context there, for the direct
                         tests (``test_write_actions.py``) to send
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from research_core.tools import catalog
from tests.model import harness
from tests.model.real_session import CASES, HOST_LABEL, RealGateway, hermes_bin_from_env
from tests.model.write_actions import RECOVERY_DEPTHS, Case

pytestmark = [
    pytest.mark.model,
    pytest.mark.skipif(not harness.KEY, reason="OPENROUTER_API_KEY is not set"),
    pytest.mark.skipif(not hermes_bin_from_env(), reason="HERMES_BIN is not set"),
]
DEFAULT_MODEL = "moonshotai/kimi-k3.1-pro"
DEFAULT_REPEATS = 10
# Measured on Kimi K3.1 Pro on 2026-11-04, 10 repeats: first try 120/120, after one refusal 40/40,
# after three 39/40. Before #180 removed operation_id, task.assign was 1/10 and this check failed.
# The check is the plain reading of the rate: the share of valid calls must be at least this. At
# the default repeats that asks for 114/120 on the first try and 38/40 per recovery depth. Chance
# that all three rows pass, by the model's true rate (exact binomial):
#                                               99%   97%   92%
#   share at least 95%, 10 repeats (this check)  98%   72%    2%
#   share at least 95%, 5 repeats                96%   69%    8%
#   Wilson top end at least 95%, 10 repeats     100%   99%   39%  (passes 110/120, 36/40)
#   Wilson low end at least 95%                 no run of this size can pass
AGREED_RATE = 0.95
MIN_SAMPLE = 20  # fewer judged calls than this say too little to decide on
MODEL = (harness.env_list("IDE_TOOL_CALL_MODELS") or [DEFAULT_MODEL])[0]


def selected() -> list[Case]:
    wanted = harness.env_list("IDE_TOOL_CALL_CASES")
    cases = [c for c in CASES if not wanted or any(w in c.id for w in wanted)]
    repeats = int(os.environ.get("IDE_TOOL_CALL_REPEATS") or DEFAULT_REPEATS)
    return [case for case in cases for _ in range(repeats)]


@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    real = RealGateway(
        root=tmp_path_factory.mktemp("model-gateway"),
        hermes_bin=hermes_bin_from_env(),
        openrouter_key=harness.KEY,
        model=MODEL,
    )
    try:
        real.start()
        harness.RESULTS.schemas = "shipped, through a real Session"
        print(f"\ngateway log: {real.host.log}\nworkspace: {real.host.workspace}")
        yield real
    finally:
        real.stop()


@pytest.fixture(scope="module")
def captured(gateway) -> dict[str, harness.SessionContext]:
    """One real Session per role, run once, and what Hermes stored for it."""
    contexts = {}
    for role in catalog.ROLES:
        session_id = gateway.open_session(role)
        gateway.run_to_completion(role, session_id, "Reply with READY and stop.")
        contexts[role] = gateway.stored_prompt_and_tools(role, session_id)
    return contexts


def test_a_real_session_gets_the_built_prompt_and_tools(captured):
    """The real prompt carries what the stand-in leaves out; direct calls can send this capture."""
    for role, context in captured.items():
        assert "[briefing]" in context.system, role
        assert "## Order of authority" in context.system, role  # the SOUL
        assert "working-a-task" in context.system or "goals-and-tasks" in context.system, role
        names = {tool["function"]["name"] for tool in context.hermes_tools}
        assert "skill_view" in names, role
        assert not names & {spec.name for spec in catalog.tools_for(role)}, role
    path = os.environ.get("IDE_TOOL_CALL_CONTEXT")
    if path:
        Path(path).write_text(json.dumps({r: asdict(c) for r, c in captured.items()}))
    for role, context in captured.items():
        assert context.hermes_tools == harness.hermes_builtins(role), (
            f"{role}: a real Session's Hermes tools no longer match the snapshot "
            "(hermes_builtin_tools.json, hermes_orchestrator_tools.json); regenerate it "
            "(see harness.py)"
        )


def test_a_fresh_sessions_frozen_tools_show_no_drift(gateway, captured):
    """``tools/schema_drift.py`` against a real ``state.db``: a Session opened now agrees."""
    drift_tool = harness._load(
        Path(__file__).parents[3] / "tools", "schema_drift.py", "schema_drift"
    )
    for role in catalog.ROLES:
        frozen_lists = drift_tool.read_frozen_lists(gateway.state_db(role))
        assert frozen_lists, role
        for frozen in frozen_lists:
            assert [str(d) for d in drift_tool.compare(role, frozen.tools)] == [], role


@pytest.fixture(scope="module")
def calls(request, gateway):
    def run_one(case: Case) -> harness.Result:
        return gateway.run_case(MODEL, case)

    with harness.started_calls(
        request.session, test_write_action_through_a_real_session, run_one
    ) as futures:
        yield futures


@pytest.mark.parametrize("case", selected(), ids=lambda case: case.id)
def test_write_action_through_a_real_session(case: Case, calls, request):
    """Records the run. One run decides nothing: ``test_the_agreed_rate`` below does the judging,
    and the summary lists every call that was not valid."""
    result = calls[request.node.nodeid].result()
    harness.RESULTS.items.append(result)
    if result.outcome == "host_error":
        pytest.skip(f"{case.id}: {result.detail}")


@pytest.mark.parametrize("refusals", [0, *RECOVERY_DEPTHS])
def test_the_agreed_rate(refusals: int):
    """The parent task's done-when: the model tests pass at an agreed rate through a real session.

    Fails when the share of valid calls is below ``AGREED_RATE``, at each refusal depth, and when
    too few runs reached a verdict to decide. Only a run narrowed to some cases may skip it.
    """
    results = [
        r
        for r in harness.RESULTS.items
        if r.host == HOST_LABEL and harness.refusals_in(r.case) == refusals
    ]
    valid, total = harness.judged_rate(results)
    if total < MIN_SAMPLE and harness.env_list("IDE_TOOL_CALL_CASES"):
        pytest.skip(f"narrowed run: {total} judged calls, a decision needs {MIN_SAMPLE}")
    assert total >= MIN_SAMPLE, f"after {refusals} refusals: only {total} judged calls"
    assert total / len(results) >= AGREED_RATE, (
        f"after {refusals} refusals: {len(results) - total} of {len(results)} runs ended without a "
        "verdict (ERR in the table), too many to trust the rate"
    )
    assert valid / total >= AGREED_RATE, (
        f"after {refusals} refusals: {valid}/{total}, agreed rate {AGREED_RATE}"
    )
