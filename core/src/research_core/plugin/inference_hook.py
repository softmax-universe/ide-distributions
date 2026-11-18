"""The ``api_request_error`` hook, which records failed model calls in the inference state, and
``bound_output_request``, the ``llm_request`` middleware that caps a request's output.

Hermes calls the hook after it classifies each failed model call, with the class as ``reason``
(a ``FailoverReason`` value from ``agent/error_classifier.py``: ``billing``, ``rate_limit``,
``overloaded``, ``server_error``, ``timeout``, ...), plus ``session_id``, ``task_id`` and
``status_code``. ``session_id`` is the agent's live id, which compression rotates; ``task_id``
is the session id the run started with, so it is what ties an error to a run.
The hook always records the call as ``last_error``, then:

* a 402 that can still afford a smaller prompt or reply is recorded as
  ``context_overflow`` or ``output_limit`` without closing the gate;
* on ``billing`` (402 credits, or a 403 "Key limit exceeded") it blocks model work as
  ``budget_exhausted`` for the installed key (the lease file's ``key_id``), showing the next
  refill as ``until``. The block ends when the lease loop installs a different key; nobody
  writes to clear it. Within :data:`KEY_CHANGE_GRACE` of the lease's ``key_changed_at`` it
  does not block: such errors come from runs still finishing on the previous, spent key.
* after :data:`OUTAGE_COUNT` outage-class errors within :data:`OUTAGE_WINDOW` it blocks model
  work as ``provider_unavailable`` for :data:`OUTAGE_BLOCK`. Nobody clears that block; it lapses.

The state file lives beside ``IDE_INFERENCE_LEASE_FILE``; without that variable (a profile
run outside a platform-managed workspace) the hook does nothing. It runs inside Hermes: stdlib
only, and it never raises.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from research_core import inference
from research_core.plugin.executor import profile_env

log = logging.getLogger(__name__)

OUTAGE_REASONS = frozenset({"overloaded", "server_error", "timeout"})
OUTAGE_COUNT = 3
OUTAGE_WINDOW = timedelta(minutes=3)
OUTAGE_BLOCK = timedelta(minutes=5)
KEY_CHANGE_GRACE = timedelta(minutes=2)


def _classify_reason(reason: str, status_code: int | None, message: str) -> str:
    """Refine Hermes's error class when OpenRouter reports an affordability limit."""
    if status_code == 402:
        prompt_limit = re.search(
            r"prompt tokens limit exceeded:\s*[\d,]+\s*>\s*([\d,]+)", message, re.IGNORECASE
        )
        if prompt_limit is not None and int(prompt_limit[1].replace(",", "")) > 0:
            return "context_overflow"
        output_limit = re.search(r"can only afford ([\d,]+)", message, re.IGNORECASE)
        if output_limit is not None and int(output_limit[1].replace(",", "")) > 0:
            return "output_limit"
    if status_code == 403 and "key limit exceeded" in message.lower():
        return "billing"
    return reason


def record_api_error(
    lease_file: Path,
    *,
    session_id: str,
    reason: str,
    status_code: int | None,
    message: str = "",
    task_id: str | None = None,
    now: datetime | None = None,
) -> inference.InferenceState:
    """Record one failed model call in the inference state and set the block it implies."""
    now = now or datetime.now(UTC)
    reason = _classify_reason(reason, status_code, message)
    lease = inference.read_lease(lease_file)
    installed_key_id = None if lease is None else lease.key_id

    def change(state: inference.InferenceState) -> inference.InferenceState:
        state = replace(
            state,
            last_error=inference.ApiError(
                session_id=session_id,
                reason=reason,
                status_code=status_code,
                at=now,
                task_id=task_id,
            ),
        )
        if reason == "billing":
            changed_at = None if lease is None else lease.key_changed_at
            if changed_at is not None and now - changed_at < KEY_CHANGE_GRACE:
                return state
            return state.with_block(
                "budget_exhausted",
                inference.next_refill(lease, state, now),
                now,
                installed_key_id,
            )
        if reason not in OUTAGE_REASONS:
            return state
        outages = tuple(at for at in state.outages if now - at < OUTAGE_WINDOW) + (now,)
        state = replace(state, outages=outages)
        # A budget block in force outranks an outage: the refill, not the provider, ends it.
        if len(outages) >= OUTAGE_COUNT and not (
            state.blocked == "budget_exhausted"
            and state.blocking(now, installed_key_id=installed_key_id)
        ):
            state = state.with_block("provider_unavailable", now + OUTAGE_BLOCK, now)
        return state

    return inference.update_state(lease_file, change)


def make_api_request_error() -> Callable[..., None]:
    def api_request_error(
        session_id: str = "",
        reason: str | None = None,
        status_code: int | None = None,
        error: Any = None,
        task_id: str | None = None,
        **_: Any,
    ) -> None:
        try:
            lease_file = profile_env().get("IDE_INFERENCE_LEASE_FILE")
            if not lease_file:
                return
            message = str(error.get("message") or "") if isinstance(error, dict) else ""
            record_api_error(
                Path(lease_file),
                session_id=session_id or "",
                reason=reason or "unknown",
                status_code=status_code if isinstance(status_code, int) else None,
                message=message,
                task_id=task_id or None,
            )
        except Exception as failure:  # never take a model call down
            log.debug("inference-state hook failed: %s", failure)

    return api_request_error


# OpenRouter reserves the requested maximum before generation. A model's catalog maximum
# can exceed a daily key's remaining allowance even when an ordinary reply is affordable.
MAX_RESPONSE_TOKENS = 16384


def bound_output_request(
    *, request: dict[str, Any], provider: str, **_: Any
) -> dict[str, Any] | None:
    """Bound OpenRouter replies through Hermes's supported request middleware."""
    if provider != "openrouter":
        return None
    bounded = dict(request)
    fields = [
        key
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
        if key in bounded
    ]
    if not fields:
        fields = ["max_output_tokens" if "input" in bounded else "max_tokens"]
    for key in fields:
        bounded.setdefault(key, MAX_RESPONSE_TOKENS)
        bounded[key] = min(bounded[key] or MAX_RESPONSE_TOKENS, MAX_RESPONSE_TOKENS)
    return {"request": bounded, "reason": "Bound workspace output credit reservations"}
