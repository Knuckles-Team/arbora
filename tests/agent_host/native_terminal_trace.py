"""Bounded, metadata-only diagnostics for the real native transport tests."""

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

import httpx

T = TypeVar("T")


def is_log_unavailable_response(response: httpx.Response) -> bool:
    """Classify one fixed wire code without exposing arbitrary response content."""
    if len(response.content) > 4096:
        return False
    try:
        return response.json() == {"detail": "log_unavailable"}
    except ValueError:
        return False


class NativeTransportTrace:
    """Never retain credentials, references, response bodies or exception text."""

    def __init__(self) -> None:
        self.events: deque[dict[str, str | float]] = deque(maxlen=64)

    async def observe(
        self,
        stage: Literal["bind", "read", "session", "emit"],
        action: Callable[[], Awaitable[T]],
    ) -> T:
        if stage not in {"bind", "read", "session", "emit"}:
            raise ValueError("Unsupported diagnostic stage")
        started = time.monotonic()
        self.events.append({"stage": stage, "status": "started", "elapsed_ms": 0.0})
        status = "completed"
        try:
            return await action()
        except BaseException as error:
            status = self._category(error)
            raise
        finally:
            self.events.append(
                {
                    "stage": stage,
                    "status": status,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                }
            )

    @staticmethod
    def _category(error: BaseException) -> str:
        categories = (
            (asyncio.CancelledError, "cancelled"),
            (TimeoutError, "timeout"),
            (PermissionError, "permission_denied"),
        )
        return next(
            (name for kind, name in categories if isinstance(error, kind)), "error"
        )

    def summary(self) -> str:
        return json.dumps(list(self.events), separators=(",", ":"))
