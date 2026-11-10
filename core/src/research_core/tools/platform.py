"""The ``platform`` tool (design C.2): every platform read the agents need, in one tool.

Leagues, divisions and standings; a league's active policies (with ``exclude_player`` and
``exclude_user`` the opponents fall out); policy versions and policy files; a Player's results;
episodes and replays; credits; the champion; forum and wiki reads; a Coworld's manifest.
Every call goes through :func:`research_core.platform.wait.call_platform`, so a persistent
platform error puts a task Session's Task on ``waiting_on: platform`` and the refusal names the
route, status and request id. Both roles have the tool; the Researcher acts as the Player only
(``identity: user`` and ``exclude_user`` are refused with the reason, and ``policy_file`` does not
fall back to the User identity).

Files land under the Coworld's ``documents/`` folder: ``policies/<policy version id>.txt``
for UTF-8 text (``.bin`` for binary files) and
``replays/<episode>.replay`` (pinned as ``replay`` / ``episode:<id>``). Results are plain dicts.
Runs in the workspace environment (``executor="workspace"``).
"""

from __future__ import annotations

import codecs
import hashlib
from dataclasses import asdict
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from research_core import refs
from research_core.platform.errors import IdentityUnavailable
from research_core.platform.protocol import Identity, Membership, Platform, PolicyVersion
from research_core.platform.wait import call_platform, identity_for
from research_core.store import pins
from research_core.tools import _args as a
from research_core.tools.base import (
    INLINE_CHARS,
    ActionSpec,
    ToolCall,
    ToolContext,
    ToolError,
    ToolSpec,
    tool,
)

BOTH = frozenset({"orchestrator", "researcher"})
# ``policy_file`` falls back to the User identity when the Player gets one of these.
PLAYER_REFUSED_STATUSES = frozenset({401, 403, 404})
# The file route's answer for a version that has no file, such as an image policy.
NO_FILE_STATUS = 409
# ``episodes {league}`` reads the episodes of at most this many active policies.
LEAGUE_EPISODE_POLICIES = 10

LEAGUE = a.string("The League id (league_...) or a platform URL carrying it.")
IDENTITY = a.string("Default player.", enum=["player", "user"])


def handle(call: ToolCall) -> dict:
    handler = ACTIONS[call.action or ""]
    return handler(call, call.ctx.platform())


# --- leagues, divisions, standings --------------------------------------------------------


def leagues(call: ToolCall, platform: Platform) -> dict:
    game = _id(call.get("game"))
    rows = _call(call, "GET /v2/leagues", lambda: platform.leagues(game_id=game))
    return {"leagues": [asdict(row) for row in rows]}


def league(call: ToolCall, platform: Platform) -> dict:
    league_id = _id(call.get("league"))
    row = _call(call, f"GET /v2/leagues/{league_id}", lambda: platform.league(league_id))
    return {"league": asdict(row)}


def divisions(call: ToolCall, platform: Platform) -> dict:
    league_id = _id(call.get("league"))
    rows = _call(call, "GET /v2/divisions", lambda: platform.divisions(league_id))
    return {"league": league_id, "divisions": rows}


def standings(call: ToolCall, platform: Platform) -> dict:
    division_id, league_id = _id(call.get("division")), _id(call.get("league"))
    if not division_id and not league_id:
        raise ToolError("standings needs a division or a league")
    if division_id:
        return {"division": division_id, "standings": _standings(call, platform, division_id)}
    rows: list[dict] = []
    found = _call(call, "GET /v2/divisions", lambda: platform.divisions(league_id))
    for division in found:
        rows.extend(_standings(call, platform, division["id"]))
    return {"league": league_id, "divisions": found, "standings": rows}


def _standings(call: ToolCall, platform: Platform, division_id: str) -> list[dict]:
    rows = _call(
        call,
        f"GET /v2/divisions/{division_id}/leaderboard",
        lambda: platform.standings(division_id),
    )
    for row in rows:
        row.setdefault("division_id", division_id)
    return rows


# --- policies -----------------------------------------------------------------------------


