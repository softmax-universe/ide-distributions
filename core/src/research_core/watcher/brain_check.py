"""Brain check (IDE_BRAIN=1): notify about stale rules and idle skills. Notice only; nothing is
ever retired, edited or deleted here.

* A claim tagged ``rule`` with no supporting use for 90 days (``brain.is_stale``; applying a rule
  is recorded as ``claim_add_evidence`` with stance ``supports``) raises one notice per stale
  stretch (workspace-store meta ``brain_stale:<coworld>:<C-n>``, cleared when it is used again).
* An own skill (``<workspace>/skills/<name>/``) none of whose files changed for 90 days raises one
  notice (meta ``brain_idle_skill:<name>``). The skill's newest file mtime is the only usage
  signal Hermes leaves on disk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from research_core import brain
from research_core.store import claims, notices

if TYPE_CHECKING:  # pragma: no cover
    from research_core.watcher.context import TickContext

RULE_LIMIT = 200


def run(ctx: TickContext) -> dict[str, Any]:
    report: dict[str, Any] = {"stale_rules": [], "idle_skills": []}
    if not brain.enabled(ctx.env):
        return report
    if ctx.store is not None and ctx.coworld is not None:
        _stale_rules(ctx, report)
    _idle_skills(ctx, report)
    return report


def _stale_rules(ctx: TickContext, report: dict[str, Any]) -> None:
    coworld_id = ctx.coworld.coworld_id
    for found in claims.search(
        ctx.store, "", tags=["rule"], include_retired=False, limit=RULE_LIMIT
    ):
        key = f"brain_stale:{coworld_id}:{found['id']}"
        stale = brain.is_stale(claims.read(ctx.store, found["id"]), ctx.now)
        if stale and ctx.meta(key) is None:
            notices.add(
                ctx.wstore,
                ctx.actor,
                text=(
                    f"Rule {found['id']} has not been used for {brain.STALE_DAYS} days: "
                    f"{found['summary']} Keep it (add supporting evidence when it holds) or "
                    "retire it; nothing was changed."
                ),
                refs=[found["id"]],
                coworld_id=coworld_id,
            )
            ctx.set_meta(key, ctx.now.isoformat())
            report["stale_rules"].append(found["id"])
        elif not stale and ctx.meta(key) is not None:
            ctx.wstore.delete_meta(key)  # used again: a later lapse notifies again


def _idle_skills(ctx: TickContext, report: dict[str, Any]) -> None:
    root = ctx.ws.root / "skills"
    if not root.is_dir():
        return
    cutoff = ctx.now - timedelta(days=brain.STALE_DAYS)
    for skill in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        if skill.name == "seed" or skill.name.startswith("."):
            continue
        key = f"brain_idle_skill:{skill.name}"
        newest = _newest_mtime(skill)
        idle = newest is not None and newest < cutoff
        if idle and ctx.meta(key) is None:
            notices.add(
                ctx.wstore,
                ctx.actor,
                text=(
                    f"Skill {skill.name} has not changed for {brain.STALE_DAYS} days. Keep, "
                    "improve or retire it; nothing was changed."
                ),
            )
            ctx.set_meta(key, ctx.now.isoformat())
            report["idle_skills"].append(skill.name)
        elif not idle and ctx.meta(key) is not None:
            ctx.wstore.delete_meta(key)


def _newest_mtime(folder: Path) -> datetime | None:
    times = [p.stat().st_mtime for p in folder.rglob("*") if p.is_file() and not p.is_symlink()]
    return datetime.fromtimestamp(max(times), UTC) if times else None
