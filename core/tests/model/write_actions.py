"""The write-action cases and how one is run and judged (``test_write_actions.py`` runs them).

Each case offers the role's whole tool list as a Hermes session does (``harness.full_toolset``,
``tool_choice: "auto"``) with a one-line instruction, and judges the model's first call to the
expected tool: a call to that tool with no ``action``, or with any action that is not a read. A
call to an action's own tool counts as the grouped tool and action (``ShippedSchemas.canonical``).
That call must pass the checks production runs before anything changes: ``validate_args`` (the
tools' argument check) and, for the actions whose store checks need no records, those checks
too (``RECORD_CHECKS``). Calls before it get the answer production would give: the tool's own
refusal when the argument check refuses them, otherwise a plausible result (``RESULT``, or the
case's ``read_result``). A model that has not called the tool within ``MAX_REQUESTS`` requests
(``MAX_REQUESTS_REAL_CONTEXT`` with a captured context) fails the case. A recovery case starts
from a history holding one or three refused calls: the first answered with the refusal the tool
returns today, a repeat with the shorter one the repeat hook puts in its place, and is sent as the
plugin's request middleware sends it, with only the latest of identical refused calls. Measured on
2026-11-04, before #180, with a real context and no such middleware, the rate was 60/60 after one
refusal and 22/60 after three, so both depths are measured.

Which write actions are covered. One case per write action, of the record tools (claim, goal,
task, experiment, directive, settings), that has at least two required fields, so the model
has to compose arguments rather than pass one reference. Left out, to keep a run small:
  - actions with the same shape as a covered one: task.reject for the Orchestrator (task.answer),
    task.link (two references), and the Researcher's claim.retire and experiment.abandon (covered
    for the Orchestrator);
  - workspace.change_complete, which needs a worktree the instruction cannot make plausible;
  - the tools without actions (submit, upload, wiki_edit, softmax_api, chat_update): their schemas
    mark required fields as required at the top level, so a provider can enforce them, which the
    action tools cannot;
  - the other tools (version, xp, platform, forum_post, github): ``test_tool_call_format.py``
    sends each one instruction;
  - actions whose only required field is one reference (task.approve, experiment.start, ...).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from research_core.plugin.repeats import drop_repeated_refusals, firmer_refusal
from research_core.store import claims, experiments, goals, settings, tasks
from research_core.tools import catalog
from research_core.tools.base import coerce_args, validate_args
from tests.model import harness

# Models and hosts of the standard measurement: two hosts each, every column a distinct host.
# ``shipped`` is the Kimi models' shipped pin, Moonshot's own endpoint; it lands on Moonshot's
# standard endpoint (its cost per token is half of ``moonshotai/highspeed``'s). Wafer served Kimi
# K3 in the 2026-11-02 incident. GLM 5.3 and MiniMax M3 ship no pin, so each runs on its maker's
# endpoint and one third-party host.
MEASUREMENT: dict[str, list[str]] = {
    "moonshotai/kimi-k3.1-pro": [harness.SHIPPED, "moonshotai/highspeed"],
    "moonshotai/kimi-k3": [harness.SHIPPED, "wafer"],
    "z-ai/glm-5.3": ["z-ai", "wafer"],
    "minimax/minimax-m3": ["minimax", "deepinfra"],
}
MAX_REQUESTS = 3
# The real prompt tells a model to read the relevant skill first, which the stand-in does not.
MAX_REQUESTS_REAL_CONTEXT = 8
# Actions that only read: a call to the expected tool with one of these is a look before the
# write, answered and not judged.
READ_ACTIONS = frozenset(
    {"read", "list", "search", "inspect_evidence", "status", "doctor", "capabilities"}
    | {"environment_guide", "workers", "command"}
)
# The answer to a call production would accept (reads, mostly): it confirms rather than
# contradicts the instruction, so a cautious model is not sent looking for missing records.
RESULT = json.dumps({"ok": True, "result": "As the request describes; nothing further to add."})
SYSTEM = (
    "You are the {role} agent of a Softmax research workspace. Act on requests with your "
    "tools; the user is not watching and will not answer questions. Every record a request "
    "names exists and is as the request describes."
)


@dataclass(frozen=True)
class Case:
    role: str
    tool: str
    action: str
    instruction: str
    # Recovery: the arguments of an earlier call that the tool refused, ``refusals`` times. The
    # history holds those calls and their refusals, and the model's next call is judged.
    refused: dict | None = None
    # The answer to reads before the judged call, for a case whose write needs what they return.
    read_result: str = RESULT
    refusals: int = 1
    # Which plain request this is, for a case with nothing forced (``real_session.ORDINARY_CASES``).
    ordinary: str = ""

    @property
    def id(self) -> str:
        suffix = f"-recovery{self.refusals}" if self.refused is not None else ""
        suffix = f"-ordinary-{self.ordinary}" if self.ordinary else suffix
        return f"{self.role}-{self.tool}.{self.action}{suffix}"

    @property
    def label(self) -> str:
        suffix = f" (after {self.refusals} refused)" if self.refused is not None else ""
        suffix = f" (ordinary {self.ordinary})" if self.ordinary else suffix
        return f"{self.role} {self.tool}.{self.action}{suffix}"


CHAMPION_DOC = "https://softmax.com/leagues/L-7 (retrieved 2026-11-02): champion pv-42"
# A claim about a new champion: what the incident's wakes failed to record.
CHAMPION_CLAIM = (
    "Record a claim that the new champion of league L-7 is policy version pv-42 (it took over "
    f"from pv-39 at rank 1); tag it champion and L-7, and cite the league page (type doc, ref "
    f"'{CHAMPION_DOC}') as supporting evidence."
)
EXPERIMENT_CREATE = (
    "Create an experiment testing whether V-12's faster scouting beats V-11: hypothesis "
    "'earlier scouting raises the win rate', if true adopt V-12, if false keep V-11, motivated "
    "by T-4's replays; methods: baseline V-11, candidate V-12, 40 xp episodes each, primary "
    "metric win_rate (up), decide on a 3-point gain with n_min 40."
)
TASK_CREATE = (
    "Create a research Task for Goal G-1 titled 'Why do we lose early fights?': study 20 "
    "replays of lost games and report the three most common causes."
)
# What task.read and task.workers return before an assignment, as far as an assignment uses it.
ASSIGN_READS = json.dumps(
    {
        "ok": True,
        "record": {"id": "T-5", "status": "accepted", "updated": "2026-11-04T18:20:11.123Z"},
        "workers": [{"profile": "researcher", "capabilities": ["replays", "xp"], "slots": 1}],
    }
)
ORCHESTRATOR = [
    Case("orchestrator", "claim", "write", CHAMPION_CLAIM),
    Case(
        "orchestrator",
        "claim",
        "add_evidence",
        "Add experiment X-3 to claim C-5 as evidence that contradicts it.",
    ),
    Case("orchestrator", "claim", "retire", "Retire claim C-2: it is refuted by X-3."),
    Case(
        "orchestrator",
        "goal",
        "create",
        "Create a Goal 'Reach the top 3 of league L-7': done when our policy ranks in the top "
        "3; the user asked for it in message m-12.",
    ),
    Case("orchestrator", "goal", "set_status", "Park Goal G-2 because league L-4 closed."),
    Case("orchestrator", "task", "create", TASK_CREATE),
    Case(
        "orchestrator",
        "task",
        "review",
        "Accept Task T-8's result and merge its commit: the tests pass and the win rate rose.",
    ),
    Case(
        "orchestrator",
        "task",
        "answer",
        "Answer the question on Task T-6: yes, use league L-7's practice division.",
    ),
    Case(
        "orchestrator",
        "task",
        "abandon",
        "Abandon Task T-9: its xp requests failed three times on missing credits; next step is "
        "to retry after the credit refill.",
    ),
    Case(
        "orchestrator",
        "task",
        "message",
        "Tell the Researcher on Task T-6 to use league L-7's practice division from now on.",
    ),
    Case(
        "orchestrator",
        "task",
        "assign",
        "Assign Task T-5 to the researcher worker. It should start from the T-3 replays. It "
        "fits because it has replay experience and T-5's dependencies are done.",
        read_result=ASSIGN_READS,
    ),
    Case("orchestrator", "experiment", "create", EXPERIMENT_CREATE),
    Case(
        "orchestrator",
        "experiment",
        "abandon",
        "Abandon experiment X-6: its baseline Version was deleted.",
    ),
    Case(
        "orchestrator",
        "directive",
        "add",
        "Save this standing instruction from the user's message m-31: 'never submit without "
        "asking me first'.",
    ),
    Case(
        "orchestrator",
        "settings",
        "update",
        "Set max_researchers to 2, as the user asked in message m-40.",
    ),
]
RESEARCHER = [
    Case("researcher", "claim", "write", CHAMPION_CLAIM),
    Case(
        "researcher",
        "claim",
        "add_evidence",
        "Add replay evidence (ref 'episode:ep-77 xp:none') to claim C-5, supporting it.",
    ),
    Case("researcher", "experiment", "create", EXPERIMENT_CREATE),
    Case(
        "researcher",
        "task",
        "propose",
        "Propose an improvement Task for Goal G-1 titled 'Cache path finding': memoize the "
        "path planner and measure the step time.",
    ),
    Case(
        "researcher",
        "task",
        "wait",
        "Mark Task T-5 as waiting for xp request req-1c0 to finish.",
    ),
    Case(
        "researcher",
        "task",
        "ask",
        "Ask the Orchestrator on Task T-5 whether to use the practice or ranked division.",
    ),
    Case(
        "researcher",
        "task",
        "release",
        "Release Task T-5: it needs GPU credits this workspace does not have.",
    ),
    Case(
        "researcher",
        "task",
        "propose_submission",
        "Propose Version V-14 of Task T-5 for submission: it won 31 of 40 xp episodes.",
    ),
]
# The incident's refused claim write: fields of other claim actions in place of summary and tags.
INCIDENT_CLAIM_ARGS = {
    "action": "write",
    "body": "pv-42 replaced pv-39 as the champion of league L-7.",
    "evidence": [{"type": "doc", "ref": CHAMPION_DOC, "stance": "supports"}],
    "see_also": [],
    "stance": "supports",
    "status": "superseded",
    "superseded_by": "pv-42",
}
TASK_MESSAGE = "Tell the Researcher on Task T-6: use league L-7's practice division."
RECOVERY_DEPTHS = (1, 3)
RECOVERY = [
    Case(role, tool, action, instruction, refused=refused, refusals=refusals)
    for role, tool, action, instruction, refused in (
        ("orchestrator", "claim", "write", CHAMPION_CLAIM, INCIDENT_CLAIM_ARGS),
        ("researcher", "claim", "write", CHAMPION_CLAIM, INCIDENT_CLAIM_ARGS),
        (
            "orchestrator",
            "task",
            "create",
            TASK_CREATE,
            {"action": "create", "title": "Why do we lose early fights?", "goal": "G-1"},
        ),
        # The Nov 4 chat's call: refused for a field it left out, again and again.
        ("orchestrator", "task", "message", TASK_MESSAGE, {"action": "message", "task": "T-6"}),
    )
    for refusals in RECOVERY_DEPTHS
]
CASES = [*ORCHESTRATOR, *RESEARCHER, *RECOVERY]


# --- the record checks ------------------------------------------------------------------------


class EveryRecordExists:
    """A stand-in store for the record checks: every record a call names exists (as the system
    prompt promises) and no claim cites another, so only the arguments' own shape is checked."""

    def one(self, *_: Any) -> dict:
        return {"id": 1}

    def q(self, *_: Any) -> list:
        return []


