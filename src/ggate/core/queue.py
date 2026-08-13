"""Background fire-and-forget delivery.

One daemon worker drains a bounded deque. Delivery failures are retried a few times
with capped exponential backoff (the agent may be restarting); when the queue is
full the oldest event is dropped, and `flush` is deadline-bounded so application
shutdown never hangs on a dead agent.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .transport import Transport

logger = logging.getLogger("ggate")

_MAX_ATTEMPTS = 3
_BACKOFF_START = 1.0
_BACKOFF_CAP = 30.0


class DeliveryQueue:
    def __init__(self, transport: Transport, maxsize: int, flush_timeout: float):
        self._transport = transport
        self._maxsize = max(1, maxsize)
        self._flush_timeout = flush_timeout
        self._cond = threading.Condition()
        self._items: Deque[List[Any]] = deque()  # [request, attempts]
        self._in_flight = False
        self._started = False
        self._dropped = 0

    def submit(self, request: Dict[str, Any]) -> None:
        with self._cond:
            if len(self._items) >= self._maxsize:
                self._items.popleft()
                self._dropped += 1
                if self._dropped in (1, 100, 10000):
                    logger.warning("ggate queue full; dropped %d event(s) so far", self._dropped)
            self._items.append([request, 0])
            self._cond.notify_all()
        self._ensure_started()

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait until the queue drains. Returns False when the deadline expired first."""
        deadline = time.monotonic() + (self._flush_timeout if timeout is None else timeout)
        with self._cond:
            while self._items or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._cond:
            if self._started:
                return
            atexit.register(self.flush)
            thread = threading.Thread(target=self._run, name="ggate-sdk-queue", daemon=True)
            thread.start()
            self._started = True

    def _run(self) -> None:
        backoff = _BACKOFF_START
        while True:
            with self._cond:
                while not self._items:
                    self._cond.notify_all()
                    self._cond.wait()
                request, attempts = self._items.popleft()
                self._in_flight = True
            error: Optional[Exception] = None
            try:
                self._transport.request(request)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                error = exc
            with self._cond:
                self._in_flight = False
                self._cond.notify_all()
                if error is None:
                    backoff = _BACKOFF_START
                    continue
                if attempts + 1 >= _MAX_ATTEMPTS:
                    logger.warning("ggate delivery failed after %d attempts, dropping event: %s", attempts + 1, error)
                else:
                    self._items.appendleft([request, attempts + 1])
                # Wait out the backoff (releases the lock; a submit re-wakes us early).
                self._cond.wait(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP)
