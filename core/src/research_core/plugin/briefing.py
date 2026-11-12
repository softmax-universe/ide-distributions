"""The briefing prompt section (design §9, Appendix H; amendments A3.3, A3.4, A7.3, A7.8).

A computed status summary in every Session's system prompt: the workspace status, the Player's
id, the last capability check and what is broken, the active directives, the champion and recent
Versions, this Session's Task, the active Goals, any older preference claims (A3.10) and recent
claims tagged with active Goals. Every item is one line with references, so the agent reads the
record it needs with the record tools.

It is a snapshot. Hermes renders it when it builds a Session's prompt, then stores and replays that
prompt. Only two things render it again: a compaction inside a live agent's turn, and a refresh
after a release (``frozen_sessions.py``). The gateway's own compaction between turns keeps the
stored text (``_retain_seeded_system_prompt`` in Hermes). So nothing added since then is in it, and
its first line says when it was written.

Budget: at most 3,600 characters on the Orchestrator (its two sections share Hermes's
8,000-character aggregate with the session-context section) and 3,800 on the Researcher
(A3.3). On overflow the lowest-priority list is cut from its end first and ends with
"+N more, search with <tool>".

The briefing never runs a check (A3.4): it reads the last stored capability check and prints
its age. Each item is read on its own; one that fails renders as "<item>: unavailable: <Type>"
and the rest still renders. No store path appears (B.10). Stdlib only: it renders inside Hermes.

Watcher bookkeeping read from ``meta`` (the watcher and the doctor write these keys):

- workspace store: ``heartbeat`` (ISO time of the last tick), ``has_work`` (``true`` or
  ``false``), ``player_id`` (from the login check; ``SOFTMAX_PLAYER_ID`` wins), ``cli_versions``
  (JSON ``{"<cli>": {"installed", "lock"}, "lock_refreshed": "<date>"}``, A7.8);
- Coworld store: ``checks_at`` (ISO time of the last capability check), ``credits`` (JSON
  ``{"balance", "held"?, "next_refill"?}`` from that check), ``league_versions`` (one line from
  champion sync
  when no league runs the Coworld or its leagues run another version than the workspace).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from research_core.plugin.executor import profile_env
from research_core.store._common import parse_display_id, parse_iso
from research_core.tools.catalog import Role

if TYPE_CHECKING:
    from research_core.store.db import Store
    from research_core.tools.base import ToolContext

MAX_CHARS: dict[str, int] = {"orchestrator": 3600, "researcher": 3800}
RECENT_VERSIONS = 3
# Caps on what is loaded before the budget cuts; the "+N more" note counts what exists.
PREFERENCE_CLAIMS_LOADED = 100
GOAL_CLAIMS_LOADED = 300
TRUNCATED = "[+ truncated]"
TITLE_CHARS = 70
SUMMARY_CHARS = 110
DIRECTIVE_CHARS = 200


@dataclass
class Section:
    """One briefing item: fixed lines, then entries the budget may cut from the end.

    ``total`` is how many entries exist when more than were loaded, so the "+N more" note
    counts what the agent can still find with ``more`` (the tool action that lists the rest).
    """

    lines: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)
    more: str = ""
    total: int | None = None
    cut: int = 0

    @property
    def remaining(self) -> int:
        return len(self.entries) - self.cut

    def render(self) -> list[str]:
        out = [*self.lines, *self.entries[: self.remaining]]
        hidden = (self.total if self.total is not None else len(self.entries)) - self.remaining
        if hidden > 0:
            out.append(f"+{hidden} more, {self.more}")
        return out


@dataclass
class Inputs:
    """What every item reads: the stores, the Session row and the clock."""

    ctx: ToolContext
    role: str
    now: datetime
    wstore: Store
    store: Store | None
    coworld_id: str | None
    session: dict[str, Any] | None

    @property
    def goal_id(self) -> int | None:
        """The Session's Goal: its own, else its Task's."""
        if not self.session:
            return None
        if self.session.get("goal_id"):
            return int(self.session["goal_id"])
        task_id = self.session.get("task_id")
        if (
            task_id
            and self.store is not None
            and self.session.get("coworld_id")
            in (
                None,
                self.coworld_id,
            )
        ):
            row = self.store.one("SELECT goal_id FROM tasks WHERE id = ?", (task_id,))
            return None if row is None or row["goal_id"] is None else int(row["goal_id"])
        return None


def render(
    ctx: ToolContext,
    session_id: str,
    *,
    role: Role | None = None,
    max_chars: int | None = None,
    now: datetime | None = None,
) -> str:
    """The briefing text for ``session_id``, within ``max_chars`` (the role's budget)."""
    role = role or ctx.role
    max_chars = MAX_CHARS[role] if max_chars is None else max_chars
    inputs = _inputs(ctx, role, session_id, now or datetime.now(UTC))
    sections = [
        _item("Workspace", _workspace_status, inputs),
        _item("Capabilities", _capabilities, inputs),
        _item("Directives", _directives, inputs),
        _item("Champion", _champion_and_versions, inputs),
        _item("Submissions", _submission_window, inputs),
        _item("Your Task", _session_task, inputs),
        _item("Goals", _goals, inputs),
        _item("User preferences", _preference_claims, inputs),
        _item("Goal claims", _goal_claims, inputs),
    ]
    return ctx.scrub(_fit(sections, inputs.now, max_chars))


def make_section(role: Role):
    """The callable Hermes registers: reads ``info["session_id"]``, never raises. Hermes passes
    ``info`` as a read-only mapping, not a dict."""

    def section(info: Mapping[str, Any], **_: Any) -> str:
        session_id = info["session_id"]
        ctx = None
        try:
            from research_core.tools.base import ToolContext

            ctx = ToolContext.from_env(role, profile_env(IDE_SESSION_ID=session_id))
            return render(ctx, session_id, role=role)
        except Exception as error:
            text = f"Briefing unavailable: {type(error).__name__}: {error}"
            return ctx.scrub(text) if ctx is not None else text

    return section


# --- assembling and fitting ----------------------------------------------------------------


def _inputs(ctx: ToolContext, role: str, session_id: str, now: datetime) -> Inputs:
    from research_core.store import sessions

    coworld = ctx.coworld_or_none()
    store = ctx.store if coworld is not None else None
    session = sessions.read(ctx.wstore, session_id) if session_id else None
    return Inputs(
        ctx=ctx,
        role=role,
        now=now,
        wstore=ctx.wstore,
        store=store,
        coworld_id=None if coworld is None else coworld.coworld_id,
        session=session,
    )


def _item(label: str, build: Any, inputs: Inputs) -> Section:
    """Build one item; a failure becomes one line and the other items still render."""
    try:
        return build(inputs)
    except Exception as error:
        return Section(lines=[f"{label}: unavailable: {type(error).__name__}"])


def _fit(sections: list[Section], now: datetime, max_chars: int) -> str:
    """Cut entries from the lowest-priority section first until the text fits."""
    heading = (
        f"[briefing] Written {now:%Y-%m-%d %H:%M} UTC and not updated in this Session: read the"
        " record when the current state matters."
    )
    text = _text(heading, sections)
    while len(text) > max_chars:
        cuttable = [section for section in sections if section.remaining > 0]
        if not cuttable:
            return text[: max_chars - len(TRUNCATED) - 1].rstrip() + "\n" + TRUNCATED
        cuttable[-1].cut += 1
        text = _text(heading, sections)
    return text


def _text(heading: str, sections: list[Section]) -> str:
    lines = [heading]
    for section in sections:
        lines.extend(section.render())
    return "\n".join(lines)


# --- the items, in priority order ----------------------------------------------------------


def _workspace_status(inputs: Inputs) -> Section:
    from research_core.store import settings

    coworld = inputs.coworld_id or ("none; the frontend must open a session for a Coworld")
    paused = "yes" if settings.get(inputs.wstore, "paused") else "no"
    heartbeat = inputs.wstore.get_meta("heartbeat")
    if heartbeat:
        watcher = f"watcher heartbeat {_age(heartbeat, inputs.now)}"
        if (inputs.wstore.get_meta("has_work") or "").lower() in ("true", "1", "yes"):
            watcher += ", work pending"
    else:
        watcher = "no heartbeat yet: background work is not running"
    player = inputs.ctx.player_id()
    lines = [
        f"Workspace: Coworld {coworld}; paused: {paused}; {watcher}; credits: {_credits(inputs)}",
        f"Your Player: id {player}"
        if player
        else "Your Player: id not known yet; platform.results gives it",
    ]
    lines[:0] = _identity_lines(inputs)
    stale = _cli_staleness(inputs.wstore)
    if stale:
        lines.append(stale)
    setup = _setup_status(inputs)
    if setup:
        lines.append(setup)
    if inputs.store is None:
        lines.append("No session Coworld: open a Coworld session to access game work.")
    return Section(lines=lines)


def _identity_lines(inputs: Inputs) -> list[str]:
    """The cogent's one workspace-level name and whether it has met the user (store.identity)."""
    from research_core.store import settings

    name = settings.get(inputs.wstore, "identity.name")
    lines = []
    if name:
        if settings.get(inputs.wstore, "identity.name_confirmed"):
            note = ""
        elif settings.get(inputs.wstore, "identity.offer_made"):
            note = " (chosen at setup; rename offer made)"
        else:
            note = (
                " (chosen at setup, not yet confirmed by the user: offer once to change it,"
                " then workspace.identity {offer_made})"
            )
        lines.append(f"Name: {_clip(name, SUMMARY_CHARS)}{note}")
    elif inputs.role == "orchestrator":
        lines.append(
            "No name yet (setup chooses one; otherwise in your first chat with the user choose"
            " one with them: workspace.identity {name, cites})"
        )
    if inputs.role == "orchestrator" and not settings.get(inputs.wstore, "identity.met_user"):
        lines.append(
            "Met the user: no (introduce yourself briefly and ask their name and what they enjoy,"
            " save it, then workspace.identity {met_user, cites} once they reply)"
        )
    return lines


def _setup_status(inputs: Inputs) -> str | None:
    """Only the Orchestrator's own workspace.coworld_switch starts setup, so say when it is due."""
    if inputs.coworld_id is None or inputs.role != "orchestrator":
        return None
    row = inputs.wstore.one(
        "SELECT set_up_at FROM coworlds WHERE coworld_id = ?", (inputs.coworld_id,)
    )
    if row is not None and row["set_up_at"]:
        return None
    started = inputs.wstore.one(
        "SELECT 1 FROM events WHERE kind = 'coworld.setup_needed' AND subject = ?",
        (inputs.coworld_id,),
    )
    if started:
        return "Coworld setup: started, not finished"
    # The imperative is measured: 7 of 8 Kimi runs set up before building, against 1 of 8 for
    # "not started; workspace.coworld_switch starts it".
    return (
        "Coworld setup: not started. Call workspace.coworld_switch first: nothing builds before it."
    )


def _credits(inputs: Inputs) -> str:
    credits = _meta_json(inputs.store, "credits") if inputs.store is not None else None
    if not isinstance(credits, dict) or credits.get("balance") is None:
        return "unknown"
    return f"{credits['balance']}"


def _cli_staleness(wstore: Store) -> str | None:
    """A7.8: "installed X, lock Y, lock refreshed <date>" for every CLI behind the lock."""
    versions = _meta_json(wstore, "cli_versions")
    if not isinstance(versions, dict):
        return None
    stale = [
        f"{name} installed {entry.get('installed')}, lock {entry.get('lock')}"
        for name, entry in versions.items()
        if isinstance(entry, dict) and entry.get("installed") != entry.get("lock")
    ]
    if not stale:
        return None
    refreshed = versions.get("lock_refreshed") or "unknown"
    return f"CLI versions: {'; '.join(stale)}; lock refreshed {refreshed}"


def _capabilities(inputs: Inputs) -> Section:
    from research_core.store import capabilities

    lines: list[str] = []
    entries: list[str] = []
    if inputs.store is not None:
        rows = capabilities.status(inputs.store)
        checked = inputs.store.get_meta("checks_at") or max(
            (row["last_check"] for row in rows if row["last_check"]), default=None
        )
        broken = [row for row in rows if row["status"] == "broken"]
        if checked is None:
            lines.append("Capability check: none yet (the watcher runs it on its next tick)")
        else:
            ok = sum(1 for row in rows if row["status"] == "ok")
            lines.append(f"Capability check: {_age(checked, inputs.now)}; {ok} of {len(rows)} ok")
        for row in broken:
            task_id = row["maintenance_task_id"]
            repair = f"repair T-{task_id}" if task_id else "no repair Task yet"
            entries.append(f"broken: {row['name']} ({repair})")
    if inputs.role == "orchestrator":
        provisioned = bool(inputs.ctx.env.get("SOFTMAX_USER_CREDENTIAL"))
        lines.append(
            "User identity: available"
            if provisioned
            else "User identity: not provisioned (User-identity actions report it)"
        )
    return Section(lines=lines, entries=entries, more="read with workspace.capabilities")


def _directives(inputs: Inputs) -> Section:
    from research_core.store import directives

    found = [(d, "workspace") for d in directives.list(inputs.wstore, scope="workspace")]
    goal_id = inputs.goal_id
    if inputs.store is not None and goal_id is not None:
        scope = f"goal:{goal_id}"
        found += [(d, f"G-{goal_id}") for d in directives.list(inputs.store, scope=scope)]
    if not found:
        return Section(lines=["Directives: none active"])
    entries = [f'{d["id"]} ({scope}): "{_clip(d["text"], DIRECTIVE_CHARS)}"' for d, scope in found]
    return Section(
        lines=[f"Directives ({len(found)} active, the user's standing instructions):"],
        entries=entries,
        more="list with directive.list",
    )


def _champion_and_versions(inputs: Inputs) -> Section:
    from research_core.store import versions

    if inputs.store is None:
        return Section()
    lines: list[str] = []
    rows = inputs.store.q(
        "SELECT league_id, state, policy_version_id, version_id, since FROM champion"
        " ORDER BY league_id"
    )
    for row in rows:
        if row["state"] == "none":
            lines.append(f"Champion in {row['league_id']}: none (this Player has no membership)")
            continue
        version = f"V-{row['version_id']}" if row["version_id"] else "an outside policy version"
        since = f" since {str(row['since'])[:10]}" if row["since"] else ""
        lines.append(f"Champion in {row['league_id']}: {version} ({row['state']}{since})")
    if not rows:
        lines.append("Champion: none recorded yet")
    league_versions = inputs.store.get_meta("league_versions")
    if league_versions:
        lines.append(league_versions)
    recent = versions.list(inputs.store, limit=RECENT_VERSIONS)
    for record in recent:
        status = record.get("submission_status") or versions.submission_status(record)
        created = str(record["created"])[:10]
        lines.append(
            f'{record["id"]} {status}: "{_clip(record["summary"], TITLE_CHARS)}" ({created})'
        )
    if not recent:
        lines.append("Versions: none yet")
    return Section(lines=lines)


def _submission_window(inputs: Inputs) -> Section:
    from research_core.store import settings

    if inputs.store is None:
        return Section()
    stores = (inputs.wstore, inputs.store)
    if settings.get(stores, "submissions.mode") == "ask":
        return Section(lines=["Submissions: ask the user first (submissions.mode is ask)"])
    until = settings.get(stores, "submissions.until")
    if not until:
        return Section()
    state = "open until" if parse_iso(until) > inputs.now else "window closed at"
    line = f"Autonomous submissions: {state} {until}"
    max_count = settings.get(stores, "submissions.max_count")
    if max_count is not None:
        line += f", at most {max_count}"
    return Section(lines=[line])


def _session_task(inputs: Inputs) -> Section:
    from research_core.store import experiments, tasks

    session = inputs.session
    if not session or not session.get("task_id"):
        return Section()
    task_id = int(session["task_id"])
    shown = f"T-{task_id}"
    session_coworld = session.get("coworld_id")
    if session_coworld and session_coworld != inputs.coworld_id:
        return Section(
            lines=[
                f"Your Task: {shown} is in Coworld {session_coworld}, not this session's Coworld"
            ]
        )
    if inputs.store is None:
        return Section(lines=[f"Your Task: {shown} (no session Coworld to read it from)"])
    task = tasks.read(inputs.store, task_id)
    goal = f"G-{task['goal_id']}" if task["goal_id"] else "workspace maintenance"
    waiting = task.get("waiting_on")
    waits = f", waiting on {waiting.get('kind', '?')}" if isinstance(waiting, dict) else ""
    lines = [
        f'Your Task: {task["id"]} "{_clip(task["title"], TITLE_CHARS)}" — {task["type"]} for '
        f"{goal}, {task['status']}{waits}; read it with task.read before working"
    ]
    experiment_id = task.get("experiment_id")
    if not experiment_id:
        row = inputs.store.one(
            "SELECT id FROM experiments WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
        )
        experiment_id = None if row is None else row["id"]
    if experiment_id:
        experiment = experiments.read(inputs.store, experiment_id)
        summary = _clip(experiment["summary"], SUMMARY_CHARS)
        lines.append(f'Experiment: {experiment["id"]} "{summary}" ({experiment["status"]})')
    pending = tasks.pending_messages(inputs.store, task_id, "to_researcher")
    entries = [f'"{_clip(message["text"], SUMMARY_CHARS)}"' for message in pending]
    if pending:
        lines.append(f"Messages for you ({len(pending)} undelivered):")
    return Section(lines=lines, entries=entries, more="read with task.read")


def _goals(inputs: Inputs) -> Section:
    from research_core.store import goals

    if inputs.store is None:
        return Section()
    active = goals.list(inputs.store, status="active")
    lines: list[str] = []
    maintenance = goals.read(inputs.store, None)["tasks"]["open"]
    if maintenance:
        lines.append(f"Workspace maintenance: open {', '.join(maintenance)}")
    if not active:
        lines.append("Goals: none active")
        return Section(lines=lines)
    lines.insert(0, f"Active Goals ({len(active)}):")
    entries = [_goal_line(goals.read(inputs.store, parse_display_id(g["id"])[1])) for g in active]
    return Section(lines=lines, entries=entries, more="list with goal.list")


def _goal_line(goal: dict[str, Any]) -> str:
    stands = _clip(
        goal["where_it_stands"].strip().splitlines()[0] if goal["where_it_stands"] else "",
        SUMMARY_CHARS,
    )
    tasks = goal["tasks"]
    open_ids = tasks["open"]
    shown = f" ({', '.join(open_ids[:3])}{', …' if len(open_ids) > 3 else ''})" if open_ids else ""
    running = goal["experiments"]["running"]
    return (
        f'{goal["id"]} "{_clip(goal["title"], TITLE_CHARS)}": {stands or "no status yet"};'
        f" open {len(open_ids)}{shown}, done {tasks['complete']}, {running} running"
    )


def _preference_claims(inputs: Inputs) -> Section:
    from research_core.store import claims

    if inputs.store is None:
        return Section()
    found = claims.search(
        inputs.store,
        "",
        tags=["preference"],
        include_retired=False,
        limit=PREFERENCE_CLAIMS_LOADED,
    )
    if not found:
        return Section()
    entries = [f"{c['id']}: {_clip(c['summary'], SUMMARY_CHARS)}" for c in found]
    return Section(
        lines=["Earlier user preferences (older claims; directives and settings override them):"],
        entries=entries,
        more="search with claim.search",
        total=_count_tagged(inputs.store, ["preference"]),
    )


def _goal_claims(inputs: Inputs) -> Section:
    from research_core.store import claims, goals

    if inputs.store is None:
        return Section()
    goal_ids = [g["id"] for g in goals.list(inputs.store, status="active")]
    if not goal_ids:
        return Section()
    wanted = set(goal_ids)
    recent = claims.search(inputs.store, "", include_retired=False, limit=GOAL_CLAIMS_LOADED)
    entries = []
    for claim in recent:
        tagged = [tag for tag in claim["tags"] if tag in wanted]
        if tagged:
            entries.append(
                f"{claim['id']} ({', '.join(tagged)}): {_clip(claim['summary'], SUMMARY_CHARS)}"
            )
    if not entries:
        return Section(lines=["Goal claims: none yet (tag claims with the Goal id, such as G-1)"])
    return Section(
        lines=["Recent claims on active Goals:"],
        entries=entries,
        more="search with claim.search",
        total=_count_tagged(inputs.store, goal_ids),
    )


# --- helpers -------------------------------------------------------------------------------


def _count_tagged(store: Store, tags: list[str]) -> int:
    placeholders = ", ".join("?" for _ in tags)
    return store.one(
        "SELECT COUNT(DISTINCT c.id) FROM claims c, json_each(c.tags)"
        f" WHERE c.status = 'active' AND json_each.value IN ({placeholders})",
        tags,
    )[0]


def _meta_json(store: Store, key: str) -> Any:
    """A JSON ``meta`` value, or ``None`` when absent or not JSON (another process wrote it)."""
    text = store.get_meta(key)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _age(iso: str, now: datetime) -> str:
    seconds = (now - parse_iso(iso)).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)} min ago"
    hours = minutes / 60
    if hours < 36:
        return f"{round(hours)} h ago"
    return f"{round(hours / 24)} d ago"


def _clip(text: Any, limit: int) -> str:
    """One line, at most ``limit`` characters, ending in an ellipsis when cut."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
