"""Step 4: resolve what in-progress Tasks wait on, without a model (design B.13).

A Researcher that must wait records ``waiting_on`` with ``task_wait`` and ends its run (A4.4).
Every tick this step checks each wait against its source and, when it is over, calls
``tasks.resolve_wait``, whose ``task.wait_resolved`` event the wake step hands to
``resume.handle_wait_resolved`` after step 5 refreshed run statuses (A5.3). Nothing here
starts or steers a run.

Kinds and what resolves them:

- ``xp``: every request id in ``ref`` is terminal on the platform, or not found there (a 404
  ends the wait with a note, as such a request never finishes); progress is also written to
  the Experiment runs that carry those ids when the store lets the watcher (a Task-scoped
  Experiment is written by its holder only, so that write is skipped and the Researcher
  records the results itself);
- ``credits``: the balance is above zero and at least the need the detail states (``need N``);
- ``permit``: a GET of the recorded ask route shows an answer (a 404 is "not yet");
- ``submission``: a submission of the policy version in ``ref`` reached ``placed`` or
  ``rejected``;
- ``platform``: the recorded route answers again (a GET route is probed itself; for any other
  method ``whoami`` stands in, since the probe must not repeat a write);
- ``process``: the pid in ``ref`` is gone;
- ``question``: an undelivered answer exists for the Task;
- ``user``: the setting named in ``ref`` has been set, or the Task named in ``ref`` is approved;
- ``dependency``: every Task this one depends on is complete and did not fail.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from research_core.platform.errors import PlatformError
from research_core.store import experiments, settings, tasks
from research_core.store.errors import StoreError

if TYPE_CHECKING:  # pragma: no cover
    from research_core.store.db import Store
    from research_core.watcher.context import TickContext

Resolver = Callable[["TickContext", "Store", dict[str, Any], dict[str, Any]], str | None]

NEED = re.compile(r"\bneeds?\s+(\d+(?:\.\d+)?)")
ROUTE = re.compile(r"^\s*([A-Z]+)\s+(/\S*)")
SUBMISSION_FINAL = frozenset({"placed", "rejected"})
PERMIT_ANSWER_KEYS = ("answer", "answered_at", "resolved_at", "decision", "response")
PERMIT_FINAL_STATUSES = frozenset(
    {"approved", "denied", "answered", "resolved", "granted", "rejected", "closed"}
)
NOTE_LIMIT = 200


def run(ctx: TickContext) -> dict[str, Any]:
    """Check every wait of the active Coworld's in-progress Tasks; resolve those that are over."""
    if ctx.store is None:
        return {"skipped": "no active Coworld"}
    store = ctx.store
    report: dict[str, Any] = {"resolved": [], "waiting": [], "errors": []}
    for task in tasks.list(store, status="in-progress"):
        waiting = task["waiting_on"]
        if not waiting:
            continue
        kind = waiting.get("kind")
        resolver = RESOLVERS.get(kind)
        if resolver is None:
            report["errors"].append(
                {"task": task["id"], "kind": kind, "error": "unknown wait kind"}
            )
            continue
        try:
            note = resolver(ctx, store, task, waiting)
        except PlatformError as error:
            report["errors"].append({"task": task["id"], "kind": kind, "error": error.detail})
            continue
        if note is None:
            report["waiting"].append({"task": task["id"], "kind": kind})
            continue
        tasks.resolve_wait(store, ctx.actor, task["id"], note=note)
        report["resolved"].append({"task": task["id"], "kind": kind, "note": note})
    return report


# --- resolvers (a note when resolved, None while the wait goes on) -------------------------------


def _xp(ctx: TickContext, store: Store, task: dict, waiting: dict) -> str | None:
    refs = _refs(waiting.get("ref"))
    if not refs:
        return "no XP request ids were recorded for this wait"
    requests, missing = [], []
    for ref in refs:
        try:
            requests.append(ctx.platform.xp_get(ref))
        except PlatformError as error:
            if error.status != 404:
                raise
            missing.append(ref)  # it never finishes: waiting on it would sleep for good
    _record_runs(store, ctx, requests)
    if not all(request.terminal for request in requests):
        return None
    parts = [f"{r.id} {r.status}" for r in requests]
    parts += [f"{ref} not found on the platform" for ref in missing]
    return "XP requests " + ", ".join(parts)


def _record_runs(store: Store, ctx: TickContext, requests: list) -> None:
    """Platform state and episode counts onto the Experiment runs that carry these ids."""
    for request in requests:
        rows = store.q(
            "SELECT id FROM experiment_runs WHERE platform_request_id = ?", (request.id,)
        )
        for row in rows:
            try:
                experiments.update_run(
                    store,
                    ctx.actor,
                    row["id"],
                    state=request.status,
                    episodes_completed=request.completed_count,
                )
            except StoreError:
                # Task-scoped Experiments take writes from their holder only (A2.3); the
                # resumed Researcher records the results.
                continue


