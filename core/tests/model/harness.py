"""What the model tool-call tests share: the OpenRouter request, host pins, tool lists, results.

The direct tests call OpenRouter themselves, not through Hermes, so they measure what a model and
an OpenRouter host make of the tool schemas, not Hermes's own parsing of the reply
(``real_session.py`` goes through Hermes). The request is
shaped as the pinned Hermes (v0.22.6) shapes it on its OpenRouter provider profile: no
``temperature``
(``agent/transports/chat_completions.py`` ``_base_kwargs`` sends one only for a profile's fixed
temperature, and OpenRouter's has none), and ``reasoning: {enabled: true, effort: medium}`` for a
model whose OpenRouter catalog entry lists ``reasoning`` among its supported parameters when the
profile config sets no reasoning effort (``plugins/model-providers/openrouter/__init__.py``
``build_api_kwargs_extras``; the profiles set none).

Hosts. A host label is either ``shipped`` (the pin ``prepare.PROVIDER_ROUTING_MODELS`` gives the
model, or OpenRouter's default routing when it has none) or an OpenRouter provider slug or
endpoint tag (``wafer``, ``moonshotai/highspeed``), sent as ``provider: {only: [label]}``. Which
hosts serve a model: ``GET https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints``.

Tool lists. :class:`SchemaSet` supplies a role's IDE tool schemas and translates calls between
its tools and the catalog's, so a candidate schema (split write actions, per-action schemas) can
be measured by naming another set in ``IDE_TOOL_CALL_SCHEMAS=module:attribute`` without changing
the cases. :func:`full_toolset` builds what a session sends: the IDE tools plus Hermes's
built-in tools, sorted by name as Hermes's registry does, through Hermes's own schema sanitizer
(``tools/schema_sanitizer.py``) and, for Kimi models, its Moonshot sanitizer
(``agent/moonshot_schema.py``). Both are loaded from the Hermes source tree that
``IDE_HERMES_SRC`` names; without it the full-toolset tests fail rather than send schemas Hermes
would rewrite.

``hermes_builtin_tools.json`` is the Hermes tools of a real Session on the pinned image: the 14
tools Hermes froze for it, the same for both roles, captured with ``IDE_TOOL_CALL_CONTEXT``
(README, "Develop and test"). ``test_real_session.py`` fails when a real Session's list no longer
matches. To regenerate after an image change, run ``-k test_a_real_session`` with that variable,
then copy ``orchestrator.hermes_tools`` from the capture into this file.

Real context. ``IDE_TOOL_CALL_CONTEXT`` names a file ``test_real_session.py`` captured from real
Sessions: per role, the system prompt Hermes stored (SOUL, briefing, skill index, sections) and
the Hermes tools it froze. A direct call then sends those in place of the stand-in prompt and
the snapshot above; the IDE tools still come from the ``SchemaSet``, so a capture stays good for
measuring a candidate schema.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from research_core import prepare
from research_core.plugin.inference_hook import MAX_RESPONSE_TOKENS
from research_core.tools import catalog
from research_core.tools.base import ToolContext, run_tool, validate_args
from research_core.tools.catalog import Role

KEY = os.environ.get("OPENROUTER_API_KEY")
URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
SHIPPED = "shipped"
HERMES_BUILTIN_TOOLS = Path(__file__).with_name("hermes_builtin_tools.json")
# What Hermes's OpenRouter profile sends to a reasoning-capable model when the config sets no
# reasoning effort.
HERMES_DEFAULT_REASONING = {"enabled": True, "effort": "medium"}


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def provider_for(model: str, host: str) -> dict | None:
    """The request's ``provider`` object for a host label (None: OpenRouter's default routing)."""
    if host == SHIPPED:
        return prepare.PROVIDER_ROUTING_MODELS.get(model)
    return {"only": [host]}


# --- the request ------------------------------------------------------------------------------


class HostError(Exception):
    """No reply to judge. ``rejected``: the host refused the request itself (a 4xx such as a
    schema it cannot take), which counts against the model and host; otherwise a transport
    error, a 5xx, or an account limit (401, 402, 403, 408, 429), which does not."""

    def __init__(self, message: str, *, rejected: bool = False):
        super().__init__(message)
        self.rejected = rejected


# 4xx statuses that are about the account or the moment, not the request's content.
NOT_A_REJECTION = {401, 402, 403, 408, 429}


def _is_rejection(status: Any) -> bool:
    return isinstance(status, int) and 400 <= status < 500 and status not in NOT_A_REJECTION


@dataclass
class Call:
    id: str
    name: str
    arguments: dict | None  # None: the arguments were not a JSON object
    raw: str


@dataclass
class Reply:
    message: dict  # the assistant message as returned, to send back in a longer history
    calls: list[Call]
    text: str
    provider: str  # the host that served the request
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost: float
    seconds: float
    reasoning: str = ""


def reasoning_of(message: dict) -> str:
    """The model's reasoning text in an assistant message (the field name varies by host)."""
    return message.get("reasoning") or message.get("reasoning_content") or ""


