"""How long the runtime's HTTP calls to the gateway, the seam and the platform take.

Every client in the image is built on ``Observed(target)``. It times each call up to its response
headers (a stream's body is the caller's), records a payload-free ``workspace.http_client`` span,
and logs a warning for a call over ``SLOW_CALL_S``, an error answer or a failed call. Neither
names a header, body, query or credential; the path is a template (``path_template``).
"""

import asyncio
import logging
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core" / "src"))
from research_core.telemetry import span  # noqa: E402

log = logging.getLogger(__name__)
SLOW_CALL_S = 1.0
# A segment the template keeps: an API version or a plain lowercase word. Anything else (an ID, a
# percent-encoded name, a number) becomes ``{id}``.
_PATH_WORD = re.compile(r"v\d+|[a-z]+(?:[_.-][a-z]+)*")


def path_template(path: str) -> str:
    return "/".join(
        part if not part or _PATH_WORD.fullmatch(part) else "{id}" for part in path.split("/")
    )


class Observed(httpx.AsyncBaseTransport):
    """httpx's default transport, timed. ``target`` names the service: gateway, seam, platform."""

    def __init__(self, target: str, *, ready: asyncio.Event | None = None) -> None:
        self.ready = ready
        self.target = target
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.ready is not None:
            await self.ready.wait()
        route = path_template(request.url.path)
        status = 0
        started = time.monotonic()
        with span(
            "workspace.http_client",
            **{"peer.service": self.target, "http.request.method": request.method},
            **{"url.template": route},
        ) as current:
            try:
                response = await self.inner.handle_async_request(request)
                status = response.status_code
                return response
            finally:
                duration_s = time.monotonic() - started
                error = sys.exc_info()[0]
                cancelled = error is not None and issubclass(error, asyncio.CancelledError)
                outcome = "cancelled" if cancelled else error.__name__ if error else str(status)
                current.set_attribute("outcome", outcome)
                if status:
                    current.set_attribute("http.response.status_code", status)
                failed = (error is not None and not cancelled) or status >= 500
                if failed or duration_s > SLOW_CALL_S:
                    log.warning(
                        "%s %s %s: %s in %.2fs",
                        self.target,
                        request.method,
                        route,
                        outcome,
                        duration_s,
                        extra={
                            "attributes": {
                                "peer.service": self.target,
                                "http.request.method": request.method,
                                "url.template": route,
                                "outcome": outcome,
                                "duration_s": round(duration_s, 3),
                            }
                        },
                    )

    async def aclose(self) -> None:
        await self.inner.aclose()