def _claim_write(args: dict, problems: list[str]) -> None:
    store = EveryRecordExists()
    claims._check_summary(args.get("summary"), problems)
    claims._check_body(args.get("body"), problems)
    claims._check_tags(args.get("tags"), problems)
    claims._check_evidence(store, args.get("evidence"), problems)  # type: ignore[arg-type]
    claims._check_see_also(store, args.get("see_also") or [], problems)  # type: ignore[arg-type]


def _claim_add_evidence(args: dict, problems: list[str]) -> None:
    link = {name: args.get(name) for name in ("type", "ref", "stance")}
    link["note"] = args.get("note") or ""
    claims._check_evidence(EveryRecordExists(), [link], problems, label="")  # type: ignore[arg-type]


def _experiment_create(args: dict, problems: list[str]) -> None:
    experiments._check_summary(args.get("summary"), problems)
    experiments._check_hypothesis(args.get("hypothesis"), problems)
    experiments._check_methods(EveryRecordExists(), args.get("methods"), problems)  # type: ignore[arg-type]
    experiments._check_tags(args.get("tags", []), problems)


def _task_abandon(args: dict, problems: list[str]) -> None:
    problems.extend(tasks._failure_problems(args.get("failure")))


def _task_create(args: dict, problems: list[str]) -> None:
    if args.get("goal") is None and args.get("type") != "maintenance":
        problems.append("goal is required unless type is maintenance")
    problems.extend(
        tasks._end_condition_problems(args.get("end_condition"), args.get("end_detail"))
    )


