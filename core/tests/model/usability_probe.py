"""Running one situation with the session's real prompt and tools, and judging what the model did.

A run asks until the model does something that settles the situation, at most ``MAX_REQUESTS``
requests. Every call it makes before that is answered as a session would answer it. A session runs
every call in a reply, so a write later in the reply that settled a run makes it ``wrong_act``:

- ``skill_view`` and ``skills_list`` return the real skill text or list, and count as opening;
- a preparation (a read, a terminal command, an edit: :func:`is_preparation`) gets the tool's own
  refusal when the argument check refuses it, else what ``usability_reads`` holds for it;
- a call named in ``situation.acts`` is judged by ``write_actions.judge``. Valid ends the run as
  ``right``; invalid gets the refusal and the model may try again, which is how recovery shows;
- ``situation.before`` names writes that may sensibly come first; they are answered and the run
  goes on;
- any other call that passes the checks and changes something ends the run as ``wrong_act``:
  production would have acted on it unless a store check not run here refuses it, as the last
  paragraph but one says. One the checks refuse changes nothing, so it gets the refusal and the run
  goes on;
- where ``situation.stop`` is set, any call but opening a skill ends the run as ``wrong_act``, even
  a read or a refused call.

Outcomes: ``right`` (also words alone when ``"text"`` is in ``situation.acts``), ``wrong_act``,
``text`` (answered in words where a call was wanted, with or without earlier calls, or with
nothing at all),
``limit`` (still preparing after ``MAX_REQUESTS``), ``rejected`` (the host refused the request),
``host_error`` (transport, 5xx, or account limit; not counted in any rate).

Those outcomes are the strict measure, the one baselines compare. The checks above do not hold
every store check, so ``wrong_act`` and ``right`` say what the model tried, not what would have
happened: a ``task.complete`` with no ``merge`` or ``result`` passes the argument check and ends the
run, though ``tasks.complete`` would refuse it and change nothing. Beside them,
``Run.wrong_attempts`` lists the unwanted writes the model tried, refused ones included, and
``Run.act_applied`` says whether the call that ended the run would have been applied
(:func:`_would_apply`). A refused act does not make a run right: production would have answered the
refusal, and what the model did next is not measured.

``usability_baseline.json`` was measured with ``usability_reads``; the first baseline was not, and
``docs/reports/tool-usability-baseline-2026-11-04.md`` marks what that did.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

from research_core.plugin.turn import render_context_block
from research_core.store import tasks
from research_core.task_types import load as load_task_type
from research_core.tools import catalog
from research_core.tools import task as task_tool
from research_core.tools._args import record_number
from research_core.tools.base import ToolError, coerce_args, error_payload
from research_core.watcher import dispatch
from research_core.watcher.wakes import TEMPLATES_DIR
from tests.model import harness, usability_context, usability_reads, write_actions
from tests.model.usability_situations import Situation

MAX_REQUESTS = 8
HOST_RETRIES = 3
TASK_ID = usability_context.TASK_ID
# Hermes's tools that change nothing the session cannot redo; an edit is how work starts, and
# models use execute_code to look around (every case seen on 2026-11-04 was a read).
PREPARATION_BUILTINS = frozenset(
    {
        "execute_code",
        "patch",
        "process_manage",
        "read_file",
        "search_files",
        "session_search",
        "terminal",
        "vision_analyze",
        "web_extract",
        "web_search",
        "write_file",
    }
)
# Beyond write_actions.READ_ACTIONS. A notice or a view informs and leaves the next step open.
PREPARATION_ACTIONS = frozenset({"workers", "command", "show", "notify", "repositories"})


def label_of(name: str, args: dict | None) -> str:
    """``tool.action`` for an IDE action tool, else the tool's name."""
    action = (args or {}).get("action")
    if isinstance(action, str) and name not in usability_context.builtin_names():
        return f"{name}.{action}"
    return name


def is_preparation(name: str, args: dict | None) -> bool:
    args = args or {}
    if name in PREPARATION_BUILTINS or name == "platform":
        return True
    if name == "softmax_api":
        return args.get("method") in ("GET", "HEAD")
    action = args.get("action")
    return action in write_actions.READ_ACTIONS or action in PREPARATION_ACTIONS