def active_policies(call: ToolCall, platform: Platform) -> dict:
    """The league's active memberships, minus this Player's and, on request, every Player of
    the user (``exclude_user`` needs the User identity and lists the user's Players)."""
    league_id = _id(call.get("league"))
    rows = _call(
        call,
        "GET /v2/league-policy-memberships",
        lambda: platform.memberships(league_id=league_id, active_only=True),
    )
    excluded_players: set[str] = set()
    result: dict[str, Any] = {
        "league": league_id,
        "excluded": {"player": None, "user_players": None},
    }
    if call.get("exclude_player") or call.get("exclude_user"):
        this_player = _this_player(call, platform)
        excluded_players.add(this_player)
        result["excluded"]["player"] = this_player
    if call.get("exclude_user"):
        identity_for(call.ctx, "user")
        user_players = _user_players(call, platform)
        if user_players is None:
            result["note"] = (
                "the platform answered 404 for GET /players as the User, so only this Player's"
                " policies were excluded"
            )
        else:
            excluded_players.update(user_players)
            result["excluded"]["user_players"] = sorted(user_players)
    result["memberships"] = [
        _membership(row) for row in rows if row.player_id not in excluded_players
    ]
    return result


def _user_players(call: ToolCall, platform: Platform) -> set[str] | None:
    """The ids of the user's Players (``GET /players`` as the User); None when there is no
    such route (the fake platform), which the caller reports."""
    try:
        answer = _call(
            call,
            "GET /players",
            lambda: platform.request("GET", "/players", identity="user"),
            identity="user",
        )
    except ToolError as error:
        if error.context.get("status") == 404:
            return None
        raise
    entries = answer.get("entries") if isinstance(answer, dict) else None
    return {
        str(entry["id"]) for entry in entries or () if isinstance(entry, dict) and "id" in entry
    }


def policy_versions(call: ToolCall, platform: Platform) -> dict:
    identity = identity_for(call.ctx, call.get("identity"))
    player, name = _id(call.get("player")), call.get("name")
    rows = _call(
        call,
        "GET /stats/policy-versions",
        lambda: platform.policy_versions(
            name=name, player_id=player, mine=player is None, identity=identity
        ),
        identity=identity,
    )
    return {"identity": identity, "policy_versions": [_policy_version(row) for row in rows]}


def policy_file(call: ToolCall, platform: Platform) -> dict:
    """Download a policy version's file, which only its owner can read: the Player first, the User
    when the Player is refused (A4.6; the Orchestrator only). Written to
    ``documents/policies/<id>.txt`` for UTF-8 text, or ``.bin`` for binary files."""
    ref = _id(call.get("policy_version"))
    tried: list[str] = []
    content: bytes | None = None
    used: Identity = "player"
    last: ToolError | None = None
    for identity in _policy_file_identities(call.ctx):
        tried.append(identity)
        try:
            pv = _policy_version_id(call, platform, ref, identity)
            content = call_platform(
                call.ctx,
                f"GET /v2/policy-files/{pv}/download",
                partial(platform.policy_file, pv, identity=identity),
                identity=identity,
            )
        except ToolError as error:
            if isinstance(error.__cause__, IdentityUnavailable) and last is not None:
                tried.pop()  # no User to ask: the Player's refusal is the answer
                break
            if error.context.get("status") == NO_FILE_STATUS:
                raise ToolError(
                    f"{ref} has no file to download; an image policy has none",
                    **error.context,
                    next="use the code of the Version it came from",
                ) from error
            if error.context.get("status") not in PLAYER_REFUSED_STATUSES:
                raise
            last = error
            continue
        used = identity
        break
    if content is None:
        assert last is not None
        problems = list(last.problems)
        if call.ctx.role == "researcher":
            problems.append(
                "the Player was refused; the User identity could read it and belongs to the"
                " Orchestrator (ask through task_ask)"
            )
        else:
            problems.append(f"refused as {' and as '.join(tried)}")
        raise ToolError(problems, **last.context, tried=tried)
    block_size = 64 * 1024
    chunks = (
        content[offset : offset + block_size] for offset in range(0, len(content), block_size)
    )
    is_text = all(
        character.isprintable() or character in "\t\n\r"
        for text in codecs.iterdecode(chunks, "utf-8-sig", errors="surrogateescape")
        for character in text
    )
    suffix = ".txt" if is_text else ".bin"
    path = _documents(call.ctx) / "policies" / f"{pv}{suffix}"
    _write(path, content)
    return {
        "policy_version": pv,
        "path": str(path),
        "content_type": "text/plain; charset=utf-8" if is_text else "application/octet-stream",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "identity": used,
    }


