"""Warm the catalog authorities off the boot path (ARBORA-FLEET-R078).

Composing the catalog authorities proves arbora's semantic content and reads
the verified fleet catalog from the engine. On a busy store one of those
engine calls can take minutes, and a timeout used to escape ``asyncio.run``
and exit the process before it ever bound its port: the pod restarted into the
same wait and served nothing. Boot now binds and serves at once; this module
composes the catalog on a background thread, retries a failure with a capped
backoff, and never lets an error leave the thread. Until the catalog is ready
a request that needs it gets the typed, retryable ``UNAVAILABLE`` refusal
(``catalog warming``), and ``/health/ready`` reports the state.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any, Literal

__all__ = ["CatalogWarmup", "catalog_state", "start_catalog_warmup"]

logger = logging.getLogger(__name__)

CatalogStatus = Literal["warming", "retrying", "ready", "stopped"]

#: First retry delay; each retry doubles it up to the cap.
RETRY_BASE_S = 5.0
RETRY_CAP_S = 300.0


class CatalogWarmup:
    """The background composition and its typed, observable state."""

    def __init__(
        self,
        compose: Callable[[], Coroutine[Any, Any, Any]],
        *,
        after: Callable[[], None] | None = None,
        reestablish: Callable[[], None] | None = None,
        retry_base_s: float = RETRY_BASE_S,
        retry_cap_s: float = RETRY_CAP_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._compose = compose
        self._after = after
        self._reestablish = reestablish
        self._retry_base_s = retry_base_s
        self._retry_cap_s = retry_cap_s
        self._clock = clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "status": "warming",
            "attempts": 0,
            "since": clock(),
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def _attempt(self) -> bool:
        """One composition; ``True`` once the catalog is ready."""
        attempts = self.state()["attempts"] + 1
        self._set(attempts=attempts)
        try:
            self._compose_once()
        except Exception as exc:  # noqa: BLE001 - every failure is retried
            # A class name only: the readiness route is unauthenticated.
            self._set(status="retrying", last_error=type(exc).__name__)
            logger.warning(
                "catalog warm-up attempt %d failed: %s: %s",
                attempts,
                type(exc).__name__,
                exc,
            )
            return False
        self._set(status="ready", ready_at=self._clock(), last_error=None)
        logger.info("catalog authorities ready after %d attempt(s)", attempts)
        return True

    def _compose_once(self) -> None:
        """Compose once; re-establish an expired session and compose again.

        A warm-up outlives the credential it started with (the backoff alone
        reaches minutes). An expired session is not a catalog fault, so it is
        renewed and the composition repeats at once instead of failing every
        later attempt with the same expired session.
        """
        from arbora.security.session import SessionExpiredError

        try:
            asyncio.run(self._compose())
        except SessionExpiredError:
            if self._reestablish is None:
                raise
            logger.info("catalog warm-up: engine session expired; re-establishing")
            self._reestablish()
            asyncio.run(self._compose())

    def _run_after(self) -> None:
        if self._after is None:
            return
        try:
            self._after()
        except Exception as exc:  # noqa: BLE001 - serving outlives the follow-up
            logger.warning("catalog warm-up follow-up failed: %s", exc)

    def run(self) -> None:
        """Compose until ready or stopped; never raises."""
        delay = self._retry_base_s
        while not self._stop.is_set():
            if self._attempt():
                self._run_after()
                return
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self._retry_cap_s)
        self._set(status="stopped")

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="catalog-warmup", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


_CURRENT: list[CatalogWarmup | None] = [None]


def catalog_state() -> dict[str, Any] | None:
    """The process catalog warm-up state, or ``None`` before boot starts it."""
    current = _CURRENT[0]
    return None if current is None else current.state()


def start_catalog_warmup(
    compose: Callable[[], Coroutine[Any, Any, Any]],
    *,
    after: Callable[[], None] | None = None,
    reestablish: Callable[[], None] | None = None,
) -> CatalogWarmup:
    """Start the one process warm-up and make it the reported state."""
    warmup = CatalogWarmup(compose, after=after, reestablish=reestablish)
    _CURRENT[0] = warmup
    warmup.start()
    return warmup
