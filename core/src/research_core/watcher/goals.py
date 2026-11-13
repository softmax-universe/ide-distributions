"""Step 8: Goal conditions and the daily anti-stall event (design §8 wake table, Appendix H).

Three conditions, each an event a management or anti-stall wake acts on; nothing here writes a
record:

* ``goal.needs_ideation`` for an active Goal with no open work and no Task waiting on the user
  (``goals.needs_ideation``). Emitted once while the condition holds: the Coworld store's meta
  ``ideation_pending:<G-n>`` remembers it and is cleared when the Goal has an open Task again
  (or stops needing ideation for any other reason), so the next time it empties a new event
  follows. An ongoing Goal also re-arms when work ends between scans. A Goal due for parking
  is not also asked to ideate.
* ``goal.park`` when ``ideation_rounds_without_progress`` reaches the Coworld setting
  ``ideation.park_after_rounds``, once per stretch (meta ``park_pending:<G-n>``); the wake parks
  the Goal with a reason and tells the user.
* ``anti_stall`` once a day per Coworld with an active Goal, at the workspace setting
  ``anti_stall.hour`` (``HH:MM`` in America/Los_Angeles), in that Coworld's store, so its wake runs
  in that Coworld: Goals and Tasks can only be read from a Session that has one. The Coworld store's
  meta ``anti_stall_last`` holds the local date it was last due (or the day the Coworld first
  ticked), so a tick after the hour fires it even when the workspace slept through 04:00, and a
  brand-new Coworld waits until the next day.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from research_core.store import goals as goals_store
from research_core.store import settings
from research_core.store._common import parse_display_id
from research_core.store.db import Store
from research_core.watcher.resume import emit_event

if TYPE_CHECKING:  # pragma: no cover
    from research_core.watcher.context import TickContext

LOCAL_ZONE = ZoneInfo("America/Los_Angeles")
IDEATION_PENDING = "ideation_pending:"
PARK_PENDING = "park_pending:"
ANTI_STALL_LAST = "anti_stall_last"


def run(ctx: TickContext) -> dict[str, Any]:
    report: dict[str, Any] = {"needs_ideation": [], "park": [], "anti_stall": False}
    if ctx.store is not None:
        _goal_conditions(ctx, report)
        report["anti_stall"] = anti_stall(ctx)
    return report


def _goal_conditions(ctx: TickContext, report: dict[str, Any]) -> None:
    store = ctx.store
    park_after = int(settings.get(store, "ideation.park_after_rounds"))
    needs = set(goals_store.needs_ideation(store))
    for goal in goals_store.list(store):
        shown = goal["id"]
        n = parse_display_id(shown)[1]
        active = goal["status"] == "active"
        rounds = goals_store.read(store, n)["ideation_rounds_without_progress"] if active else 0
        park_due = active and rounds >= park_after
        if _first_tick(store, f"{PARK_PENDING}{shown}", park_due):
            emit_event(
                store,
                ctx.actor,
                "goal.park",
                shown,
                {
                    "goal": shown,
                    "title": goal["title"],
                    "rounds_without_progress": rounds,
                    "park_after_rounds": park_after,
                },
            )
            report["park"].append(shown)
        key = f"{IDEATION_PENDING}{shown}"
        if n not in needs or park_due:
            if store.get_meta(key) is not None:
                store.delete_meta(key)
            continue
        revision = (
            str(goals_store.ideation_revision(store, n)) if goal["kind"] == "ongoing" else "1"
        )
        if store.get_meta(key) != revision:
            with store.tx(ctx.actor) as tx:
                tx.event("goal.needs_ideation", shown, {"goal": shown, "title": goal["title"]})
                store.set_meta(key, revision)
            report["needs_ideation"].append(shown)


def _first_tick(store: Store, key: str, due: bool) -> bool:
    """True on the first tick ``due`` holds; the mark is forgotten once it stops holding."""
    pending = store.get_meta(key) == "1"
    if due and not pending:
        store.set_meta(key, "1")
        return True
    if not due and pending:
        store.set_meta(key, "0")
    return False


def anti_stall(ctx: TickContext) -> bool:
    """Emit ``anti_stall`` for this tick's Coworld when the local day's hour has passed and it has
    not been due today. A wake starts a model run, so a Coworld with no active Goal gets none.

    The first tick of a Coworld's life only records the day: one set up a minute ago has nothing to
    be stalled on, and the wake would fire only because the hour of the day had already passed.
    """
    store = ctx.coworld_store()
    hour_text = str(settings.get(ctx.wstore, "anti_stall.hour"))
    hours, _, minutes = hour_text.partition(":")
    local = ctx.now.astimezone(LOCAL_ZONE)
    due_at = local.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0)
    today = local.date().isoformat()
    last = store.get_meta(ANTI_STALL_LAST)
    if last is None:
        store.set_meta(ANTI_STALL_LAST, today)
        return False
    if local < due_at or last == today:
        return False
    store.set_meta(ANTI_STALL_LAST, today)
    if not any(goal["status"] == "active" for goal in goals_store.list(store)):
        return False
    emit_event(
        store,
        ctx.actor,
        "anti_stall",
        today,
        {"date": today, "hour": hour_text, "local_time": local.isoformat(timespec="minutes")},
    )
    return True
