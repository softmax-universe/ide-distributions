"""Cogent learning on the record store (flag IDE_BRAIN=1; off = no behaviour change).

Lessons, failures, gotchas, rules, wins and unverified notes are claims with tags (``lesson``,
``failed``, ``gotcha``, ``rule``, ``win``, ``unverified``); "seen again" is ``claim_add_evidence``
with stance ``supports``; hypotheses are experiments; plans and handoffs are Goals and Tasks.
This package holds only what the store does not: the flag, the computed ``stale`` signal, the
SOUL hook. Stdlib only.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

FLAG = "IDE_BRAIN"
STALE_DAYS = 90  # cogent-howto: a rule UNUSED for 90 days is flagged (ARCHITECTURE.md, rules.md)
TAGS = ("lesson", "failed", "gotcha", "rule", "win", "unverified")


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """The flag, read from ``env``; with no ``env``, from the profile's environment (the routed
    profile's ``.env`` under a multiplexing gateway keeps it out of ``os.environ``)."""
    if env is None:
        from research_core.plugin.executor import profile_env

        env = profile_env()
    return env.get(FLAG, "") == "1"


def last_used(claim: Mapping[str, Any]) -> str | None:
    """When a rule was last used: the newest supporting or qualifying evidence link (the store's
    computed ``freshness``). Applying a rule is recorded as ``claim_add_evidence`` with stance
    ``supports``, so this is the only signal needed. Falls back to the claim's own ``created``."""
    return claim.get("freshness") or claim.get("created")


def is_stale(claim: Mapping[str, Any], now: datetime, days: int = STALE_DAYS) -> bool:
    """A claim tagged ``rule`` with no supporting use for ``days`` days. Other claims are never
    stale (their freshness is shown, not flagged)."""
    from research_core.store._common import parse_iso

    if "rule" not in (claim.get("tags") or []) or claim.get("status", "active") != "active":
        return False
    seen = last_used(claim)
    return seen is not None and now - parse_iso(seen) > timedelta(days=days)
