"""The platform operations the tools and the watcher need (design C.2, C.4, B.6, B.13).

Both the real client (``platform/real.py``, on the official ``coworld`` and ``softmax_client``
packages) and the fake (``platform/fake.py``) implement :class:`Platform`. Identities are
``"player"`` (the workspace's Player; every profile) and ``"user"`` (the user's own account;
Orchestrator only, when provisioned).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from research_core.platform.errors import PlatformError

Identity = Literal["player", "user"]
AutoChampion = Literal["always", "never", "lineage"]


@dataclass(frozen=True)
class WhoAmI:
    kind: Literal["player", "user", "anonymous"]
    subject_id: str | None
    email: str | None = None
    owner_user_id: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class PolicyVersion:
    id: str
    name: str
    version: int
    content_hash: str | None = None
    player_id: str | None = None
    created_at: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name}:v{self.version}"


@dataclass(frozen=True)
class League:
    id: str
    name: str
    game_id: str | None = None
    coworld_id: str | None = None  # the Coworld version the league runs
    coworld_name: str | None = None  # the same in every version of the Coworld
    default_variant_id: str | None = None  # None: the manifest's first variant
    participation_url: str | None = None
    forum_markdown_url: str | None = None
    wiki_markdown_url: str | None = None


@dataclass(frozen=True)
class Membership:
    id: str
    league_id: str
    policy_version: PolicyVersion
    status: str
    division_id: str | None = None
    player_id: str | None = None
    substatus: str | None = None
    is_champion: bool = False


@dataclass(frozen=True)
class Submission:
    id: str
    league_id: str
    policy_version_id: str
    status: str  # pending | processing | placed | rejected
    membership_id: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class XpRequest:
    id: str
    status: str  # pending | running | completed | failed | cancelled
    episode_count: int
    completed_count: int = 0
    failed_count: int = 0
    coworld_id: str | None = None
    created_at: str | None = None
    error: str | None = None
    title: str | None = None
    coworld_version: str | None = None  # the game version the platform runs the request on

    @property
    def terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class Credits:
    balance: float
    max: float | None = None
    refill_amount: float | None = None
    next_refill_at: str | None = None
    spent_this_period: float | None = None
    recent_requests: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class EpisodePage:
    entries: list[dict]
    next_cursor: str | None


class Platform(Protocol):
    def close(self) -> None: ...

    def whoami(self, identity: Identity = "player") -> WhoAmI: ...

    def credits(self) -> Credits: ...

    def upload_file_policy(
        self, *, name: str, content: bytes, tags: dict[str, str] | None = None
    ) -> PolicyVersion: ...

    def policy_versions(
        self,
        *,
        name: str | None = None,
        player_id: str | None = None,
        mine: bool = True,
        identity: Identity = "player",
    ) -> list[PolicyVersion]: ...

    def upload_image_policy(
        self, *, name: str, archive_path: Path, tags: dict[str, str] | None = None
    ) -> PolicyVersion: ...

    def upload_inputs(
        self, *, name: str, artifact: Path, image_archive: bool = False
    ) -> dict[str, Any]:
        """The exact inputs an upload of ``artifact`` under ``name`` is deduplicated on.

        For a file: ``{name, player_id, file_hash, file_size, attributes}``, the dict
        :meth:`lookup_policy_upload` takes. Computed locally (the Player id may need one read).
        """
        ...

    def lookup_policy_upload(self, inputs: dict[str, Any]) -> PolicyVersion | None:
        """The published policy version these exact upload inputs produced, read-only.

        ``None`` is the platform's definite answer that no committed version matches. A refusal
        or an outage raises ``PlatformError`` instead: not knowing is not the same as ``None``.
        """
        ...

    def policy_file(self, policy_version_id: str, identity: Identity = "player") -> bytes: ...

    def leagues(self, *, game_id: str | None = None) -> list[League]: ...

    def league(self, league_id: str) -> League: ...

    def divisions(self, league_id: str) -> list[dict]: ...

    def standings(self, division_id: str) -> list[dict]: ...

    def memberships(
        self,
        *,
        league_id: str | None = None,
        player_id: str | None = None,
        policy_version_id: str | None = None,
        active_only: bool = False,
        mine: bool = False,
        champions_only: bool = False,
    ) -> list[Membership]: ...

    def submissions(
        self,
        *,
        league_id: str | None = None,
        player_id: str | None = None,
        policy_version_id: str | None = None,
        mine: bool = False,
    ) -> list[Submission]: ...

    def submit(
        self,
        *,
        league_id: str,
        policy_version_id: str,
        auto_champion: AutoChampion = "always",
        player_id: str | None = None,
    ) -> Submission: ...

    def set_champion(self, membership_id: str) -> dict: ...

    def xp_create(self, body: dict) -> XpRequest: ...

    def xp_get(self, xp_id: str) -> XpRequest: ...

    def xp_list(self, *, mine: bool = True, limit: int = 50) -> list[XpRequest]: ...

    def xp_episodes(self, xp_id: str) -> list[dict]: ...

    def xp_download(self, xp_id: str, *, include: list[str], dest: Path) -> list[Path]: ...

    def episodes_for_policy(
        self, policy_version_id: str, *, limit: int = 20, cursor: str | None = None
    ) -> EpisodePage: ...

    def episode(self, episode_request_id: str) -> dict: ...

    def episode_artifact(self, episode_request_id: str, artifact_type: str) -> bytes: ...

    def coworld_manifest(self, coworld_id: str) -> dict: ...

    def forum_list(self, slug: str, *, sort: str = "hot", limit: int = 20) -> list[dict]: ...

    def forum_read(self, post_id: str) -> str: ...

    def forum_search(self, query: str, *, slug: str | None = None) -> list[dict]: ...

    def forum_post(
        self,
        *,
        title: str,
        body: str,
        idempotency_key: str,
        slug: str | None = None,
        identity: Identity = "player",
    ) -> dict:
        """A post in the forum named by ``slug``; with none, in the global forum."""
        ...

    def forum_comment(
        self,
        post_id: str,
        *,
        body: str,
        idempotency_key: str,
        parent_id: str | None = None,
        identity: Identity = "player",
    ) -> dict: ...

    def forum_vote(self, post_id: str, value: int, comment_id: str | None = None) -> dict: ...

    def wiki_read(self, wiki: str, page: str) -> dict: ...

    def wiki_search(self, wiki: str, query: str) -> list[dict]: ...

    def wiki_history(self, page_id: str) -> list[dict]: ...

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
    ) -> dict: ...

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        identity: Identity = "player",
    ) -> dict: ...


RETRY_DELAYS: tuple[float, ...] = (1.0, 4.0, 16.0)


def with_retries[T](
    fn: Callable[[], T],
    *,
    tries: int = 3,
    delays: Iterable[float] = RETRY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` up to ``tries`` times, sleeping between retryable platform errors.

    A non-retryable error (4xx other than 408/425/429, a credits refusal, a missing identity)
    is raised at once. The last retryable error is raised when the tries are spent.
    """
    delays = list(delays)
    last: PlatformError | None = None
    for attempt in range(tries):
        try:
            return fn()
        except PlatformError as error:
            if not error.retryable:
                raise
            last = error
            if attempt + 1 < tries:
                sleep(delays[min(attempt, len(delays) - 1)] if delays else 0)
    assert last is not None
    raise last
