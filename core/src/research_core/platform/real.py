"""The Softmax platform through the official ``coworld`` and ``softmax_client`` packages (C.4).

One :class:`RealPlatform` builds, lazily and once per identity, the three official client
surfaces (``CoworldApiClient``, ``CoworldUploadClient``, ``ForumWikiClient``) plus a raw
``observatory_client`` for routes the typed clients lack. Every HTTP call goes through
:meth:`RealPlatform._call`, which maps ``httpx`` failures to :class:`PlatformError`:

* an HTTP error status becomes ``PlatformError(route, status, request_id, detail)``, retryable
  by status (``errors.RETRYABLE_STATUSES``); 402 becomes :class:`CreditsRefused` with whatever
  balance/held/next-refill fields the body carries;
* a transport failure (connection, timeout) becomes a retryable ``PlatformError``;
* a response the client cannot parse becomes a non-retryable one.

Retries are the caller's (``protocol.with_retries``), never done here. Results are the
protocol's dataclasses or plain dicts (``model_dump(mode="json")``), never pydantic models.
``last_identity`` names the identity of the most recent call; forum and wiki writes also carry
``"identity"`` in their result.

Routes the installed clients do not have go through the raw client: the policy-file download, the
champion route, a post to a named forum and a wiki edit with no base revision.

Upload recovery (B.13) reads ``POST /stats/policies/lookup`` through the upload client
(``lookup_policy_upload``) with the exact inputs ``upload_inputs`` computed before the upload
(coworld's ``policy_upload_inputs``). The listing's hash column (``player_file_content_hash``)
is only served to team callers, so it is not a way for a Player to find its own upload.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
import pydantic
from coworld import api_client as coworld_api
from coworld.api_client import CoworldApiClient
from coworld.config import NEXT_CURSOR_HEADER
from coworld.episode_downloads import DownloadResult, download_episodes
from coworld.player_files import InvalidPlayerFile
from coworld.upload import CoworldUploadClient, _upload_container_image, policy_upload_inputs
from softmax_client.auth import fetch_cogames_whoami
from softmax_client.forum_wiki import (
    CreatePostRequest,
    ForumWikiClient,
    WikiEditConflictResponse,
    WikiEditPublic,
)
from softmax_client.http import observatory_client

from research_core.platform import identity as identities
from research_core.platform.errors import CreditsRefused, PlatformError
from research_core.platform.protocol import (
    AutoChampion,
    Credits,
    EpisodePage,
    Identity,
    League,
    Membership,
    PolicyVersion,
    Submission,
    WhoAmI,
    XpRequest,
)

DETAIL_LIMIT = 2000  # characters of an error body kept in PlatformError.detail
HTTP_TIMEOUT = 30.0  # the typed clients' default; the raw client gets the same
UPLOAD_TIMEOUT = 600.0  # the presigned PUT of a policy file (coworld's own value)
UPLOAD_CONTENT_TYPE = "application/octet-stream"
PAGE_SIZE = 100
MAX_PAGES = 50  # a listing longer than this is a bug in the filter, not a result to return


class RealPlatform:
    """The platform behind the workspace's Player (and, when provisioned, the user's User)."""

    def __init__(self, env: Mapping[str, str] | None = None):
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._server = identities.server(self._env)
        self._stack = ExitStack()
        self._api_clients: dict[Identity, CoworldApiClient] = {}
        self._upload_clients: dict[Identity, CoworldUploadClient] = {}
        self._forum_clients: dict[Identity, ForumWikiClient] = {}
        self._http_clients: dict[Identity, httpx.Client] = {}
        self.last_identity: Identity | None = None
        self._known_player_id: str | None = None

    @property
    def server(self) -> str:
        return self._server

    def close(self) -> None:
        self._stack.close()

    def __enter__(self) -> RealPlatform:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- clients and the one error boundary -------------------------------------------------

    def _client[T](
        self, cache: dict[Identity, T], identity: Identity, build: Callable[[str], T]
    ) -> T:
        self.last_identity = identity
        if identity not in cache:
            token = identities.token_for(self._env, identity)
            cache[identity] = self._stack.enter_context(build(token))
        return cache[identity]

    def _api(self, identity: Identity = "player") -> CoworldApiClient:
        return self._client(
            self._api_clients,
            identity,
            lambda token: CoworldApiClient(server_url=self._server, token=token),
        )

    def _upload(self, identity: Identity = "player") -> CoworldUploadClient:
        return self._client(
            self._upload_clients,
            identity,
            lambda token: CoworldUploadClient(server_url=self._server, token=token),
        )

    def _forum(self, identity: Identity = "player") -> ForumWikiClient:
        return self._client(
            self._forum_clients,
            identity,
            lambda token: ForumWikiClient(server=self._server, token=token),
        )

    def _http(self, identity: Identity = "player") -> httpx.Client:
        return self._client(
            self._http_clients,
            identity,
            lambda token: observatory_client(
                server=self._server, token=token, timeout=HTTP_TIMEOUT
            ),
        )

    def _call[T](self, route: str, fn: Callable[[], T]) -> T:
        """Run one client call; map httpx failures to PlatformError. Retries are the caller's."""
        try:
            return fn()
        except httpx.HTTPStatusError as error:
            raise _status_error(route, error.response) from error
        except httpx.TransportError as error:
            detail = f"{type(error).__name__}: {error}".strip(": ")
            raise PlatformError(detail, route=route, retryable=True) from error
        except pydantic.ValidationError as error:
            detail = f"unexpected response shape: {error}"[:DETAIL_LIMIT]
            raise PlatformError(detail, route=route, retryable=False) from error

    def _pages(self, route: str, fetch: Callable[[str | None], Any]) -> list[Any]:
        """Every entry of a cursor-paged listing; ``fetch(cursor)`` returns one page model."""
        rows: list[Any] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            page = self._call(route, partial(fetch, cursor))
            rows.extend(page.entries)
            cursor = page.next_cursor
            if not cursor:
                return rows
        raise PlatformError(f"listing ran past {MAX_PAGES} pages", route=route, retryable=False)

    # -- identity and credits ----------------------------------------------------------------

    def _player_id(self) -> str | None:
        """``SOFTMAX_PLAYER_ID``, else what one ``whoami`` names: hosting does not set the variable.
        None when the credential is not a Player's."""
        if self._known_player_id is None:
            self._known_player_id = identities.player_id(self._env)
        if self._known_player_id is None:
            who = self.whoami()
            if who.kind == "player":
                self._known_player_id = who.subject_id
        return self._known_player_id

    def whoami(self, identity: Identity = "player") -> WhoAmI:
        token = identities.token_for(self._env, identity)
        self.last_identity = identity
        me = self._call(
            "GET /whoami", lambda: fetch_cogames_whoami(api_server=self._server, token=token)
        )
        expires_at = None
        if identity == "player":
            expires_at = identities.player_credential(self._env).expires_at
        return WhoAmI(
            kind=cast(Any, me.subject_type),
            subject_id=me.subject_id,
            email=me.user_email,
            owner_user_id=me.owner_user_id,
            expires_at=expires_at,
        )

    def credits(self) -> Credits:
        me = self._call("GET /usage/me/credits", self._api().get_my_credits)
        status = me.status
        return Credits(
            balance=status.balance_credits,
            max=status.max_balance_credits,
            refill_amount=status.refill_credits,
            next_refill_at=_iso(status.next_refill_at),
            spent_this_period=status.spent_this_period_credits,
            recent_requests=[_dump(row) for row in me.recent_requests],
        )

    # -- policies ----------------------------------------------------------------------------

    def upload_file_policy(
        self, *, name: str, content: bytes, tags: dict[str, str] | None = None
    ) -> PolicyVersion:
        """coworld's own three-step upload: request a ticket, PUT the exact bytes, complete.

        The backend verifies size and digest on ``complete``. Should a client version ever keep
        a ``content_hash`` on the returned version, a value other than ours is refused.
        """
        client = self._upload()
        player_id = self._player_id()  # before the bytes leave: a failure here costs nothing
        content_hash = hashlib.sha256(content).hexdigest()
        body = {
            "name": name,
            "content_hash": content_hash,
            "size_bytes": len(content),
            "tags": tags,
        }
        ticket = self._call(
            "POST /stats/policies/files/upload",
            lambda: client.request_player_file_upload(**body),
        )
        if ticket.existing_policy_version is None and ticket.upload_url is not None:
            upload_url = ticket.upload_url
            self._call(
                f"PUT {_without_query(upload_url)}",
                lambda: httpx.put(
                    upload_url,
                    content=content,
                    headers={"Content-Type": UPLOAD_CONTENT_TYPE},
                    timeout=UPLOAD_TIMEOUT,
                ).raise_for_status(),
            )
        version = self._call(
            "POST /stats/policies/files/complete",
            lambda: client.complete_player_file_policy(**body),
        )
        reported = (version.model_extra or {}).get("content_hash")
        if isinstance(reported, str) and reported.lower() != content_hash:
            raise PlatformError(
                f"the platform stored {version.name}:v{version.version} with content hash "
                f"{reported}, but the uploaded bytes hash to {content_hash}",
                route="POST /stats/policies/files/complete",
                retryable=False,
            )
        return PolicyVersion(
            id=version.id,
            name=version.name,
            version=version.version,
            content_hash=content_hash,
            player_id=player_id,
        )

    def upload_image_policy(
        self, *, name: str, archive_path: Path, tags: dict[str, str] | None = None
    ) -> PolicyVersion:
        """Use the same archive upload and completion path as coworld upload-policy."""
        client = self._upload()
        player_id = self._player_id()
        uploaded = self._call(
            "container image upload", lambda: _upload_container_image(client, archive_path)
        )
        version = self._call(
            "POST /stats/policies/docker/complete",
            lambda: client.complete_docker_image_policy(
                name=name, container_image_id=uploaded.id, run=None, secret_env=None, tags=tags
            ),
        )
        return PolicyVersion(
            id=version.id,
            name=version.name,
            version=version.version,
            player_id=player_id,
        )

    def upload_inputs(
        self, *, name: str, artifact: Path, image_archive: bool = False
    ) -> dict[str, Any]:
        """coworld's exact deduplication inputs for a file or image archive upload.

        The lookup route requires the Player id, while the upload route defaults it from the token.
        """
        player_id = self._player_id()
        if not player_id:
            raise PlatformError(
                "the Player id is unknown (SOFTMAX_PLAYER_ID is unset and whoami named no Player);"
                " the upload lookup needs it",
                retryable=False,
            )
        try:
            return policy_upload_inputs(
                artifact=artifact, name=name, player_id=player_id, image_archive=image_archive
            )
        except InvalidPlayerFile as error:
            raise PlatformError(str(error), retryable=False) from error

    def lookup_policy_upload(self, inputs: dict[str, Any]) -> PolicyVersion | None:
        """``POST /stats/policies/lookup``: the version these exact inputs made, or None.

        None is an HTTP 200 with a null body: the platform's definite answer that no committed
        version matches. A 403 (not this Player's upload), a 404 (unknown player), a 422 (bad
        inputs) or an outage raises instead.
        """
        client = self._upload()
        found = self._call(
            "POST /stats/policies/lookup", lambda: client.lookup_policy_upload(inputs)
        )
        if found is None:
            return None
        return PolicyVersion(
            id=found.id,
            name=found.name,
            version=found.version,
            content_hash=inputs.get("file_hash"),
            player_id=inputs.get("player_id"),
        )

    def policy_versions(
        self,
        *,
        name: str | None = None,
        player_id: str | None = None,
        mine: bool = True,
        identity: Identity = "player",
    ) -> list[PolicyVersion]:
        """``GET /stats/policy-versions`` with ``name_exact`` when a name is given.

        coworld's ``lookup_policy_version`` sends the same query but keeps only the first row;
        callers here need every version of a name. The rows' ``player_file_content_hash`` is
        served to team callers only: a Player sees ``content_hash`` None (upload recovery goes
        through :meth:`lookup_policy_upload` instead).
        """
        http = self._http(identity)
        params: dict[str, str | int] = {"mine": str(mine).lower(), "limit": PAGE_SIZE}
        if name is not None:
            params["name_exact"] = name
        if player_id is not None:
            params["player_id"] = player_id

        def fetch(cursor: str | None) -> coworld_api.PolicyVersionsResponse:
            query = {**params, "cursor": cursor} if cursor else params
            response = _ok(http.get("/stats/policy-versions", params=query))
            page = coworld_api.PolicyVersionsResponse.model_validate(response.json())
            if page.next_cursor is None:
                page.next_cursor = response.headers.get(NEXT_CURSOR_HEADER)
            return page

        rows = self._pages("GET /stats/policy-versions", fetch)
        return [_policy_version_row(row) for row in rows]

    def policy_file(self, policy_version_id: str, identity: Identity = "player") -> bytes:
        """The owner-bound download. The route answers with a URL valid for five minutes, 404 for a
        version the caller does not own and 409 for one with no file, such as an image policy."""
        http = self._http(identity)
        path = f"/v2/policy-files/{_uuid(policy_version_id)}/download"
        route = f"GET {path}"
        ticket = self._call(route, lambda: _ok(http.get(path)).json())
        url = ticket["download_url"]
        content = self._call(
            f"GET {_without_query(url)}",
            lambda: _ok(httpx.get(url, timeout=UPLOAD_TIMEOUT)).content,
        )
        if hashlib.sha256(content).hexdigest() != ticket["content_hash"]:
            raise PlatformError(
                f"the downloaded file of {policy_version_id} does not match the platform's hash",
                route=route,
                retryable=False,
            )
        return content

    # -- leagues, standings, memberships, submissions ----------------------------------------

    def leagues(self, *, game_id: str | None = None) -> list[League]:
        rows = self._call("GET /v2/leagues", lambda: self._api().list_leagues(game_id=game_id))
        return [_league(row) for row in rows]

    def league(self, league_id: str) -> League:
        row = self._call(f"GET /v2/leagues/{league_id}", lambda: self._api().get_league(league_id))
        return _league(row)

    def divisions(self, league_id: str) -> list[dict]:
        rows = self._call(
            "GET /v2/divisions", lambda: self._api().list_divisions(league_id=league_id)
        )
        return [_dump(row) for row in rows]

    def standings(self, division_id: str) -> list[dict]:
        rows = self._call(
            f"GET /v2/divisions/{division_id}/leaderboard",
            lambda: self._api().get_division_leaderboard(division_id),
        )
        return [_dump(row) for row in rows]

    def memberships(
        self,
        *,
        league_id: str | None = None,
        player_id: str | None = None,
        policy_version_id: str | None = None,
        active_only: bool = False,
        mine: bool = False,
        champions_only: bool = False,
    ) -> list[Membership]:
        api = self._api()
        policy_version = _uuid(policy_version_id) if policy_version_id else None

        def fetch(cursor: str | None) -> coworld_api.LeaguePolicyMembershipPage:
            return api.list_memberships(
                league_id=league_id,
                player_id=player_id,
                policy_version_id=policy_version,
                active_only=active_only,
                champions_only=champions_only,
                mine=mine,
                limit=PAGE_SIZE,
                cursor=cursor,
            )

        rows = self._pages("GET /v2/league-policy-memberships", fetch)
        return [_membership(row) for row in rows]

    def submissions(
        self,
        *,
        league_id: str | None = None,
        player_id: str | None = None,
        policy_version_id: str | None = None,
        mine: bool = False,
    ) -> list[Submission]:
        api = self._api()
        policy_version = _uuid(policy_version_id) if policy_version_id else None

        def fetch(cursor: str | None) -> coworld_api.LeagueSubmissionPage:
            return api.list_submissions(
                league_id=league_id,
                player_id=player_id,
                policy_version_id=policy_version,
                mine=mine,
                limit=PAGE_SIZE,
                cursor=cursor,
            )

        rows = self._pages("GET /v2/league-submissions", fetch)
        return [_submission(row) for row in rows]

    def submit(
        self,
        *,
        league_id: str,
        policy_version_id: str,
        auto_champion: AutoChampion = "always",
        player_id: str | None = None,
    ) -> Submission:
        """Submit as the workspace's Player unless a player is named."""
        api = self._api()
        policy_version = _uuid(policy_version_id)
        as_player = player_id or self._player_id()
        row = self._call(
            "POST /v2/league-submissions",
            lambda: api.submit_to_league(
                league_id,
                policy_version,
                auto_champion=coworld_api.AutoChampion(auto_champion),
                player_id=as_player,
            ),
        )
        return _submission(row)

    def set_champion(self, membership_id: str) -> dict:
        """The champion route has no client method yet; it goes through :meth:`request`."""
        return self.request("POST", f"/v2/league-policy-memberships/{membership_id}/champion")

    # -- experience requests and episodes ----------------------------------------------------

    def xp_create(self, body: dict) -> XpRequest:
        row = self._call(
            "POST /v2/experience-requests", lambda: self._api().create_experience_request(body)
        )
        return _xp(row, title=body.get("title"))

    def xp_get(self, xp_id: str) -> XpRequest:
        row = self._call(
            f"GET /v2/experience-requests/{xp_id}",
            lambda: self._api().get_experience_request(xp_id),
        )
        return _xp(row)

    def xp_list(self, *, mine: bool = True, limit: int = 50) -> list[XpRequest]:
        page = self._call(
            "GET /v2/experience-requests",
            lambda: self._api().list_experience_requests(mine=mine, limit=limit),
        )
        return [_xp(row) for row in page.entries]

    def xp_episodes(self, xp_id: str) -> list[dict]:
        rows = self._call(
            f"GET /v2/experience-requests/{xp_id}/episodes",
            lambda: self._api().list_experience_request_episodes(xp_id),
        )
        return [_dump(row) for row in rows]

    def xp_download(self, xp_id: str, *, include: list[str], dest: Path) -> list[Path]:
        """coworld's manifest-driven download; returns the files that completed.

        Artifacts still pending or never produced are left out rather than raised: a finished
        request legitimately lacks, say, a replay.
        """
        api = self._api()
        files: list[Path] = []

        def collect(result: DownloadResult) -> None:
            if result.state == "completed" and result.path:
                files.append(dest / result.path)

        self._call(
            "GET /v2/episode-requests/download-manifest",
            lambda: download_episodes(
                api, [xp_id], include=cast(Any, include), directory=dest, on_result=collect
            ),
        )
        return files

    def episodes_for_policy(
        self, policy_version_id: str, *, limit: int = 20, cursor: str | None = None
    ) -> EpisodePage:
        route = f"GET /v2/policy-versions/{policy_version_id}/episode-requests"
        page = self._call(
            route,
            lambda: self._api().list_policy_version_episode_requests(
                _uuid(policy_version_id), limit=limit, cursor=cursor
            ),
        )
        return EpisodePage([_dump(row) for row in page.entries], page.next_cursor)

    def episode(self, episode_request_id: str) -> dict:
        row = self._call(
            f"GET /v2/episode-requests/{episode_request_id}",
            lambda: self._api().get_episode_request(episode_request_id),
        )
        return _dump(row)

    def episode_artifact(self, episode_request_id: str, artifact_type: str) -> bytes:
        return self._call(
            f"GET /v2/episode-requests/{episode_request_id}/artifacts/{artifact_type}",
            lambda: self._api().get_episode_request_artifact_bytes(
                episode_request_id, artifact_type
            ),
        )

    def coworld_manifest(self, coworld_id: str) -> dict:
        """``GET /v2/coworlds/{id}`` as the Player, so private Coworlds the Player may see work.

        (``coworld.upload.download_coworld`` fetches the same document anonymously.)
        """
        row = self._call(
            f"GET /v2/coworlds/{coworld_id}", lambda: self._upload().get_coworld(coworld_id)
        )
        return {"id": row.id, "name": row.name, "version": row.version, "manifest": row.manifest}

    # -- forum -------------------------------------------------------------------------------

    def forum_list(self, slug: str, *, sort: str = "hot", limit: int = 20) -> list[dict]:
        page = self._call(
            f"GET /v2/forums/{slug}/posts",
            lambda: self._forum().list_forum(slug, sort=cast(Any, sort), limit=limit),
        )
        return [_dump(post) for post in page.entries]

    def forum_read(self, post_id: str) -> str:
        return self._call(f"GET /v2/posts/{post_id}.md", lambda: self._forum().read_post(post_id))

    def forum_search(self, query: str, *, slug: str | None = None) -> list[dict]:
        route = "GET /v2/forums/search" if slug is None else f"GET /v2/forums/{slug}/search"
        page = self._call(route, lambda: self._forum().search_forum(query, forum_slug=slug))
        return [_dump(hit) for hit in page.entries]

    def forum_post(
        self,
        *,
        title: str,
        body: str,
        idempotency_key: str,
        slug: str | None = None,
        identity: Identity = "player",
    ) -> dict:
        """The typed client reaches only the global forum; a named one goes through ``request``."""
        if slug is not None:
            request = CreatePostRequest(title=title, body=body, idempotency_key=idempotency_key)
            post = self.request(
                "POST",
                f"/v2/forums/{quote(slug, safe='')}/posts",
                json=request.model_dump(mode="json"),
                identity=identity,
            )
            return {**post, "identity": identity}
        client = self._forum(identity)
        created = self._call(
            "POST /v2/posts",
            lambda: client.create_post(title=title, body=body, idempotency_key=idempotency_key),
        )
        return {**_dump(created), "identity": identity}

    def forum_comment(
        self,
        post_id: str,
        *,
        body: str,
        idempotency_key: str,
        parent_id: str | None = None,
        identity: Identity = "player",
    ) -> dict:
        client = self._forum(identity)
        comment = self._call(
            f"POST /v2/posts/{post_id}/comments",
            lambda: client.create_comment(
                post_id, body=body, idempotency_key=idempotency_key, parent_id=parent_id
            ),
        )
        return {**_dump(comment), "identity": identity}

    def forum_vote(self, post_id: str, value: int, comment_id: str | None = None) -> dict:
        target = post_id if comment_id is None else f"{post_id}/comments/{comment_id}"
        vote = self._call(
            f"PUT /v2/posts/{target}/vote",
            lambda: self._forum().vote(post_id, value=cast(Any, value), comment_id=comment_id),
        )
        return {**_dump(vote), "identity": "player"}

    # -- wiki --------------------------------------------------------------------------------

    def wiki_read(self, wiki: str, page: str) -> dict:
        row = self._call(
            f"GET /v2/wikis/{wiki}/pages/{page}",
            lambda: self._forum().read_wiki_page(wiki, page_slug=page),
        )
        return _dump(row)

    def wiki_search(self, wiki: str, query: str) -> list[dict]:
        result = self._call(
            f"GET /v2/wikis/{wiki}/search", lambda: self._forum().search_wiki(wiki, query=query)
        )
        return [_dump(hit) for hit in result.entries]

    def wiki_history(self, page_id: str) -> list[dict]:
        result = self._call(
            f"GET /v2/wiki-pages/{page_id}/revisions", lambda: self._forum().wiki_history(page_id)
        )
        return [_dump(revision) for revision in result.entries]

    def wiki_edit(
        self,
        wiki: str,
        page: str,
        *,
        title: str,
        body: str,
        base_revision_id: str | None,
        idempotency_key: str,
        note: str | None = None,
        identity: Identity = "player",
    ) -> dict:
        """Compare-and-swap edit; a lost race returns ``{"conflict": True, ...}``, not an error.

        No base creates the page. The route allows that, but the typed client's request requires a
        base, so this goes through the raw client.
        """
        http = self._http(identity)
        path = f"/v2/wikis/{quote(wiki, safe='')}/pages/{quote(page, safe='/')}"
        request = {
            "title": title,
            "body": body,
            "idempotency_key": idempotency_key,
            "base_revision_id": base_revision_id,
            "note": note,
        }

        def edit() -> WikiEditPublic | WikiEditConflictResponse:
            response = http.put(path, json={k: v for k, v in request.items() if v is not None})
            if response.status_code == 409:
                return WikiEditConflictResponse.model_validate(response.json())
            return WikiEditPublic.model_validate(_ok(response).json())

        result = self._call(f"PUT {path}", edit)
        if isinstance(result, WikiEditConflictResponse):
            return {
                "conflict": True,
                "current_revision_id": result.detail.current_revision_id,
                "current_body": result.detail.current_body,
                "identity": identity,
            }
        return {"conflict": False, **_dump(result), "identity": identity}

    # -- the escape hatch --------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        identity: Identity = "player",
    ) -> dict:
        """Any route under ``/observatory`` as ``identity``.

        A JSON object body is returned as is; a bare-list body comes back as ``{"entries": [...],
        "next_cursor": <X-Next-Cursor>}``; an empty body as ``{}``; anything else as ``{"text"}``.
        """
        http = self._http(identity)
        method = method.upper()
        response = self._call(
            f"{method} {path}", lambda: _ok(http.request(method, path, params=params, json=json))
        )
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            return {"text": response.text}
        if isinstance(body, dict):
            return body
        return {"entries": body, "next_cursor": response.headers.get(NEXT_CURSOR_HEADER)}


