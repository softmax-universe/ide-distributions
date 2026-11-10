"""Relay the model's reasoning to the AIDE backend (``image/aide.py``) while it streams.

An admitted ``/v1/runs`` turn's event stream carries text deltas and tool events, never reasoning,
so a client sees nothing while a model thinks. Hermes hands each reasoning delta to the
``on_stream_delta`` hook (``kind="reasoning"``) once ``plugins.stream_reasoning_deltas`` is on, on
a worker thread off the token path. This sends each one as a JSON line over one local TCP
connection to the backend at ``IDE_REASONING_RELAY``. The backend logs it in the request whose run
serves that Session and drops it otherwise, so it alone orders it beside the run's events.

Hermes's worker queue holds 1,024 deltas and drops the oldest when full. Sending a line takes
microseconds, so that needs the worker to stall for seconds. A failed send raises into Hermes's hook
isolation, which logs the first of each kind of failure and never reaches the turn; the next delta
reconnects. Each line carries this process's running count ``n``, so the backend sees a lost line as
a gap. Compaction moves a Session to a new id mid-turn, so a line names every id its Session had.

The relay is process-wide, as a multiplexed gateway registers this plugin once per profile.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any

RELAY_VARIABLE = "IDE_REASONING_RELAY"
SEND_TIMEOUT_S = 1.0  # a backend that stops reading costs one worker this long per delta


class Relay:
    def __init__(self, address: str) -> None:
        host, port = address.rsplit(":", 1)
        self.address = (host, int(port))
        self.lock = threading.Lock()
        self.connection: socket.socket | None = None
        self.sent = 0
        self.parents: dict[str, str] = {}  # a Session id -> the id compaction moved it from

    def post_compaction(self, session_id: str = "", old_session_id: str = "", **_: Any) -> None:
        if old_session_id and session_id != old_session_id:
            self.parents[session_id] = old_session_id

    def on_stream_delta(
        self, delta: str = "", kind: str = "text", session_id: str = "", **_: Any
    ) -> None:
        if kind != "reasoning" or not delta:
            return
        sessions = [session_id]
        while sessions[-1] in self.parents:
            sessions.append(self.parents[sessions[-1]])
        with self.lock:
            self.sent += 1
            line = json.dumps({"sessions": sessions, "n": self.sent, "text": delta})
            # Held only after a whole line went out, so a failed send leaves no half line behind.
            connection, self.connection = self.connection, None
            if connection is None:
                connection = socket.create_connection(self.address, timeout=SEND_TIMEOUT_S)
            connection.sendall(line.encode() + b"\n")
            self.connection = connection


_relay: Relay | None = None


def register_relay(ctx: Any) -> None:
    """Register the relay's hooks where the runtime names a backend; elsewhere there is none."""
    global _relay
    if RELAY_VARIABLE not in os.environ:
        return
    if _relay is None:
        _relay = Relay(os.environ[RELAY_VARIABLE])
    ctx.register_hook("on_stream_delta", _relay.on_stream_delta)
    ctx.register_hook("post_compaction", _relay.post_compaction)