@functools.cache
def reasoning_models() -> frozenset[str]:
    """Models whose OpenRouter catalog entry lists ``reasoning`` (Hermes's live-catalog test)."""
    with urllib.request.urlopen(MODELS_URL, timeout=60) as response:
        models = json.load(response)["data"]
    return frozenset(
        m["id"] for m in models if "reasoning" in (m.get("supported_parameters") or [])
    )


def request_body(
    model: str, messages: list[dict], tools: list[dict], *, tool_choice: str, provider: dict | None
) -> dict:
    return {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "max_tokens": MAX_RESPONSE_TOKENS,  # as the plugin's request middleware bounds it
        **({"reasoning": HERMES_DEFAULT_REASONING} if model in reasoning_models() else {}),
        **({"provider": provider} if provider else {}),
        "usage": {"include": True},
    }


def complete(
    model: str,
    messages: list[dict],
    tools: list[dict],
    *,
    tool_choice: str,
    provider: dict | None,
) -> Reply:
    body = request_body(model, messages, tools, tool_choice=tool_choice, provider=provider)
    request = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise HostError(
            f"HTTP {error.code}: {detail}", rejected=_is_rejection(error.code)
        ) from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise HostError(f"{type(error).__name__}: {error}") from None
    if "error" in payload or not payload.get("choices"):
        error = payload.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
        raise HostError(json.dumps(error or payload)[:300], rejected=_is_rejection(code))
    message = payload["choices"][0]["message"]
    usage = payload.get("usage") or {}
    return Reply(
        message=message,
        calls=[_call(c) for c in message.get("tool_calls") or []],
        text=message.get("content") or "",
        provider=payload.get("provider", "?"),
        prompt_tokens=usage.get("prompt_tokens", 0),
        cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
        completion_tokens=usage.get("completion_tokens", 0),
        cost=float(usage.get("cost") or 0.0),
        seconds=time.monotonic() - started,
        reasoning=reasoning_of(message),
    )


def _call(raw: dict) -> Call:
    text = raw["function"].get("arguments") or "{}"
    try:
        arguments = json.loads(text)
    except json.JSONDecodeError:
        arguments = None
    arguments = arguments if isinstance(arguments, dict) else None
    return Call(raw.get("id", ""), raw["function"]["name"], arguments, text)


# --- tool lists -------------------------------------------------------------------------------


def catalog_refusal(role: Role, tool: str, args: dict) -> str:
    """``run_tool``'s text for a call it refuses before touching the store or the workspace
    (the argument check), using a valid context without opening the workspace."""
    ctx = ToolContext.from_env(role, {})
    return run_tool(tool, args, ctx)


class SchemaSet(Protocol):
    label: str

    def tools(self, role: Role) -> list[dict]:
        """The role's IDE tools as OpenAI function bodies (name, description, parameters)."""

    def canonical(self, role: Role, name: str, args: dict) -> tuple[str, dict]:
        """The catalog tool and arguments (``action`` included) that a returned call stands for."""

    def outgoing(self, role: Role, tool: str, args: dict) -> tuple[str, dict]:
        """The reverse of ``canonical``: how this set names a catalog call (for a history)."""

    def refusal(self, role: Role, tool: str, args: dict) -> str:
        """The tool result this set's tools give a catalog call that the argument check refuses
        (a recovery case's history, and the answer to a refused call before the judged one)."""


def own_tool_problems(role: Role, name: str, args: dict) -> list[str]:
    """What an action's own tool refuses in a call: it is stricter than the grouped call the judging
    reads, which drops another action's empty fields and takes a field named action."""
    spec = catalog.TOOLS.get(name)
    if spec is None or spec.split_from is None:
        return []
    return validate_args(spec, role, args)[1]