# -- error mapping -----------------------------------------------------------------------------


def _status_error(route: str, response: httpx.Response) -> PlatformError:
    status = response.status_code
    request_id = response.headers.get("x-request-id")
    detail = _error_detail(response)
    if status == 402:
        return CreditsRefused(
            detail, route=route, request_id=request_id, **_credit_fields(response)
        )
    return PlatformError(detail, route=route, status=status, request_id=request_id)


def _error_detail(response: httpx.Response) -> str:
    """The backend's ``detail`` (or ``detail.message``) when the body is JSON, else the body."""
    text = response.text.strip()
    body = _json_or_none(response)
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            text = detail
        elif isinstance(detail, dict) and isinstance(detail.get("message"), str):
            text = detail["message"]
    if not text:
        text = f"HTTP {response.status_code} {response.reason_phrase}".strip()
    return text[:DETAIL_LIMIT]


def _credit_fields(response: httpx.Response) -> dict[str, Any]:
    """balance / held / next refill from a 402 body (top level or under ``detail``), else None."""
    body = _json_or_none(response)
    sources: list[dict] = []
    if isinstance(body, dict):
        if isinstance(body.get("detail"), dict):
            sources.append(body["detail"])
        sources.append(body)

    def pick(*keys: str) -> Any:
        for source in sources:
            for key in keys:
                if source.get(key) is not None:
                    return source[key]
        return None

    next_refill = pick("next_refill_at", "next_refill")
    return {
        "balance": _number(pick("balance", "balance_credits")),
        "held": _number(pick("held", "held_credits")),
        "next_refill_at": None if next_refill is None else str(next_refill),
    }


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _without_query(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


# -- small helpers -----------------------------------------------------------------------------


def _ok(response: httpx.Response) -> httpx.Response:
    response.raise_for_status()
    return response


def _dump(model: pydantic.BaseModel) -> dict:
    return model.model_dump(mode="json")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise PlatformError(f"not a policy version id: {value!r}", retryable=False) from error


# -- model mapping (pydantic in, protocol dataclasses out) -------------------------------------


def _policy_version(row: coworld_api.PolicyVersionPublic) -> PolicyVersion:
    return PolicyVersion(
        id=str(row.id), name=row.policy.name, version=row.version, player_id=row.player_id
    )


def _policy_version_row(row: coworld_api.PolicyVersionRow) -> PolicyVersion:
    extra = row.model_extra or {}
    return PolicyVersion(
        id=str(row.id),
        name=row.name,
        version=row.version,
        content_hash=extra.get("player_file_content_hash"),
        player_id=extra.get("player_id"),
        created_at=extra.get("created_at"),
    )


def _league(row: coworld_api.LeaguePublic) -> League:
    return League(
        id=row.id,
        name=row.name,
        game_id=row.game.id,
        coworld_id=row.game.coworld_id,
        coworld_name=row.game.coworld_name,
        default_variant_id=(row.commissioner_config or {}).get("default_variant_id"),
        participation_url=row.participation_url,
        forum_markdown_url=row.forum_markdown_url,
        wiki_markdown_url=row.wiki_markdown_url,
    )


def _membership(row: coworld_api.LeaguePolicyMembershipPublic) -> Membership:
    player_id = row.player.id if row.player is not None else row.policy_version.player_id
    return Membership(
        id=row.id,
        league_id=row.league.id,
        policy_version=_policy_version(row.policy_version),
        status=row.status,
        division_id=row.division.id,
        player_id=player_id,
        substatus=row.substatus,
        is_champion=row.is_champion,
    )


def _submission(row: coworld_api.LeagueSubmissionPublic) -> Submission:
    return Submission(
        id=row.id,
        league_id=row.league.id,
        policy_version_id=str(row.policy_version.id),
        status=row.status,
        membership_id=row.league_policy_membership_id,
        created_at=_iso(row.created_at),
    )


def _xp(row: coworld_api.ExperienceRequestRow, title: str | None = None) -> XpRequest:
    return XpRequest(
        id=row.id,
        status=row.status,
        episode_count=row.episode_count,
        completed_count=row.completed_count,
        failed_count=row.failed_count,
        coworld_id=row.coworld_id,
        created_at=_iso(row.created_at),
        error=row.error,
        title=row.title or title,
        coworld_version=row.coworld_version,
    )
