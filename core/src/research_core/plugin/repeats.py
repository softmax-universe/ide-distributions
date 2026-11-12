"""Repeated failures: a shorter history across the request, and a notice when Hermes halts a turn.

Hermes's loop guard (``tool_loop_guardrails``, hard stop on in the shipped configs) ends a turn
with ``turn_exit_reason = "guardrail_halt"``. In Hermes v0.22.6 the shipped hard stop halts after
5 failures of one tool (``same_tool_failure``, lowered from Hermes's 8; never for the
failure-tolerant tools below), blocks after 5 identical failing calls (Hermes's default
``exact_failure``), and halts after 10 identical calls with identical results or 10 laps of an
identical call cycle, for any tool but pollers (``idempotent_no_progress``, raised from Hermes's
5). A block ends the turn too, and so does Hermes's per-turn cap of 50 ``web_search`` calls or
subagent spawns. So a halt does not always mean a tool failed. The runs API reports such a run
``completed``, and nobody reads a wake's final reply, so these fill the gaps:

* ``without_repeated_refusals`` (``llm_request`` middleware): a model shown the same refused call
  several times tends to send it again, whatever the refusal says. So each request keeps only the
  latest of identical refused calls. A copy stays if its response held several calls, if its
  result holds anything but the refusal and loop guard notes, or if dropping it would leave two
  user messages in a row. Only the request changes: the stored history keeps them all, and
  Hermes's guard still counts them. When a call is refused again, the provider's cache misses once
  from where the earlier copy was. Measured on Kimi K3.1 Pro on 2026-11-04 (README, "Develop and
  test").
* ``transform_tool_result``: the second time the same call (tool and arguments) is refused in a
  turn, its result is replaced by a short refusal that names what to supply and how to get out
  (report the problem and end the run). The replacement is still a JSON refusal with an
  ``"error"`` key, so Hermes's guard keeps counting it as a failure.
* ``on_session_end``: Hermes fires it at the end of every turn. On ``guardrail_halt`` it writes
  a notice naming the Session, its kind and the tool with the most failures this turn, if any
  (failures of tools Hermes never halts on are not counted).

Counts live in memory per Session and are dropped at the end of each turn, as Hermes resets its
own counters each turn. All of these run inside Hermes: stdlib only, never raising.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

from research_core.plugin.advisory import failure_of
from research_core.plugin.executor import profile_env
from research_core.tools.catalog import Role

log = logging.getLogger(__name__)

HALT_REASON = "guardrail_halt"
# Tools whose failures Hermes's same_tool_failure halt never counts (a red test run is normal
# work). Copied from Hermes v0.22.6 agent/tool_guardrails.py FAILURE_TOLERANT_TOOL_NAMES: the
# plugin cannot import Hermes.
FAILURE_TOLERANT_TOOL_NAMES = frozenset(
    {"terminal", "execute_code", "process_manage", "process", "browser_navigate", "web_extract"}
)
_REQUIRED = re.compile(r"^([\w.]+) is required\b")
# What Hermes's loop guard appends to a result, about the repeat itself. Anything else after a
# refusal (a user's mid-turn message, a budget warning, a plugin's notice) is sent only there.
_ONLY_GUARD_NOTES = re.compile(
    r"(?:\s*\[(?:Tool loop warning|Tool loop hard stop|hermes note): [^\n]*\])*\s*"
)
# How each role reports a problem it cannot fix itself.
WAY_OUT: dict[str, str] = {
    "orchestrator": "report the problem to the user (from a wake, a failure chat_update)",
    "researcher": "report the problem with task_ask if you hold a Task",
}

_lock = threading.Lock()
# Session id -> (tool name, argument hash) -> refusals of that exact call this turn.
_refusals: dict[str, Counter[tuple[str, str]]] = {}
# Session id -> tool name -> failed calls this turn (names the tool in the halt notice).
_failures: dict[str, Counter[str]] = {}


def refusal_of(result: Any) -> dict | None:
    """A profile tool's refusal (``{"ok": false, "error", "problems"}``) parsed; else ``None``.

    Text after the JSON is allowed: Hermes appends its loop warnings to a result in the history.
    """
    if not isinstance(result, str) or not result.lstrip().startswith("{"):
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(result.lstrip())
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get("ok") is False and parsed.get("problems"):
        return parsed
    return None


def args_hash(args: Any) -> str:
    text = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def firmer_refusal(role: Role, tool_name: str, refusal: dict, count: int) -> str:
    """The short refusal that replaces the ``count``-th identical one. It may be the only copy a
    request keeps, so all but the long ``usage`` stays: a wiki conflict's ``current_body``, say."""
    problems = [str(p) for p in refusal.get("problems") or []]
    fields = [m.group(1) for p in problems if (m := _REQUIRED.match(p))]
    if fields:
        supply = f"Supply {', '.join(dict.fromkeys(fields))}"
    else:
        supply = f"Fix what the refusal names ({'; '.join(problems)[:300]})"
    way_out = WAY_OUT.get(role, "report the problem")
    message = f"This exact call was refused {count} times. {supply}, or {way_out} and end the run."
    kept = {key: value for key, value in refusal.items() if key != "usage"}
    return json.dumps(
        {
            **kept,
            "ok": False,
            "error": message,
            "problems": problems,
            "context": {**(refusal.get("context") or {}), "tool": tool_name, "refused": count},
        }
    )