def _policy_file_identities(ctx: ToolContext) -> tuple[Identity, ...]:
    return ("player",) if ctx.role == "researcher" else ("player", "user")


def _policy_version_id(call: ToolCall, platform: Platform, ref: str, identity: Identity) -> str:
    """The id behind ``ref``: the file route takes only an id, so a ``name:v3`` label is looked up
    among what this identity owns."""
    if refs.parse(ref).kind != "policy_label":
        return ref
    name, _, number = ref.rpartition(":v")
    rows = _call(
        call,
        "GET /stats/policy-versions",
        partial(platform.policy_versions, name=name, identity=identity),
        identity=identity,
    )
    for row in rows:
        if row.version == int(number):
            return row.id
    raise ToolError(f"no policy version {ref} among this {identity}'s uploads", status=404)


# --- results, episodes, replays -----------------------------------------------------------


def results(call: ToolCall, platform: Platform) -> dict:
    """A Player's standing rows and memberships, per league (the session Coworld's leagues when
    none is named)."""
    player = _id(call.get("player")) or _this_player(call, platform)
    league_id = _id(call.get("league"))
    if league_id:
        found = [_call(call, f"GET /v2/leagues/{league_id}", lambda: platform.league(league_id))]
    else:
        found = _coworld_leagues(call, platform)
    per_league = []
    for row in found:
        memberships = _call(
            call,
            "GET /v2/league-policy-memberships",
            partial(platform.memberships, league_id=row.id, player_id=player),
        )
        standing_rows = []
        for division in _call(call, "GET /v2/divisions", partial(platform.divisions, row.id)):
            standing_rows.extend(
                r
                for r in _standings(call, platform, division["id"])
                if r.get("player_id") == player
            )
        per_league.append(
            {
                "league": asdict(row),
                "memberships": [_membership(m) for m in memberships],
                "standings": standing_rows,
            }
        )
    return {"player": player, "leagues": per_league}


def episodes(call: ToolCall, platform: Platform) -> dict:
    pv, league_id = _id(call.get("policy_version")), _id(call.get("league"))
    if not pv and not league_id:
        raise ToolError("episodes needs a policy_version or a league")
    limit = call.get("limit", 20)
    if limit not in range(1, 101):
        raise ToolError("limit must be between 1 and 100")
    cursor = call.get("cursor")
    if cursor and not pv:
        raise ToolError("cursor needs a policy_version; use a league result's next_cursors")
    if pv:
        page = _call(
            call,
            f"GET /v2/policy-versions/{pv}/episode-requests",
            lambda: platform.episodes_for_policy(pv, limit=limit, cursor=cursor),
        )
        return {"policy_version": pv, "episodes": page.entries, "next_cursor": page.next_cursor}
    memberships = _call(
        call,
        "GET /v2/league-policy-memberships",
        lambda: platform.memberships(league_id=league_id, active_only=True),
    )
    policies: list[str] = []
    for membership in memberships:
        if membership.policy_version.id not in policies:
            policies.append(membership.policy_version.id)
    seen: dict[str, dict] = {}
    next_cursors: dict[str, str] = {}
    for policy in policies[:LEAGUE_EPISODE_POLICIES]:
        route = f"GET /v2/policy-versions/{policy}/episode-requests"
        page = _call(call, route, partial(platform.episodes_for_policy, policy, limit=limit))
        if page.next_cursor:
            next_cursors[policy] = page.next_cursor
        for row in page.entries:
            seen.setdefault(row["id"], row)
    return {
        "league": league_id,
        "policy_versions": policies[:LEAGUE_EPISODE_POLICIES],
        "truncated": len(policies) > LEAGUE_EPISODE_POLICIES,
        "episodes": sorted(
            seen.values(),
            key=lambda row: datetime.fromisoformat(row["created_at"]),
            reverse=True,
        ),
        "next_cursors": next_cursors,
    }