@dataclass
class Run:
    situation: str
    repeat: int
    outcome: str = "limit"
    calls: list[str] = field(default_factory=list)  # every call, in order
    skills: list[str] = field(default_factory=list)  # skill_view names, in order
    act: str = ""  # the call that ended the run
    act_valid: bool | None = None  # whether the first call naming an expected act was valid
    act_attempts: int = 0  # calls naming an expected act
    args_right: bool | None = None  # whether the right call holds what ``situation.args`` asks
    act_args: dict | None = None  # the arguments of the call that ended the run
    # :func:`_would_apply`; None too when a wanted read ended the run, or no call did.
    act_applied: bool | None = None
    wrong_attempts: list[str] = field(default_factory=list)  # unwanted writes, refused ones too
    problems: list[str] = field(default_factory=list)  # what the checks said, per refused call
    unknown_tools: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)  # Versions an allowed ``upload`` sent
    requests: int = 0
    first_prompt_tokens: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    provider: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def first_call(self) -> str:
        """The first call that is not opening a skill: the tool the model reached for."""
        return next((c for c in self.calls if not c.startswith("skill")), "")


# --- the conversation -------------------------------------------------------------------------


def _assistant_call(
    call_id: str, role: str, tool: str, args: dict, schemas: harness.SchemaSet
) -> dict:
    tool, args = schemas.outgoing(role, tool, args)
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args)},
            }
        ],
    }


def _wake_prompt(situation: Situation) -> str:
    template = (TEMPLATES_DIR / f"{situation.wake}.md").read_text(encoding="utf-8")
    payload = json.dumps(situation.payload, indent=2, sort_keys=True)
    for placeholder, value in (
        ("{event_kind}", situation.event),
        ("{subject}", situation.subject),
        ("{payload_json}", payload),
        ("{coworld_id}", usability_context.COWORLD),
    ):
        template = template.replace(placeholder, value)
    return template


def _task_prompt(situation: Situation) -> str:
    return dispatch.render(
        "task_start",
        coworld=usability_context.COWORLD,
        task_id=TASK_ID,
        title=situation.title,
        type=situation.task_type,
        priority="normal",
        goal=usability_reads.goal_line(situation.goal),
        end_condition="deliverable",
        experiment=None,
        depends_on=None,
        description=situation.text,
        reasoning="",
        worktree="worktrees/task-21",
        branch="task/21",
    )


def _claim_exchange(situation: Situation, schemas: harness.SchemaSet) -> list[dict]:
    """The ``task.claim`` already made, with the type's real instructions in its result."""
    result = usability_reads.claim_result(situation, load_task_type(situation.task_type).body)
    args = {"action": "claim", "task": TASK_ID}
    return [
        _assistant_call("call_claim", situation.role, "task", args, schemas),
        {"role": "tool", "tool_call_id": "call_claim", "content": json.dumps(result)},
    ]


def first_user_message(situation: Situation) -> str:
    if situation.wake:
        return _wake_prompt(situation)
    if situation.role == "researcher":
        return _task_prompt(situation)
    if situation.turn_context:
        # The turn hook adds the client's context block to the user's message.
        return f"{situation.text}\n\n{render_context_block(situation.turn_context)}"
    return situation.text


def session_kind(situation: Situation) -> str:
    if situation.role == "researcher":
        return "task"
    return "wake" if situation.wake else "chat"


def messages_for(situation: Situation, schemas: harness.SchemaSet) -> list[dict]:
    your_task = ""
    if situation.role == "researcher":
        your_task = (
            f'Your Task: {TASK_ID} "{situation.title}" — {situation.task_type} for '
            f"{situation.goal}, in-progress; read it with task.read before working\n"
        )
    system = usability_context.system_prompt(
        situation.role,
        your_task=your_task,
        declared=situation.declared,
        capabilities=situation.capabilities,
        has_coworld=situation.has_coworld,
        kind=session_kind(situation),
    )
    history = [
        {"role": "system", "content": system},
        {"role": "user", "content": first_user_message(situation)},
    ]
    if situation.role == "researcher" and situation.claimed:
        history += _claim_exchange(situation, schemas)
    for number, prior in enumerate(situation.prior):
        call_id = f"call_prior_{number}"
        history += [
            _assistant_call(call_id, situation.role, prior.tool, prior.args, schemas),
            {"role": "tool", "tool_call_id": call_id, "content": prior.result},
        ]
    if situation.after_prior:
        history.append({"role": "user", "content": situation.after_prior})
    return history


# --- one call ---------------------------------------------------------------------------------


def _problems(situation: Situation, call: harness.Call) -> str:
    """Why production's argument and record checks refuse this call; empty when they pass."""
    args = call.arguments or {}
    if call.name == "delegate_task":
        # Hermes's own tool: only a spawn with tasks asks anyone for anything.
        spawns = args.get("tasks") and args.get("action") in (None, "spawn")
        return "" if spawns else f"delegate_task did not spawn a task; sent {sorted(args)}"
    case = write_actions.Case(
        situation.role, call.name, str(args.get("action") or ""), situation.text
    )
    outcome, detail = write_actions.judge(case, call, args)
    return "" if outcome == "valid" else detail