class ShippedSchemas:
    """The schemas the profile plugin registers today (``ToolSpec.schema``)."""

    label = "shipped"

    def tools(self, role: Role) -> list[dict]:
        return [spec.schema(role) for spec in catalog.tools_for(role)]

    def canonical(self, role: Role, name: str, args: dict) -> tuple[str, dict]:
        """A call to an action's own tool reads as the grouped tool and action, for the judging.
        :func:`own_tool_problems` says what production's flat tool refuses in it."""
        spec = catalog.TOOLS.get(name)
        if spec is None or spec.split_from is None:
            return name, args
        tool, action = spec.split_from
        return tool, {**args, "action": action}

    def outgoing(self, role: Role, tool: str, args: dict) -> tuple[str, dict]:
        own_tool = catalog.TOOLS.get(f"{tool}_{args.get('action')}")
        if own_tool is None or own_tool.split_from is None:
            return tool, args
        return own_tool.name, {k: v for k, v in args.items() if k != "action"}

    def refusal(self, role: Role, tool: str, args: dict) -> str:
        return catalog_refusal(role, *self.outgoing(role, tool, args))


def schema_set() -> SchemaSet:
    reference = os.environ.get("IDE_TOOL_CALL_SCHEMAS")
    if not reference:
        return ShippedSchemas()
    module, _, attribute = reference.partition(":")
    return getattr(importlib.import_module(module), attribute)


def _load(source: Path, relative: str, name: str) -> ModuleType:
    path = source / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


@functools.cache
def hermes_sanitizers() -> tuple[ModuleType, ModuleType]:
    """Hermes's ``tools/schema_sanitizer.py`` and ``agent/moonshot_schema.py`` (stdlib only),
    from the source tree ``IDE_HERMES_SRC`` names."""
    source = os.environ.get("IDE_HERMES_SRC")
    if not source:
        raise RuntimeError(
            "IDE_HERMES_SRC is not set: full-toolset mode sends schemas through Hermes's own "
            "sanitizers; point it at the source of the pinned Hermes (/opt/hermes in the image)"
        )
    root = Path(source)
    return (
        _load(root, "tools/schema_sanitizer.py", "hermes_schema_sanitizer"),
        _load(root, "agent/moonshot_schema.py", "hermes_moonshot_schema"),
    )


@dataclass(frozen=True)
class SessionContext:
    """What a real Session of a role is given before its first message."""

    system: str  # the system prompt Hermes stored for it
    hermes_tools: list[dict]  # its frozen tool list without the IDE tools


@functools.cache
def _captured_contexts(path: str) -> dict[str, SessionContext]:
    return {
        role: SessionContext(**context)
        for role, context in json.loads(Path(path).read_text()).items()
    }


def session_context(role: Role) -> SessionContext | None:
    """The role's captured real context, when ``IDE_TOOL_CALL_CONTEXT`` names a capture."""
    path = os.environ.get("IDE_TOOL_CALL_CONTEXT")
    return _captured_contexts(path)[role] if path else None


def full_toolset(role: Role, schemas: SchemaSet, model: str) -> list[dict]:
    """Every tool a session of the role offers ``model``, as Hermes sends them: sorted by name,
    sanitized, and for a Kimi model rewritten for Moonshot's schema flavour."""
    sanitizer, moonshot = hermes_sanitizers()
    captured = session_context(role)
    if captured is not None:
        builtins = captured.hermes_tools
    else:
        builtins = json.loads(HERMES_BUILTIN_TOOLS.read_text())
    ide = [{"type": "function", "function": s} for s in schemas.tools(role)]
    tools = sanitizer.sanitize_tool_schemas(
        sorted([*builtins, *ide], key=lambda tool: tool["function"]["name"])
    )
    return moonshot.sanitize_moonshot_tools(tools) if moonshot.is_moonshot_model(model) else tools


# --- results ----------------------------------------------------------------------------------


@dataclass
class Result:
    model: str
    host: str
    case: str  # "<role> <tool>.<action>", with " (after N refused)" for a recovery case
    outcome: str  # one of OUTCOME_CELL
    detail: str = ""
    replies: list[Reply] = field(default_factory=list)  # one per request, in order
    detours: list[str] = field(default_factory=list)  # other calls made before the judged one
    # Fields the judged call's reasoning names but its arguments omit (see ``reasoning_gaps``).
    reasoning_gaps: list[str] = field(default_factory=list)
    # Outcomes of the later judged calls, after an invalid one in a forced turn or ordinary case.
    later_attempts: list[str] | None = None  # None: this run did not go on to measure them