def episode(call: ToolCall, platform: Platform) -> dict:
    episode_id = _id(call.get("episode"))
    row = _call(
        call, f"GET /v2/episode-requests/{episode_id}", lambda: platform.episode(episode_id)
    )
    return {"episode": row}


def replay(call: ToolCall, platform: Platform) -> dict:
    """Fetch an episode's replay once: the file lands in ``documents/replays/`` and a pin keyed
    ``replay`` / ``episode:<id>`` records it; a later call returns the pin."""
    episode_id = _id(call.get("episode"))
    ref = f"episode:{episode_id}"
    pinned = pins.find(call.ctx.store, "replay", ref)
    if pinned is not None and Path(pinned["download"]["path"]).is_file():
        path = pinned["download"]["path"]
        return {"episode": episode_id, "path": path, "pin": pinned, "existing": True}
    path = _replay_path(call.ctx, episode_id, call.get("dest"))
    content = _call(
        call,
        f"GET /v2/episode-requests/{episode_id}/artifacts/replay",
        lambda: platform.episode_artifact(episode_id, "replay"),
    )
    _write(path, content)
    download = {
        "path": str(path),
        "digest": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "artifact": "replay",
    }
    pin = pins.add(call.ctx.store, call.ctx.actor, kind="replay", ref=ref, download=download)
    return {"episode": episode_id, "path": str(path), "pin": pin, "existing": pin["existing"]}


# --- credits, champion, Coworld -----------------------------------------------------------


def credits(call: ToolCall, platform: Platform) -> dict:
    return {"credits": asdict(_call(call, "GET /usage/me/credits", platform.credits))}


def champion(call: ToolCall, platform: Platform) -> dict:
    """This Player's champion in a league: the representative, never the winner."""
    league_id = _id(call.get("league"))
    rows = _call(
        call,
        "GET /v2/league-policy-memberships",
        lambda: platform.memberships(league_id=league_id, mine=True, champions_only=True),
    )
    return {"league": league_id, "champion": _membership(rows[0]) if rows else None}


def coworld(call: ToolCall, platform: Platform) -> dict:
    coworld_id = _id(call.get("coworld"))
    row = _call(
        call, f"GET /v2/coworlds/{coworld_id}", lambda: platform.coworld_manifest(coworld_id)
    )
    game = ((row.get("manifest") or {}).get("game")) or {}
    runnable = game.get("runnable") or {}
    return {
        "coworld": {
            "id": row.get("id"),
            "name": row.get("name"),
            "version": row.get("version"),
            "player_runtime": game.get("player_runtime"),
            "source_url": runnable.get("source_url"),
            "commit": runnable.get("commit"),
        }
    }


# --- forum and wiki -----------------------------------------------------------------------


def forum_list(call: ToolCall, platform: Platform) -> dict:
    slug = call.get("slug") or default_forum_slug(call, platform)
    sort, limit = call.get("sort") or "hot", call.get("limit") or 20
    posts = _call(
        call,
        f"GET /v2/forums/{slug}/posts",
        lambda: platform.forum_list(slug, sort=sort, limit=limit),
    )
    return {"slug": slug, "sort": sort, "posts": posts}


def forum_read(call: ToolCall, platform: Platform) -> dict:
    post = _id(call.get("post"))
    text = _call(call, f"GET /v2/posts/{post}.md", lambda: platform.forum_read(post))
    return {"post": post, "markdown": text}


def forum_search(call: ToolCall, platform: Platform) -> dict:
    query, slug = call.get("query"), call.get("slug")
    hits = _call(call, "GET /v2/forums/search", lambda: platform.forum_search(query, slug=slug))
    return {"query": query, "slug": slug, "hits": hits}


def wiki_read(call: ToolCall, platform: Platform) -> dict:
    wiki, page = call.get("wiki"), call.get("page")
    row = _call(call, f"GET /v2/wikis/{wiki}/pages/{page}", lambda: platform.wiki_read(wiki, page))
    return {"page": row}


def wiki_search(call: ToolCall, platform: Platform) -> dict:
    wiki, query = call.get("wiki"), call.get("query")
    hits = _call(call, f"GET /v2/wikis/{wiki}/search", lambda: platform.wiki_search(wiki, query))
    return {"wiki": wiki, "query": query, "hits": hits}