def _answer(
    situation: Situation,
    run: Run,
    call: harness.Call,
    schemas: harness.SchemaSet,
    problems: str = "",
) -> str:
    if call.name == "skill_view":
        return usability_context.skill_view_result(
            situation.role, str((call.arguments or {}).get("name"))
        )
    if call.name == "skills_list":
        return usability_context.skills_list_result(situation.role)
    args = call.arguments
    case = write_actions.Case(situation.role, call.name, "", situation.text)
    answer = write_actions.answer(case, schemas, call.name, args)
    if answer != write_actions.RESULT:
        return answer  # the tool's argument check refused it
    if problems:  # only a store check refused it, which production reports the same way
        return json.dumps(error_payload([problems], {"tool": call.name}))
    return usability_reads.result(situation, label_of(call.name, args), args, run.uploaded)


def args_hold(wanted: dict[str, str | None], args: dict) -> bool:
    """``wanted`` maps an argument to a regex searched in its JSON, or None for must be absent. A
    name ending in ``?`` may also be absent, for an argument whose default is right."""
    for name, pattern in wanted.items():
        value = args.get(name.removesuffix("?"))
        if name.endswith("?") and value is None:
            continue
        if pattern is None:
            if value not in (None, "", []):
                return False
        elif value is None or not re.search(pattern, json.dumps(value), re.DOTALL):
            return False
    return True


def _is_ide_tool(situation: Situation, name: str) -> bool:
    return any(spec.name == name for spec in catalog.tools_for(situation.role))


class _OwnTaskRecords(write_actions.EveryRecordExists):
    """The stand-in store for a completion: this Task also proposed every Task its checklist
    names, for its own Goal."""

    def __init__(self, row: dict):
        self.row = row

    def one(self, *_: object) -> dict:
        return {"id": 1, "proposed_by": usability_reads.SESSION, "goal_id": self.row["goal_id"]}

    def q(self, sql: str, *_: object) -> list:
        if "FROM experiment_runs" in sql:
            return []
        return [{"session_id": usability_reads.SESSION.removeprefix("session:")}]


def _only_reads(name: str, args: dict | None) -> bool:
    """:func:`is_preparation` for a call not yet checked: a malformed action reads nothing."""
    action = (args or {}).get("action")
    return (action is None or isinstance(action, str)) and is_preparation(name, args)


def _task_complete_problems(situation: Situation, args: dict) -> list[str]:
    """What ``tasks.complete``'s own checks refuse in this call, on the situation's Task. As in
    ``write_actions.RECORD_CHECKS``, the records it names exist; so does a commit on the Task's
    branch, as there is no repository here. The holder check is not run. It is not a
    ``RECORD_CHECKS`` entry because a refusal there lets the run go on, which would change ``right``
    and ``wrong_act``."""
    own = int(TASK_ID.removeprefix("T-"))
    try:
        named = record_number(args.get("task"), "task", "task")
    except ToolError as error:
        return [str(error)]
    if named != own:
        return [f"T-{named} is not the Task this Session holds"]
    row = {**usability_reads.own_task(situation), "id": own}
    fields = task_tool._complete_fields(coerce_args(task_tool.SPEC, situation.role, args))
    if fields["outcome"] == "failed":
        return tasks._fail_problems(row, fields["failure"], fields["commit"])  # type: ignore[arg-type]
    store = _OwnTaskRecords(row)
    return tasks._completion_problems(store, row, **fields)[0]  # type: ignore[arg-type]


def _would_apply(situation: Situation, call: harness.Call, problems: str) -> bool | None:
    """Whether production would change something on the call that ended a run. False: a check
    refuses it, or it only reads. True: the store's checks for that action pass. None: no store
    check is run for that action here, only the argument check, so this run cannot say."""
    args = call.arguments or {}
    if problems or _only_reads(call.name, args):
        return False
    action = args.get("action")
    if not isinstance(action, str):
        return None
    if (call.name, action) == ("task", "complete"):
        return not _task_complete_problems(situation, args)
    return True if (call.name, action) in write_actions.RECORD_CHECKS else None