def _goal_create(args: dict, problems: list[str]) -> None:
    fields = {name: args.get(name) for name in ("title", "end_state", "reason")}
    goals._check_fields(problems, {**fields, "tags": args.get("tags") or []})


def _settings_update(args: dict, problems: list[str]) -> None:
    found = settings.SETTINGS.get(args.get("key"))
    if found is None:
        problems.append(f"unknown setting {args.get('key')!r}")
    else:
        problems.extend(settings.validate_value(found, args.get("value")))


# The checks past validate_args that depend only on the arguments (lists of objects, objects, word
# limits, a field required in some cases), run with records assumed to exist. This is the actions
# that have such checks and that a model gets wrong; it is not every check a store makes.
RECORD_CHECKS = {
    ("claim", "write"): _claim_write,
    ("claim", "add_evidence"): _claim_add_evidence,
    ("experiment", "create"): _experiment_create,
    ("task", "abandon"): _task_abandon,
    ("task", "create"): _task_create,
    ("goal", "create"): _goal_create,
    ("settings", "update"): _settings_update,
}


# --- running and judging one case -------------------------------------------------------------


def system_prompt(role: str) -> str:
    captured = harness.session_context(role)
    return captured.system if captured is not None else SYSTEM.format(role=role)


def refused_history(case: Case, schemas: harness.SchemaSet) -> list[dict]:
    """The assistant calls and tool results of a recovery case's refused calls."""
    name, args = schemas.outgoing(case.role, case.tool, case.refused)
    refusal = schemas.refusal(case.role, case.tool, case.refused)
    parsed = json.loads(refusal)
    assert parsed["ok"] is False, refusal
    history: list[dict] = []
    for count in range(1, case.refusals + 1):
        call_id = f"call_refused_{count}"
        history += [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": (
                    refusal if count == 1 else firmer_refusal(case.role, name, parsed, count)
                ),
            },
        ]
    return history


