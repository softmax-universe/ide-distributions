"""Where telemetry leaves a process: one ``write`` per line to the container's output stream.

The Datadog Agent collects whatever a container writes there. Every process opens the stream of PID
1 itself and never uses its own stdout, because ``run_in_workspace`` takes a tool process's last
stdout line as the tool's result (``plugin/executor.py``).

On Linux one ``write`` of at most 4,096 bytes reaches a pipe whole, so lines of different processes
never mix. Above that size concurrent lines tear, which is why ``records.MAX_LINE_BYTES`` is a hard
limit.

Other output shares this stream, and can have a line open when a telemetry line arrives: an
unbuffered ``print`` writes its text and its newline separately, a child can leave a line
unfinished, and a pipe splits a long line. So each telemetry line starts with a newline of its own
(``records.line_bytes``), at the price of a blank line on the stream. Buffering cannot do this: it
covers only Python's writers, and buffered stdout makes an interpreter abort at exit when a thread
is stuck on a full pipe.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

CONTAINER_STREAM = "/proc/1/fd/1"
# For tests and local runs. It names a file or pipe to write to instead.
STREAM_VARIABLE = "IDE_TELEMETRY_STREAM"


def stream_path(env: Mapping[str, str] = os.environ) -> str:
    return env.get(STREAM_VARIABLE) or CONTAINER_STREAM


class LogStream:
    """One process's end of the stream, opened at the first line so that a read costs nothing.

    A developer's Mac has no ``/proc``, so with no such path a line goes nowhere and that is not an
    error. Any other failure to open is warned about once.
    """

    def __init__(self, path: str, *, nonblocking: bool = False) -> None:
        """``nonblocking``: a line that does not fit in the pipe now raises ``BlockingIOError``
        instead of waiting, for a writer on its caller's own thread. A line is at most ``PIPE_BUF``
        bytes on Linux, so it goes in whole or not at all."""
        self._path = path
        self._nonblocking = nonblocking
        self._descriptor: int | None = None
        self._unavailable = False

    def write(self, line: bytes) -> None:
        if self._descriptor is None and not self._unavailable:
            self._open()
        if self._descriptor is not None:
            os.write(self._descriptor, line)

    def _open(self) -> None:
        # The plain form is what a real pod under gVisor was tested with. A file needs the rest.
        flags = os.O_WRONLY | (os.O_NONBLOCK if self._nonblocking else 0)
        if self._path != CONTAINER_STREAM:
            flags |= os.O_APPEND | os.O_CREAT
        try:
            self._descriptor = os.open(self._path, flags, 0o600)
        except OSError as error:
            self._unavailable = True
            if not isinstance(error, FileNotFoundError):
                logger.warning("telemetry: cannot open the stream (%s)", type(error).__name__)