def wiki_history(call: ToolCall, platform: Platform) -> dict:
    page_id = call.get("page_id")
    rows = _call(
        call, f"GET /v2/wiki-pages/{page_id}/revisions", lambda: platform.wiki_history(page_id)
    )
    return {"page_id": page_id, "revisions": rows}


def default_forum_slug(call: ToolCall, platform: Platform) -> str:
    """The forum of the session Coworld's first league (from its ``forum_markdown_url``)."""
    for row in _coworld_leagues(call, platform):
        if row.forum_markdown_url:
            return row.forum_markdown_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md")
    raise ToolError("slug is required: no league of the session Coworld names a forum")


# --- helpers ------------------------------------------------------------------------------


def _call[T](call: ToolCall, route: str, fn, *, identity: Identity = "player") -> T:
    return call_platform(call.ctx, route, fn, identity=identity)


def _id(value: Any) -> str | None:
    """A platform id given as itself or inside a platform URL; None when absent."""
    if value in (None, ""):
        return None
    ref = refs.parse(str(value))
    return ref.value if ref.kind not in ("unknown", "url", "record") else str(value).strip()


def _this_player(call: ToolCall, platform: Platform) -> str:
    """The workspace's Player id as the context knows it, else the platform's answer."""
    player = call.ctx.player_id()
    if player:
        return player
    who = _call(call, "GET /whoami", lambda: platform.whoami("player"))
    if not who.subject_id:
        raise ToolError("the platform did not name this Player (GET /whoami)", route="GET /whoami")
    return who.subject_id


def _coworld_leagues(call: ToolCall, platform: Platform) -> list:
    """The leagues of the session Coworld."""
    rows = _call(call, "GET /v2/leagues", platform.leagues)
    coworld_id = call.ctx.coworld().coworld_id
    return [row for row in rows if row.coworld_id == coworld_id]


def _documents(ctx: ToolContext) -> Path:
    return ctx.coworld().documents


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(content)
    tmp.replace(path)


def _replay_path(ctx: ToolContext, episode_id: str, dest: str | None) -> Path:
    """``dest`` is a file or folder, absolute or under documents/; default replays/<id>.replay."""
    name = f"{episode_id}.replay"
    if not dest:
        return _documents(ctx) / "replays" / name
    path = Path(dest) if Path(dest).is_absolute() else _documents(ctx) / dest
    return path / name if path.is_dir() or dest.endswith("/") else path


def _policy_version(row: PolicyVersion) -> dict:
    return {**asdict(row), "label": row.label}


def _membership(row: Membership) -> dict:
    record = asdict(row)
    record["policy_version"] = _policy_version(row.policy_version)
    return record


ACTIONS = {
    "leagues": leagues,
    "league": league,
    "divisions": divisions,
    "standings": standings,
    "active_policies": active_policies,
    "policy_versions": policy_versions,
    "policy_file": policy_file,
    "results": results,
    "episodes": episodes,
    "episode": episode,
    "replay": replay,
    "credits": credits,
    "champion": champion,
    "forum_list": forum_list,
    "forum_read": forum_read,
    "forum_search": forum_search,
    "wiki_read": wiki_read,
    "wiki_search": wiki_search,
    "wiki_history": wiki_history,
    "coworld": coworld,
}