def messages(case: Case, schemas: harness.SchemaSet) -> list[dict]:
    history = [
        {"role": "system", "content": system_prompt(case.role)},
        {"role": "user", "content": case.instruction},
    ]
    return history if case.refused is None else [*history, *refused_history(case, schemas)]


def _role_has_action(case: Case, action: str) -> bool:
    spec = next(s for s in catalog.tools_for(case.role) if s.name == case.tool)
    return any(known.name == action for known in spec.actions_for(case.role))


def is_judged(case: Case, tool: str, args: dict) -> bool:
    """A call to the expected tool counts unless it is a read that tool has, or a Researcher's
    first step of taking the Task: no action, an action it does not have, or a write action."""
    if tool != case.tool:
        return False
    action = args.get("action")
    if case.role == "researcher" and (tool, action) == ("task", "claim"):
        return False
    return not (action in READ_ACTIONS and _role_has_action(case, action))


def judge(case: Case, call: harness.Call, args: dict) -> tuple[str, str]:
    """The outcome of the judged call, and what production would refuse."""
    if call.arguments is None:
        return "invalid", f"arguments are not a JSON object: {call.raw[:200]}"
    refused = harness.own_tool_problems(case.role, call.name, call.arguments)
    if refused:
        return "invalid", f"{'; '.join(refused)}; sent {sorted(call.arguments)}"
    if args.get("action") not in (None, case.action):
        return "invalid", f"action {args['action']!r} instead of {case.action}; sent {sorted(args)}"
    spec = next(s for s in catalog.tools_for(case.role) if s.name == case.tool)
    _, problems = validate_args(spec, case.role, args)
    if problems:
        return "invalid", f"{'; '.join(problems)}; sent {sorted(args)}"
    check = RECORD_CHECKS.get((case.tool, case.action))
    if check is not None:
        check(coerce_args(spec, case.role, args), problems)  # production checks the coerced values
        if problems:
            # The structured fields show what went wrong; the prose ones only take up room.
            shown = {k: v for k, v in args.items() if k not in ("summary", "body", "description")}
            return "invalid", f"record check: {'; '.join(problems)}; sent {json.dumps(shown)[:250]}"
    return "valid", ""