def reasoning_gaps(
    required: Iterable[str], optional: Iterable[str], args: dict, text: str
) -> list[str]:
    """The fields ``text`` names, as whole words spelled as in the schema, that ``args`` omit.

    A model that reasons "add the summary" and sends no summary points at a schema or host
    problem, not at a model that misread the tool. The match is on words, so a prose field name
    ("note", "reason") can be a false alarm, which is why a required field is marked: that is the
    part to read first. It reports; it fails nothing.
    """

    def named_and_omitted(field_name: str) -> bool:
        return field_name not in args and bool(re.search(rf"\b{re.escape(field_name)}\b", text))

    return [
        *(f"{name} (required)" for name in required if named_and_omitted(name)),
        *(name for name in optional if named_and_omitted(name)),
    ]


@dataclass
class Results:
    schemas: str = ""
    items: list[Result] = field(default_factory=list)


RESULTS = Results()


@contextmanager
def started_calls(
    session: Any, test_function: Callable, run_one: Callable[..., Result]
) -> Iterator[dict[str, Future]]:
    """Start the model calls of every collected test of ``test_function`` at once, keyed by test
    id, ``IDE_TOOL_CALL_PARALLEL`` at a time (default 4): with repeats a run is hundreds of calls
    that spend most of their time waiting for the host. Each test then waits for its own. Calls
    not yet started are dropped on exit, so stopping pytest early stops the spending once the
    calls under way end."""
    pool = ThreadPoolExecutor(int(os.environ.get("IDE_TOOL_CALL_PARALLEL") or 4))
    try:
        yield {
            item.nodeid: pool.submit(run_one, **item.callspec.params)
            for item in session.items
            if getattr(item, "function", None) is test_function
        }
    finally:
        pool.shutdown(cancel_futures=True)


OUTCOME_CELL = {
    "valid": "ok",  # the judged call's arguments pass the tools' checks
    "invalid": "BAD",  # they do not, or it named another write action of the tool
    "wrong_tool": "TOOL",  # never called the expected tool within the request limit
    "no_call": "TEXT",  # answered in text at once
    "stopped": "STOP",  # answered in text after other calls
    "rejected": "REJ",  # the host refused the request (4xx)
    "host_error": "ERR",  # transport error, 5xx or account limit: not counted
}


def refusals_in(case_label: str) -> int:
    """How many refused calls a case's history starts with, from its label."""
    match = re.search(r"\(after (\d+) refused\)$", case_label)
    return int(match[1]) if match else 0


def _short(model: str) -> str:
    return model.rsplit("/", 1)[-1]


def _cell(results: list[Result]) -> str:
    if not results:
        return ""
    if len(results) == 1:
        result = results[0]
        detours = f"+{len(result.detours)}" if result.detours else ""
        return OUTCOME_CELL[result.outcome] + detours
    judged = [r for r in results if r.outcome != "host_error"]
    valid = sum(r.outcome == "valid" for r in judged)
    errors = len(results) - len(judged)
    return f"{valid}/{len(judged)}" + (f" +{errors}E" if errors else "")


