"""Wait for terminal experience requests through Observatory's change feed."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

import httpx
from pydantic import BaseModel, ValidationError
from softmax_client.agent import user_agent

from research_core.platform import identity
from research_core.platform.errors import PlatformError

if TYPE_CHECKING:
    from research_core.layout import Workspace

log = logging.getLogger(__name__)
PATH = "/v2/episode-requests/changes"
MAX_ROOTS = 50
WAIT_SECONDS = 25


class BatchProgress(BaseModel):
    id: str
    status: Literal["pending", "submitted", "running", "completed", "failed", "cancelled"]

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}


class ChangePage(BaseModel):
    batches: list[BatchProgress]
    next_cursor: str


type ReadChanges = Callable[[tuple[str, ...], str | None], Awaitable[ChangePage]]


@asynccontextmanager
async def connect(env: Mapping[str, str], ws: Workspace) -> AsyncIterator[ReadChanges]:
    if env.get("IDE_PLATFORM", "real").lower() == "fake":
        from research_core.platform.fake import FakePlatform

        fake = FakePlatform(ws.state / "fake_platform.json", env)

        async def fake_read(ids: tuple[str, ...], cursor: str | None) -> ChangePage:
            while True:
                batches = await asyncio.to_thread(fake.xp_progress, ids)
                page = ChangePage(
                    batches=batches, next_cursor=":".join(b["status"] for b in batches)
                )
                if page.next_cursor != cursor:
                    return page
                await asyncio.sleep(0.1)

        yield fake_read
        return

    async with httpx.AsyncClient(
        base_url=f"{identity.server(env)}/observatory/",
        headers={"User-Agent": user_agent("ide-distributions")},
        timeout=WAIT_SECONDS + 10,
        follow_redirects=True,
    ) as client:

        async def read(ids: tuple[str, ...], cursor: str | None) -> ChangePage:
            params: dict = {
                "ids": ids,
                "view": "progress",
                "limit": 200,
                "wait_seconds": WAIT_SECONDS,
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(
                PATH.lstrip("/"),
                params=params,
                headers={"Authorization": f"Bearer {identity.player_token(env)}"},
            )
            return ChangePage.model_validate_json(response.raise_for_status().content)

        yield read


async def wait_for_terminal(read: ReadChanges, ids: tuple[str, ...]) -> None:
    """Follow one selection from a fresh snapshot, including after workspace wake."""
    expected = set(ids)
    cursor = None
    backoff = 1
    while True:
        (page,) = await asyncio.gather(read(ids, cursor), return_exceptions=True)
        if isinstance(page, ChangePage) and {batch.id for batch in page.batches} == expected:
            if all(batch.terminal for batch in page.batches):
                return
            cursor = page.next_cursor
            backoff = 1
            continue
        status = page.response.status_code if isinstance(page, httpx.HTTPStatusError) else None
        if status in {409, 410} and cursor is not None:
            # The platform invalidates cursors on selection changes and expiry.
            cursor = None
            continue
        delay = backoff
        if isinstance(page, httpx.HTTPStatusError) and status == 429:
            retry_after = page.response.headers.get("Retry-After", "")
            if retry_after.isascii() and retry_after.isdecimal() and len(retry_after) <= 9:
                delay = max(delay, int(retry_after))
        if isinstance(page, ChangePage):
            problem = "batch selection mismatch"
            cursor = None
        elif isinstance(page, (httpx.HTTPError, PlatformError, ValidationError)):
            problem = type(page).__name__
        else:
            raise page
        log.warning(
            "Experience change feed failed (%s, status=%s); reconnecting in %ss",
            problem,
            status,
            delay,
        )
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, 60)