def gaps_in_reasoning(case: Case, args: dict, reasoning: str) -> list[str]:
    """Fields of the case's action that the judged call's reasoning names and its arguments omit."""
    spec = next(s for s in catalog.tools_for(case.role) if s.name == case.tool)
    action = spec.action(case.action)
    accepted = spec.accepted(action)
    required = [*action.required, *spec.required]
    optional = [name for name in accepted if name not in required]
    return harness.reasoning_gaps(required, optional, args, reasoning)


def answer(case: Case, schemas: harness.SchemaSet, tool: str, args: dict | None) -> str:
    """What production returns for a call before the judged one: the tool's refusal when its
    argument check refuses the call, else the case's read result (Hermes's own tools always get
    RESULT)."""
    spec = next((s for s in catalog.tools_for(case.role) if s.name == tool), None)
    if spec is None:
        return RESULT
    if args is None:
        return json.dumps({"ok": False, "error": "arguments are not a JSON object"})
    _, problems = validate_args(spec, case.role, args)
    return schemas.refusal(case.role, tool, args) if problems else case.read_result


def run_case(model: str, host: str, case: Case, schemas: harness.SchemaSet) -> harness.Result:
    """Ask until the model calls the expected tool (see ``is_judged``), at most MAX_REQUESTS
    times; every other call is answered as production would and recorded as a detour."""
    result = harness.Result(model, host, case.label, "wrong_tool")
    history = messages(case, schemas)
    tools = harness.full_toolset(case.role, schemas, model)
    real_context = harness.session_context(case.role) is not None
    for _ in range(MAX_REQUESTS_REAL_CONTEXT if real_context else MAX_REQUESTS):
        try:
            reply = harness.complete(
                model,
                drop_repeated_refusals(history),  # as the plugin's request middleware does
                tools,
                tool_choice="auto",
                provider=harness.provider_for(model, host),
            )
        except harness.HostError as error:
            result.outcome = "rejected" if error.rejected else "host_error"
            result.detail = str(error)
            return result
        result.replies.append(reply)
        if not reply.calls:
            result.outcome = "stopped" if result.detours else "no_call"
            result.detail = f"text: {reply.text[:150]!r}"
            return result
        answers = []
        for call in reply.calls:
            tool, args = schemas.canonical(case.role, call.name, call.arguments or {})
            if is_judged(case, tool, args):
                result.outcome, result.detail = judge(case, call, args)
                result.reasoning_gaps = gaps_in_reasoning(case, args, reply.reasoning)
                return result
            result.detours.append(f"{call.name} {call.raw[:120]}")
            if call.arguments is not None and harness.own_tool_problems(
                case.role, call.name, call.arguments
            ):
                tool, args = call.name, call.arguments  # refused as the flat call it was
            content = answer(case, schemas, tool, None if call.arguments is None else args)
            answers.append({"role": "tool", "tool_call_id": call.id, "content": content})
        history = [*history, reply.message, *answers]
    result.detail = f"never called {case.tool}.{case.action}; called {result.detours}"
    return result
