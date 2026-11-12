"""Settings: the registry of keys, their Appendix H defaults, per-key scope and cited updates.

Settings bound the agents' own initiative, never the user's explicit request (design B.9). Each
key has a scope (A2.4): workspace keys (``paused``, ``max_researchers``, ``recovery.*``,
``anti_stall.*``, ``chat_updates.*``) live in the workspace store, the rest in the Coworld
store. ``get`` accepts one store or a pair of stores and reads the key from the store its scope
names; ``update`` refuses a key on the wrong store. Values are JSON text in the ``value`` column.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from research_core.store._common import json_field, parse_iso, parse_json, require
from research_core.store.errors import StoreError
from research_core.store.registry import CHAT_UPDATE_CATEGORIES, TASK_TYPES

if TYPE_CHECKING:
    from research_core.store.actors import Actor
    from research_core.store.db import Store

Scope = Literal["workspace", "coworld"]
TYPES = ("bool", "int", "number", "text", "enum", "list", "timestamp", "time_of_day")

_TIME_OF_DAY = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: str
    default: Any
    scope: Scope
    enforced_by: str
    description: str
    # Allowed values for an enum; allowed items for a list.
    enum: tuple[str, ...] | None = None
    nullable: bool = False
    minimum: int | float | None = None


def _s(
    key: str,
    type: str,
    default: Any,
    scope: Scope,
    enforced_by: str,
    description: str,
    *,
    enum: tuple[str, ...] | None = None,
    nullable: bool = False,
    minimum: int | float | None = None,
) -> SettingSpec:
    return SettingSpec(key, type, default, scope, enforced_by, description, enum, nullable, minimum)


SETTINGS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        _s(
            "paused",
            "bool",
            False,
            "workspace",
            "the watcher",
            "While true the watcher starts no Researcher work, drains running Sessions and holds "
            "new user messages.",
        ),
        _s(
            "max_researchers",
            "int",
            2,
            "workspace",
            "the watcher",
            "How many Researcher Sessions may run at once.",
            minimum=0,
        ),
        _s(
            "approval_required.task_types",
            "list",
            [],
            "coworld",
            "the task tool",
            "Task types only the user can accept; a user request in the conversation overrides it.",
            enum=TASK_TYPES,
        ),
        _s(
            "submissions.mode",
            "enum",
            "autonomous",
            "coworld",
            "the submit tool and the management wake",
            "ask or autonomous; applies only to submissions on the agents' own initiative.",
            enum=("ask", "autonomous"),
        ),
        _s(
            "submissions.until",
            "timestamp",
            None,
            "coworld",
            "the submit tool and the management wake",
            "End of the autonomous window; null means always open.",
            nullable=True,
        ),
        _s(
            "submissions.max_count",
            "int",
            None,
            "coworld",
            "the submit tool and the management wake",
            "Cap on autonomous submissions in the window; null means unlimited.",
            nullable=True,
            minimum=0,
        ),
        _s(
            "submissions.quality_bar",
            "bool",
            True,
            "coworld",
            "the submit tool and the management wake",
            "Autonomous submissions need a Version merged into main and an Experiment confirming "
            "it against the champion; with no champion, a passing mixin check.",
        ),
        _s(
            "submissions.min_margin_over_champion",
            "number",
            0,
            "coworld",
            "the submit tool and the management wake",
            "Minimum effect on the primary metric against the champion, under the quality bar.",
        ),
        _s(
            "platform_writes.forum",
            "enum",
            "allowed",
            "coworld",
            "the forum_post tool",
            "allowed, ask or never, for forum posts on the agents' own initiative.",
            enum=("allowed", "ask", "never"),
        ),
        _s(
            "platform_writes.wiki",
            "enum",
            "allowed",
            "coworld",
            "the wiki_edit tool",
            "allowed, ask or never, for wiki edits on the agents' own initiative.",
            enum=("allowed", "ask", "never"),
        ),
        _s(
            "xp.ask_above_requests_per_experiment",
            "int",
            None,
            "coworld",
            "the xp tool",
            "Above this many XP requests per Experiment a Researcher must ask the Orchestrator "
            "and the Orchestrator is warned; null means no limit.",
            nullable=True,
            minimum=0,
        ),
        _s(
            "ideation.park_after_rounds",
            "int",
            3,
            "coworld",
            "the management wake",
            "Ideation rounds without progress after which a Goal is parked.",
            minimum=1,
        ),
        _s(
            "recovery.receipt_timeout_seconds",
            "int",
            60,
            "workspace",
            "the coordinator",
            "Seconds awaiting an unknown start receipt before private-runtime exclusion.",
            minimum=1,
        ),
        _s(
            "recovery.delivery_backoff_seconds",
            "int",
            5,
            "workspace",
            "the coordinator",
            "Delay before retrying a definitively refused transient start or steering delivery.",
            minimum=1,
        ),
        _s(
            "recovery.max_attempts",
            "int",
            2,
            "workspace",
            "the watcher",
            "Failed execution or rejected-delivery attempts before an explicit failed outcome.",
            minimum=0,
        ),
        _s(
            "anti_stall.hour",
            "time_of_day",
            "04:00",
            "workspace",
            "the watcher",
            "Local time (America/Los_Angeles) of the daily anti-stall wake, as HH:MM.",
        ),
        _s(
            "chat_updates.categories",
            "list",
            ["decision", "outcome", "submission", "failure"],
            "workspace",
            "the ui tool",
            "Which categories of a background wake's ui.tell reach the user's chat as a "
            "message; any other category becomes a notice. An empty list sends them all to "
            "notices.",
            enum=CHAT_UPDATE_CATEGORIES,
        ),
    )
}


def spec(key: str) -> SettingSpec:
    """The registry entry for ``key``; a refusal naming the known keys otherwise."""
    found = SETTINGS.get(key)
    if found is None:
        raise StoreError([f"unknown setting {key!r}; known settings: {', '.join(SETTINGS)}"])
    return found


def get(store_or_pair: Store | Sequence[Store], key: str) -> Any:
    """The value of ``key`` from the store its scope names; the default when unset."""
    found = spec(key)
    store = _store_for(store_or_pair, found)
    row = store.one("SELECT value FROM settings WHERE key = ?", (key,))
    return found.default if row is None else parse_json(row["value"])


def all(store_or_pair: Store | Sequence[Store]) -> dict[str, Any]:
    """Every key whose scope one of the given stores serves, with its value or default."""
    stores = _stores(store_or_pair)
    kinds = {store.kind for store in stores}
    return {key: get(stores, key) for key, found in SETTINGS.items() if found.scope in kinds}


def update(store: Store, actor: Actor, key: str, value: Any, *, cites: str) -> dict[str, Any]:
    """Set ``key`` to ``value`` in ``store``; type-checked, scope-checked, citing the user.

    ``cites`` is the reference of the user's message that asked for the change (B.9). Emits
    ``settings.updated`` with the previous value.
    """
    found = spec(key)
    problems: list[str] = []
    if found.scope != store.kind:
        problems.append(f"{key} is a {found.scope} setting; this is the {store.kind} store")
    problems.extend(validate_value(found, value))
    require(
        problems,
        isinstance(cites, str) and cites.strip(),
        "cites is required: the reference of the user's message that asked for the change",
    )
    if problems:
        raise StoreError(problems)
    with store.tx(actor) as tx:
        previous_row = tx.one("SELECT value FROM settings WHERE key = ?", (key,))
        previous = found.default if previous_row is None else parse_json(previous_row["value"])
        tx.execute(
            "INSERT INTO settings(key, value, updated, updated_by, cites) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated,"
            " updated_by = excluded.updated_by, cites = excluded.cites",
            (key, json_field(value), tx.at, str(actor), cites),
        )
        tx.event(
            "settings.updated",
            key,
            {"key": key, "value": value, "previous": previous, "cites": cites},
        )
        return {
            "key": key,
            "value": value,
            "previous": previous,
            "default": found.default,
            "type": found.type,
            "scope": found.scope,
            "enforced_by": found.enforced_by,
            "description": found.description,
            "updated": tx.at,
            "updated_by": str(actor),
            "cites": cites,
        }


def validate_value(found: SettingSpec, value: Any) -> list[str]:
    """Every way ``value`` fails ``found``'s type, as problems; empty when it is acceptable."""
    key = found.key
    if value is None:
        return [] if found.nullable else [f"{key} cannot be null"]
    if found.type == "bool":
        return [] if isinstance(value, bool) else [f"{key} must be a bool (true or false)"]
    if found.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{key} must be an int"]
        return _at_least(found, value)
    if found.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return [f"{key} must be a number"]
        return _at_least(found, value)
    if found.type == "text":
        return [] if isinstance(value, str) else [f"{key} must be text"]
    if found.type == "enum":
        choices = ", ".join(found.enum or ())
        return [] if value in (found.enum or ()) else [f"{key} must be one of {choices}"]
    if found.type == "list":
        if not isinstance(value, list | tuple):
            return [f"{key} must be a list"]
        if found.enum is not None:
            bad = [item for item in value if item not in found.enum]
            if bad:
                choices = ", ".join(found.enum)
                return [f"{key} items must be among {choices}; not {', '.join(map(repr, bad))}"]
        return []
    if found.type == "timestamp":
        if isinstance(value, str):
            try:
                parse_iso(value)
                return []
            except ValueError:
                pass
        return [f"{key} must be an ISO 8601 timestamp such as 2026-10-29T06:00:00Z"]
    if found.type == "time_of_day":
        if isinstance(value, str) and _TIME_OF_DAY.match(value):
            return []
        return [f"{key} must be a time of day as HH:MM (24-hour)"]
    raise ValueError(f"setting {key} has an unknown type {found.type!r}")


def _at_least(found: SettingSpec, value: int | float) -> list[str]:
    if found.minimum is not None and value < found.minimum:
        return [f"{found.key} must be at least {found.minimum}"]
    return []


def _stores(store_or_pair: Store | Sequence[Store]) -> tuple[Store, ...]:
    if isinstance(store_or_pair, list | tuple):
        return tuple(store_or_pair)
    return (store_or_pair,)


def _store_for(store_or_pair: Store | Sequence[Store], found: SettingSpec) -> Store:
    stores = _stores(store_or_pair)
    for store in stores:
        if store.kind == found.scope:
            return store
    given = " and ".join(sorted({store.kind for store in stores}))
    raise StoreError([f"{found.key} is a {found.scope} setting; only the {given} store was given"])