def _credits(ctx: TickContext, store: Store, task: dict, waiting: dict) -> str | None:
    credits = ctx.platform.credits()
    match = NEED.search(waiting.get("detail") or "")
    need = float(match.group(1)) if match else 0.0
    if credits.max is not None:
        need = min(need, credits.max)  # the balance can never pass the account's maximum
    if credits.balance <= 0 or credits.balance < need:
        return None
    return f"credits balance {credits.balance:g} covers the need ({need:g})"


def _permit(ctx: TickContext, store: Store, task: dict, waiting: dict) -> str | None:
    route = waiting.get("ref")
    if not isinstance(route, str) or not route:
        return None
    try:
        payload = ctx.platform.request("GET", route)
    except PlatformError as error:
        if error.status == 404:
            return None
        raise
    answer = permit_answer(payload)
    if answer is None:
        return None
    return f"permit {route} answered: {answer}"[:NOTE_LIMIT]


def permit_answer(payload: Any) -> str | None:
    """What a platform ask's payload says once it is answered, or None while it is open."""
    if not isinstance(payload, dict):
        return None
    for key in PERMIT_ANSWER_KEYS:
        if payload.get(key) not in (None, "", False):
            return f"{key}={payload[key]}"
    status = payload.get("status") or payload.get("state")
    if isinstance(status, str) and status.lower() in PERMIT_FINAL_STATUSES:
        return status
    return None


def _submission(ctx: TickContext, store: Store, task: dict, waiting: dict) -> str | None:
    ref = waiting.get("ref")
    if not ref:
        return None
    for submission in ctx.platform.submissions(policy_version_id=str(ref)):
        if submission.status in SUBMISSION_FINAL:
            return f"submission {submission.id} of {ref} {submission.status}"
    return None


def _platform(ctx: TickContext, store: Store, task: dict, waiting: dict) -> str | None:
    match = ROUTE.match(waiting.get("detail") or "")
    try:
        if match and match.group(1) == "GET":
            ctx.platform.request("GET", match.group(2))
            return f"GET {match.group(2)} answers again"
        ctx.platform.whoami()
    except PlatformError:
        return None
    return "the platform answers again (whoami)"


def _process(ctx: TickContext, store: Store, task: dict, waiting: dict) -> str | None:
    try:
        pid = int(waiting.get("ref"))
    except (TypeError, ValueError):
        return f"no valid pid in the wait reference {waiting.get('ref')!r}"
    if process_alive(pid):
        return None
    return f"process {pid} has ended"


def process_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists (signal 0; a permission error still means yes)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _question(ctx: TickContext, store: Store, task: dict, waiting: dict) -> str | None:
    pending = tasks.pending_messages(store, task["id"], "to_researcher")
    answers = [message for message in pending if message["kind"] == "answer"]
    if not answers:
        return None
    return f"answered: {answers[-1]['text']}"[:NOTE_LIMIT]


def _user(ctx: TickContext, store: Store, task: dict, waiting: dict) -> str | None:
    ref = str(waiting.get("ref") or "")
    if ref in settings.SETTINGS:
        scope = settings.spec(ref).scope
        for candidate in ctx.stores:
            if candidate.kind != scope:
                continue
            row = candidate.one("SELECT value FROM settings WHERE key = ?", (ref,))
            if row is not None:
                return f"setting {ref} was set to {row['value']}"
        return None
    if ref.startswith("T-"):
        try:
            approved_by = tasks.read(store, ref)["approved_by"]
        except StoreError:
            return None
        return f"{ref} approved by {approved_by}" if approved_by else None
    return None


def _dependency(ctx: TickContext, store: Store, task: dict, waiting: dict) -> str | None:
    """Resolved once the Task in ``ref`` has ended, however it ended: a failed or abandoned one
    will never complete, so the waiter is told instead of sleeping for good."""
    ended = []
    for ref in [waiting["ref"]] if waiting.get("ref") else task["depends_on"]:
        dependency = tasks.read(store, ref)
        if dependency["status"] in tasks.OPEN_STATUSES:
            return None
        ended.append(f"{ref} {dependency['outcome'] or dependency['status']}")
    return "dependencies ended: " + ", ".join(ended)


RESOLVERS: dict[str, Resolver] = {
    "xp": _xp,
    "credits": _credits,
    "permit": _permit,
    "submission": _submission,
    "platform": _platform,
    "process": _process,
    "question": _question,
    "user": _user,
    "dependency": _dependency,
}


def _refs(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list | tuple):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]