SPEC = tool(
    ToolSpec(
        name="platform",
        description=(
            "Read the Softmax platform: leagues, standings, policies, results, episodes and "
            "replays, credits, the champion, the forum and wiki, a Coworld's manifest. Never "
            "changes the platform; writes have their own tools. A persistent platform error makes "
            "a task Session wait on it."
        ),
        handler=handle,
        roles=BOTH,
        executor="workspace",
        actions=(
            ActionSpec(
                "leagues",
                "List leagues, optionally of one game.",
                {"game": a.string("A game id (game_<uuid>, not its name) to filter by.")},
            ),
            ActionSpec("league", "One league.", {"league": LEAGUE}, ("league",)),
            ActionSpec("divisions", "A league's divisions.", {"league": LEAGUE}, ("league",)),
            ActionSpec(
                "standings",
                "The leaderboard of a division, or of every division of a league; give one.",
                {"division": a.string("The Division id (div_...)."), "league": LEAGUE},
                example_fields=("league",),
            ),
            ActionSpec(
                "active_policies",
                "A league's active memberships, with the opponents after the exclusions.",
                {
                    "league": LEAGUE,
                    "exclude_player": a.boolean("Drop this Player's policies."),
                    "exclude_user": a.boolean("Drop the policies of all the user's Players."),
                },
                ("league",),
                orchestrator_fields=("exclude_user",),
            ),
            ActionSpec(
                "policy_versions",
                "Policy versions: this Player's by default, a Player's with player, by name.",
                {
                    "player": a.string("A Player id (ply_...)."),
                    "name": a.string("A policy name; every version of it."),
                    "identity": IDENTITY,
                },
                orchestrator_fields=("identity",),
            ),
            ActionSpec(
                "policy_file",
                "Download the file of a policy version you own into documents/policies/ and "
                "return its path and content_type. UTF-8 text is saved as .txt for read_file; "
                "binary files use .bin. An image policy has none.",
                {"policy_version": a.string("The policy version id or name:v3 label.")},
                ("policy_version",),
            ),
            ActionSpec(
                "results",
                "A Player's standing rows and memberships per league (this Player by default).",
                {"player": a.string("A Player id (ply_...)."), "league": LEAGUE},
            ),
            ActionSpec(
                "episodes",
                "Recent episodes, newest first: one page per policy, default 20, maximum 100. "
                "Give policy_version, or league for up to ten active policies "
                "wherever they played. "
                "Continue a policy with next_cursor; league next_cursors names each policy's "
                "continuation, and truncated says more policies exist.",
                {
                    "policy_version": a.string("A policy version id."),
                    "league": LEAGUE,
                    "limit": a.integer("Episodes per policy (default 20, between 1 and 100)."),
                    "cursor": a.string("A policy's next_cursor from the previous page."),
                },
                example_fields=("league",),
            ),
            ActionSpec(
                "episode",
                "One episode request.",
                {"episode": a.string("The episode id (ereq_...).")},
                ("episode",),
            ),
            ActionSpec(
                "replay",
                "Fetch an episode's replay into documents/replays/ once (pinned); gives the path.",
                {
                    "episode": a.string("The episode id (ereq_...)."),
                    "dest": a.string("A file or folder, absolute or under documents/."),
                },
                ("episode",),
            ),
            ActionSpec("credits", "The account's XP credits: balance, refill, recent requests."),
            ActionSpec(
                "champion", "This Player's champion in a league.", {"league": LEAGUE}, ("league",)
            ),
            ActionSpec(
                "forum_list",
                "List forum posts (the session Coworld's league forum by default).",
                {
                    "slug": a.string("The forum slug."),
                    "sort": a.string("Default hot.", enum=["hot", "new", "top"]),
                    "limit": a.integer("How many posts (default 20)."),
                },
            ),
            ActionSpec(
                "forum_read",
                "A post with its comments, as Markdown.",
                {"post": a.string("The post id.")},
                ("post",),
            ),
            ActionSpec(
                "forum_search",
                "Search forum posts.",
                {"query": a.string("Search text."), "slug": a.string("Limit to one forum.")},
                ("query",),
            ),
            ActionSpec(
                "wiki_read",
                "Read a wiki page.",
                {"wiki": a.string("The wiki slug."), "page": a.string("The page slug.")},
                ("wiki", "page"),
            ),
            ActionSpec(
                "wiki_search",
                "Search a wiki.",
                {"wiki": a.string("The wiki slug."), "query": a.string("Search text.")},
                ("wiki", "query"),
            ),
            ActionSpec(
                "wiki_history",
                "A wiki page's revisions, newest first.",
                {"page_id": a.string("The wiki page id.")},
                ("page_id",),
            ),
            ActionSpec(
                "coworld",
                "A Coworld's manifest summary: id, name, version, player_runtime, source, commit.",
                {"coworld": a.string("The Coworld id (cow_...).")},
                ("coworld",),
            ),
        ),
        inline_chars=INLINE_CHARS,
        spill_hint="A smaller limit or a narrower filter returns a result that fits inline.",
    )
)
