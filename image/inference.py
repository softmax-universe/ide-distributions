"""Acquire and renew the platform's spend-capped model credential inside the image."""

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from dotenv import set_key
from pydantic import BaseModel, SecretStr, TypeAdapter

# The inference state's helpers live in the release's stdlib-only core, which the image ships
# beside this folder but does not install into Hermes's interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core" / "src"))
from research_core import inference as state  # noqa: E402
from research_core.restricted_profile import INFERENCE_KEY  # noqa: E402
from research_core.restricted_profile import PROFILE as RESTRICTED_PROFILE  # noqa: E402

log = logging.getLogger(__name__)


@dataclass
class Availability:
    """The image's inference gate: an unexpired lease and no block in the inference state."""

    lease_file: Path | None = None
    expires_at: datetime = datetime.min.replace(tzinfo=UTC)

    def blocking(self) -> state.InferenceState | None:
        return state.blocking_state(self.lease_file)

    def available(self) -> bool:
        return datetime.now(UTC) < self.expires_at and self.blocking() is None


class Lease(BaseModel):
    key: SecretStr
    expires_at: datetime
    renew_at: datetime
    allowance_usd: float


class Unavailable(BaseModel):
    kind: Literal["budget_exhausted", "reconciliation_required", "mint_in_progress"]
    message: str
    next_refill_at: datetime
    retry_at: datetime


async def record_state(update, *args, **kwargs) -> None:
    """Run an inference-state update off the event loop (its lock polls with ``time.sleep``).

    A failed write (``OSError``, which includes the lock's ``LockTimeout``) is logged, never
    raised: the lease loop runs in the runtime's task group, so raising would stop the
    workspace. Only a refusal's bookkeeping goes through here; a new key is recorded in the
    lease file alone, which ends a budget block without a state write.
    """
    try:
        await asyncio.to_thread(update, *args, **kwargs)
    except OSError as error:
        log.warning("Inference state not recorded: %s: %s", type(error).__name__, error)


async def maintain(
    *,
    platform_url: str,
    workspace_id: str,
    credential: str,
    homes: tuple[Path, ...],
    availability: Availability,
    lease_file: Path,
    ready: asyncio.Event,
) -> None:
    async with httpx.AsyncClient(
        base_url=platform_url.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {credential}"},
        timeout=30,
    ) as client:
        while True:
            (response,) = await asyncio.gather(
                client.put(f"v2/workspaces/{workspace_id}/inference"), return_exceptions=True
            )
            if isinstance(response, httpx.TransportError):
                log.warning("Inference lease transport unavailable")
                await asyncio.sleep(10)
                continue
            if isinstance(response, BaseException):
                raise response
            if not response.is_success:
                delay = max(1, int(response.headers.get("Retry-After", "30")))
                log.warning(
                    "Inference lease refused: HTTP %s; retry in %ss", response.status_code, delay
                )
                await asyncio.sleep(delay)
                continue
            result = TypeAdapter(Lease | Unavailable).validate_json(response.content)
            if isinstance(result, Unavailable):
                log.warning("Inference unavailable: %s", result.kind)
                renew_at = min(result.retry_at, result.next_refill_at)
                await record_state(
                    state.lease_refused,
                    lease_file,
                    result.next_refill_at,
                    budget_exhausted=result.kind == "budget_exhausted",
                )
            else:
                for home in homes:
                    pending = home / ".env.inference-pending"
                    pending.touch(mode=0o600)
                    pending.chmod(0o600)
                    pending.write_text((home / ".env").read_text())
                    name = (
                        INFERENCE_KEY
                        if home.name == RESTRICTED_PROFILE
                        else "OPENROUTER_API_KEY"
                    )
                    set_key(pending, name, result.key.get_secret_value())
                    pending.replace(home / ".env")
                refill_at = result.expires_at - state.RENEWAL_GRACE
                # Renew by the refill even if the platform's renew_at lands later, so a
                # workspace blocked on a spent key resumes on time.
                renew_at = min(result.renew_at, refill_at)
                availability.expires_at = result.expires_at
                # The lease file is essential (it is the gate and the record of the key), so a
                # failed write raises, as the .env writes above do.
                await asyncio.to_thread(
                    state.lease_installed,
                    lease_file,
                    result.expires_at,
                    result.key.get_secret_value(),
                )
                ready.set()
            await asyncio.sleep(max(2, (renew_at - datetime.now(UTC)).total_seconds()))
