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
MAX_ROOTS = 50
WAIT_SECONDS = 25
TERMINAL_STATES = {"completed", "failed", "cancelled"}


class BatchProgress(BaseModel):
    id: str
    status: Literal["pending", "submitted", "running", "completed", "failed", "cancelled"]


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
                "v2/episode-requests/changes",
                params=params,
                headers={"Authorization": f"Bearer {identity.player_token(env)}"},
            )
            return ChangePage.model_validate_json(response.raise_for_status().content)

        yield read


async def wait_for_terminal(read: ReadChanges, ids: tuple[str, ...]) -> bool:
    """Follow one selection from a fresh snapshot, including after workspace wake."""
    expected = set(ids)
    cursor = None
    backoff = 1
    while True:
        (result,) = await asyncio.gather(read(ids, cursor), return_exceptions=True)
        if isinstance(result, ChangePage):
            if {batch.id for batch in result.batches} == expected:
                if all(batch.status in TERMINAL_STATES for batch in result.batches):
                    return True
                cursor = result.next_cursor
                backoff = 1
                continue
            cursor = None
            problem = "batch selection mismatch"
        elif isinstance(result, (httpx.HTTPError, PlatformError, ValidationError)):
            problem = type(result).__name__
        else:
            raise result

        delay = backoff
        if isinstance(result, httpx.HTTPStatusError):
            status = result.response.status_code
            if status == 404 and b'"scope_changed"' in result.response.content:
                return False
            if status in {409, 410} and cursor is not None:
                cursor = None
                continue
            problem = f"HTTP {status}"
            retry_after = result.response.headers.get("Retry-After", "")
            if (
                status == 429
                and retry_after.isascii()
                and retry_after.isdecimal()
                and len(retry_after) <= 9
            ):
                delay = max(delay, int(retry_after))
        log.warning("Experience change feed failed (%s); reconnecting in %ss", problem, delay)
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, 60)