def _handle_call(
    situation: Situation, run: Run, call: harness.Call, schemas: harness.SchemaSet
) -> str | None:
    """Record one call. Returns what to answer it with, or None when it ends the run."""
    refused = ""
    if call.arguments is not None:
        refused = "; ".join(harness.own_tool_problems(situation.role, call.name, call.arguments))
        name, canonical = schemas.canonical(situation.role, call.name, call.arguments)
        call = harness.Call(call.id, name, canonical, call.raw)
    args = call.arguments
    label = label_of(call.name, args)
    run.calls.append(label)
    if call.name == "skill_view":
        run.skills.append(usability_context.bare_skill_name(str((args or {}).get("name"))))
        return _answer(situation, run, call, schemas)
    is_ide = _is_ide_tool(situation, call.name)
    problems = refused or (
        _problems(situation, call) if is_ide or call.name == "delegate_task" else ""
    )
    if situation.stop and call.name != "skills_list":
        if not _only_reads(call.name, args):
            run.wrong_attempts.append(label)
        run.outcome, run.act, run.act_args = "wrong_act", label, args
        run.act_applied = _would_apply(situation, call, problems)
        run.detail = call.raw[:300]
        return None
    if not is_ide and call.name not in usability_context.builtin_names():
        run.unknown_tools.append(call.name)
        return json.dumps({"error": f"Unknown tool: {call.name}"})
    if problems:
        run.problems.append(f"{label}: {problems}"[:400])
    # A call to the wanted tool with no action is a try at the wanted act, refused as such.
    no_action = call.arguments is None or not args.get("action")
    aimed = no_action and any(act.split(".")[0] == call.name for act in situation.acts)
    if label in situation.acts or (is_ide and aimed):
        run.act_attempts += 1
        if run.act_valid is None:
            run.act_valid = not problems
        if not problems:
            if run.outcome == "right":  # a second valid act in the reply: the first one is judged
                return None
            run.outcome, run.act, run.act_args = "right", label, args
            # A wanted read has nothing to apply, so False here can only mean refused.
            if not _only_reads(call.name, args):
                run.act_applied = _would_apply(situation, call, problems)
            if situation.args is not None:
                run.args_right = args_hold(situation.args, args or {})
            return None
        return _answer(situation, run, call, schemas, problems)
    prepares = _only_reads(call.name, args) or call.name == "skills_list"
    if not prepares and label not in situation.before:
        run.wrong_attempts.append(label)
    if problems or prepares:
        return _answer(situation, run, call, schemas, problems)
    if label in situation.before:
        if label == "upload":
            run.uploaded.append(usability_reads.version_ref(args))
        return _answer(situation, run, call, schemas)
    run.outcome, run.act, run.act_args = "wrong_act", label, args
    run.act_applied = _would_apply(situation, call, problems)
    run.detail = call.raw[:300]
    return None


# --- one run ----------------------------------------------------------------------------------


def _complete_with_retries(
    model: str, history: list[dict], tools: list[dict], provider: dict | None
) -> harness.Reply:
    """A transient host error costs the run nothing but time; a refusal is not retried."""
    for attempt in range(HOST_RETRIES):
        try:
            return harness.complete(model, history, tools, tool_choice="auto", provider=provider)
        except harness.HostError as error:
            if error.rejected or attempt == HOST_RETRIES - 1:
                raise
            time.sleep(15 * (attempt + 1))
    raise AssertionError("unreachable")


def _count(run: Run, reply: harness.Reply) -> None:
    run.requests += 1
    run.first_prompt_tokens = run.first_prompt_tokens or reply.prompt_tokens
    run.prompt_tokens += reply.prompt_tokens
    run.cached_tokens += reply.cached_tokens
    run.completion_tokens += reply.completion_tokens
    run.cost += reply.cost
    run.provider = reply.provider


def run_situation(
    situation: Situation,
    repeat: int,
    model: str,
    host: str,
    schemas: harness.SchemaSet,
) -> Run:
    run = Run(situation.id, repeat)
    history = messages_for(situation, schemas)
    tools = usability_context.session_tools(situation.role, schemas, model)
    provider = harness.provider_for(model, host)
    for _ in range(MAX_REQUESTS):
        try:
            reply = _complete_with_retries(model, history, tools, provider)
        except harness.HostError as error:
            run.outcome = "rejected" if error.rejected else "host_error"
            run.detail = str(error)[:300]
            return run
        _count(run, reply)
        if not reply.calls:
            run.outcome = "right" if "text" in situation.acts and reply.text.strip() else "text"
            run.detail = reply.text[:300]
            return run
        answers = []
        for call in reply.calls:
            content = _handle_call(situation, run, call, schemas)
            if run.outcome == "wrong_act":
                return run
            if content is not None:
                answers.append({"role": "tool", "tool_call_id": call.id, "content": content})
        if run.outcome == "right":
            return run
        history = [*history, reply.message, *answers]
    return run