def _result_text(content: Any) -> Any:
    """A result's text, or ``None`` if it holds more than text. Hermes sends the last results as
    text parts carrying its cache markers."""
    if not isinstance(content, list):
        return content
    if all(part.get("type") == "text" for part in content):
        return "".join(part["text"] for part in content)
    return None


def _refused_call(messages: list[dict], index: int) -> tuple[str, str] | None:
    """(tool, arguments text) when ``messages[index]`` is a single call whose result, the next
    message, is a refusal with nothing added but loop guard notes. Hermes sends arguments in one
    canonical form, so equal text is an identical call."""
    calls = messages[index].get("tool_calls") or []
    if len(calls) != 1 or index + 1 == len(messages):
        return None
    result = messages[index + 1]
    text = _result_text(result.get("content"))
    if result.get("tool_call_id") != calls[0].get("id") or refusal_of(text) is None:
        return None
    _, end = json.JSONDecoder().raw_decode(text.lstrip())
    if not _ONLY_GUARD_NOTES.fullmatch(text.lstrip()[end:]):
        return None
    return calls[0]["function"]["name"], calls[0]["function"]["arguments"]


def drop_repeated_refusals(messages: list[dict]) -> list[dict]:
    """``messages`` with only the latest of each set of identical refused calls."""
    latest: dict[tuple[str, str], int] = {}
    dropped: set[int] = set()
    for index in range(len(messages)):
        call = _refused_call(messages, index)
        if call is None:
            continue
        if call in latest:
            dropped |= {latest[call], latest[call] + 1}
        latest[call] = index
    kept: list[dict] = []
    for index, message in enumerate(messages):
        if index in dropped:
            continue
        # Hermes merges adjacent user messages before this runs, and some providers reject them.
        previous_role = kept[-1].get("role") if kept else None
        if index - 1 in dropped and message.get("role") == "user" == previous_role:
            kept += messages[index - 2 : index]
        kept.append(message)
    return kept


def without_repeated_refusals(*, request: dict[str, Any], **_: Any) -> dict[str, Any] | None:
    """``llm_request`` middleware: send ``drop_repeated_refusals`` of the request's messages."""
    try:
        messages = request.get("messages") or []
        kept = drop_repeated_refusals(messages)
        if len(kept) == len(messages):
            return None
        return {"request": {**request, "messages": kept}, "reason": "Drop repeated refused calls"}
    except Exception as error:  # never break a model call
        log.debug("repeated refusal middleware failed: %s", error)
        return None


def make_hooks(role: Role) -> dict[str, Callable[..., Any]]:
    def transform_tool_result(
        tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "", **_: Any
    ) -> str | None:
        try:
            if not session_id:
                return None
            failed = failure_of(result) is not None
            refusal = refusal_of(result)
            key = (tool_name, args_hash(args))
            with _lock:
                if not failed:
                    _refusals.get(session_id, Counter()).pop(key, None)
                    return None
                if tool_name not in FAILURE_TOLERANT_TOOL_NAMES:
                    _failures.setdefault(session_id, Counter())[tool_name] += 1
                if refusal is None:
                    return None
                refusals = _refusals.setdefault(session_id, Counter())
                refusals[key] += 1
                count = refusals[key]
            return firmer_refusal(role, tool_name, refusal, count) if count >= 2 else None
        except Exception as error:  # never break a tool result
            log.debug("repeat hook failed: %s", error)
            return None

    def on_session_end(session_id: str = "", turn_exit_reason: str | None = None, **_: Any) -> None:
        try:
            with _lock:
                _refusals.pop(session_id, None)
                failures = _failures.pop(session_id, Counter())
            if turn_exit_reason != HALT_REASON or not session_id:
                return
            write_halt_notice(role, session_id, failures)
        except Exception as error:  # best effort only
            log.debug("halt notice hook failed: %s", error)

    return {"transform_tool_result": transform_tool_result, "on_session_end": on_session_end}


def write_halt_notice(role: Role, session_id: str, failures: Counter[str]) -> None:
    """The notice for a turn Hermes's loop guard halted: the Session, its kind and the tool with
    the most counted failures, if any (a halt on identical successful calls has none)."""
    from research_core.store import notices, sessions
    from research_core.tools.base import ToolContext

    ctx = ToolContext.from_env(role, profile_env(IDE_SESSION_ID=session_id))
    session = sessions.read(ctx.wstore, session_id)
    kind = session["kind"] if session else "unregistered"
    about = f"{kind} Session {session_id}"
    refs: list[str] = []
    if session and session.get("event"):
        about += f" for {session['event']}"
    if session and session.get("task_id"):
        about += f" on T-{session['task_id']}"
        refs.append(f"T-{session['task_id']}")
    most_failures = f" (most failures: {failures.most_common(1)[0][0]})" if failures else ""
    notices.add(
        ctx.wstore,
        ctx.actor,
        text=(
            f"Hermes's loop guard stopped the {role}'s {about} after repeated calls"
            f"{most_failures}. Its work may be unfinished."
        ),
        refs=refs,
        urgency="high",
    )
