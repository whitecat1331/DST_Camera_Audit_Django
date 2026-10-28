"""Live review updates over Server-Sent Events (port of IMS's SSEBroker, sse.go).

Saves on the review page (Complete / Notes / assignee) broadcast the camera's
new state to every open review page, so teammates see changes without a
refresh. The broker is in-process, like IMS's, so the review instance runs a
single gunicorn worker (threads carry the concurrency; see the Dockerfile).

Each stream holds one gunicorn thread, so connections are capped below the
thread count (DST_SSE_MAX_CLIENTS), and each stream ends after
DST_SSE_MAX_SECONDS: the browser's EventSource reconnects on its own, which
also re-checks the session (IMS re-validates every 30 min).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections.abc import Iterator

logger = logging.getLogger(__name__)

MAX_CLIENTS = int(os.environ.get("DST_SSE_MAX_CLIENTS", "12"))
MAX_SECONDS = float(os.environ.get("DST_SSE_MAX_SECONDS", "600"))
HEARTBEAT_SECONDS = 20.0
RETRY_MS = 3000


class Broker:
    def __init__(self, max_clients: int = MAX_CLIENTS) -> None:
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._clients: dict[queue.Queue, int] = {}  # queue -> round id

    def try_subscribe(self, round_id: int) -> queue.Queue | None:
        with self._lock:
            if len(self._clients) >= self.max_clients:
                return None
            q: queue.Queue = queue.Queue(maxsize=64)
            self._clients[q] = round_id
            logger.info("[SSE] client connected round=%s total=%d", round_id, len(self._clients))
            return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if self._clients.pop(q, None) is not None:
                logger.info("[SSE] client disconnected total=%d", len(self._clients))

    def broadcast(self, round_id: int, event: str, data: dict) -> None:
        with self._lock:
            targets = [q for q, rid in self._clients.items() if rid == round_id]
        for q in targets:
            try:
                q.put_nowait((event, data))
            except queue.Full:  # a stalled client misses updates, as in IMS
                logger.warning("[SSE] client queue full; dropping %s", event)

    def count(self) -> int:
        with self._lock:
            return len(self._clients)


broker = Broker()


def frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def stream(q: queue.Queue, *, max_seconds: float = MAX_SECONDS, heartbeat: float = HEARTBEAT_SECONDS) -> Iterator[str]:
    """SSE body for one subscriber; always unsubscribes when the client goes away."""
    try:
        yield f"retry: {RETRY_MS}\n" + frame("connected", {})
        ends = time.monotonic() + max_seconds
        while (left := ends - time.monotonic()) > 0:
            try:
                event, data = q.get(timeout=min(heartbeat, left))
            except queue.Empty:
                yield ": ping\n\n"  # a write to a gone client ends the stream
                continue
            yield frame(event, data)
    finally:
        broker.unsubscribe(q)