def wilson_interval(successes: int, total: int) -> tuple[float, float]:
    """The 95% Wilson score interval of a rate. It stays sensible at small samples and at rates
    near 0 or 1, where the usual normal approximation claims a certainty the sample cannot give."""
    z = 1.96
    share = successes / total
    denominator = 1 + z**2 / total
    center = (share + z**2 / (2 * total)) / denominator
    margin = z * math.sqrt(share * (1 - share) / total + z**2 / (4 * total**2)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def judged_rate(results: list[Result]) -> tuple[int, int]:
    """(valid, judged): a transport error, 5xx or account limit says nothing about the call."""
    judged = [r for r in results if r.outcome != "host_error"]
    return sum(r.outcome == "valid" for r in judged), len(judged)


def _recovered_later(results: list[Result]) -> str:
    """Of the runs that go on (forced turns, ordinary cases) whose judged call was invalid, how many
    sent a valid one later in the turn. The other runs stop at that call and say nothing either way.
    """
    invalid = [r for r in results if r.outcome == "invalid" and r.later_attempts is not None]
    if not invalid:
        return "-"
    return f"{sum('valid' in r.later_attempts for r in invalid)}/{len(invalid)}"


def _rate(results: list[Result]) -> str:
    valid, total = judged_rate(results)
    if not total:
        return "-"
    low, high = wilson_interval(valid, total)
    return f"{valid}/{total} {100 * valid / total:.0f}% [{100 * low:.0f}-{100 * high:.0f}]"


def _hosts(results: list[Result]) -> str:
    served = sorted({x.provider for r in results for x in r.replies})
    return ",".join(served[:2]) + (f" +{len(served) - 2}" if len(served) > 2 else "")


def _first_prompt_tokens(results: list[Result]) -> str:
    """Prompt tokens of each case's first request: the tool list plus the case's history."""
    first = [r.replies[0].prompt_tokens for r in results if r.replies]
    return f"{sum(first) / len(first):,.0f}" if first else "-"


def _cached(results: list[Result]) -> str:
    replies = [x for r in results for x in r.replies]
    total = sum(x.prompt_tokens for x in replies)
    return f"{100 * sum(x.cached_tokens for x in replies) / total:.0f}%" if total else "-"


def _gap_counts(results: list[Result]) -> str:
    """Calls whose reasoning names an omitted field, and how many of those name a required one."""
    gaps = [r.reasoning_gaps for r in results if r.reasoning_gaps]
    required = sum(any(gap.endswith("(required)") for gap in found) for found in gaps)
    return f"{len(gaps)} ({required} required)"


def _cost(results: list[Result]) -> float:
    return sum(x.cost for r in results for x in r.replies)


def summary() -> list[str]:
    """The results as a table: one row per case, one column per model and host."""
    items = RESULTS.items
    if not items:
        return []
    columns = list(dict.fromkeys((r.model, r.host) for r in items))
    cases = list(dict.fromkeys(r.case for r in items))
    by_cell: dict[tuple, list[Result]] = {}
    for r in items:
        by_cell.setdefault((r.case, r.model, r.host), []).append(r)
    rows = [[case, *(_cell(by_cell.get((case, m, h), [])) for m, h in columns)] for case in cases]

    def footer(label: str, value) -> list[str]:
        return [label, *(value([r for r in items if (r.model, r.host) == c]) for c in columns)]

    def with_refusals(refusals: int) -> Callable[[list[Result]], str]:
        return lambda results: _rate([r for r in results if refusals_in(r.case) == refusals])

    rows += [
        ["-" * 8, *("" for _ in columns)],
        footer("served by", _hosts),
        footer("first-try valid", with_refusals(0)),
        footer("after 1 refusal", with_refusals(1)),
        footer("after 3 refusals", with_refusals(3)),
        footer("valid, all cases", _rate),
        footer("valid, no detour", lambda rs: _rate([r for r in rs if not r.detours])),
        footer("invalid, valid later", _recovered_later),
        footer("reasoning gaps", _gap_counts),
        footer("prompt tokens", _first_prompt_tokens),
        footer("cached share", _cached),
        footer("requests", lambda rs: str(sum(len(r.replies) for r in rs))),
        footer("cost $", lambda rs: f"{_cost(rs):.3f}"),
    ]
    table = [["case", *(f"{_short(m)}@{h}" for m, h in columns)], *rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    rendered = ["  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)) for row in table]
    lines = [
        f"Tool schemas: {RESULTS.schemas}. Each cell judges the first call to the expected tool "
        "(any call to the tool without an action, or with a write action). ok = it passes the "
        "tools' argument and record checks; BAD = it does not; TOOL = never within the request "
        "limit; TEXT = a text answer at once; STOP = a text answer after other calls; REJ = the "
        "host refused the request (4xx); ERR = transport error, 5xx or account limit, or through "
        "a real Session a time limit or a gateway that stopped (not counted). +N = N "
        "other calls first, answered as the tools would (refusal or plausible result). "
        "Invalid, valid later: invalid calls followed by a valid one; only a forced turn or an "
        "ordinary case goes on after one, so elsewhere it reads -. "
        "Prompt tokens: mean of each case's first request (through a real Session, of its "
        "requests: Hermes stores totals). With repeats a cell is valid/judged "
        "and a rate is valid/judged, percent and [95% Wilson interval]. Intervals that do not "
        "overlap show a real difference; overlapping ones decide nothing either way.",
        "",
        *rendered,
        "",
        f"Total cost ${_cost(items):.3f} over {sum(len(r.replies) for r in items)} requests.",
    ]
    failures = [r for r in items if r.outcome != "valid"]
    if failures:
        lines += ["", "Not valid:"]
        lines += [
            f"  {_short(r.model)}@{r.host} {r.case}: {r.outcome}: {r.detail}"[:400]
            for r in failures
        ]
    gaps = [r for r in items if r.reasoning_gaps]
    if gaps:
        lines += ["", "Reasoning names a field the arguments omit:"]
        lines += [
            f"  {_short(r.model)}@{r.host} {r.case} ({r.outcome}): {', '.join(r.reasoning_gaps)}"
            for r in gaps
        ]
    return lines
