"""The one gate every connector launch and attach passes (ARBORA-HOST-R037.3).

A connector's integrity is a pinned digest set that arbora records from the
package index (``ARBORA-HOST-R037.1``) and the connector SDK verifies
(``verify_release``). This module joins the two behind one function so no launch
path decides for itself: :meth:`ReleaseGate.admit` records the release the
connector serves when it is not yet recorded, then verifies the installed
content digest against the pin and raises the SDK's typed refusal on any
mismatch. Recording comes first because the pin must exist before the first
verify; a connector whose version the package index does not publish gets no
pin and is refused with ``RELEASE_PIN_MISSING``.

Every site that starts a connector or opens a session to one calls
:func:`admit_release` before it imports a pack or makes a tool call. The
architecture test ``tests/architecture/test_release_gate_chokepoint.py``
enumerates those sites from the source, so a new one cannot skip the gate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from scion_sdk.manifest.release_digest import ReleaseDigestError, read_release_evidence
from scion_sdk.ports.release_pins import ReleasePins
from scion_sdk.runner.errors import ReleaseVerificationError
from scion_sdk.runner.provisioning import verify_release

from arbora.fleet.release_record import RecordSender

logger = logging.getLogger(__name__)

__all__ = [
    "RELEASE_URI",
    "ReleaseGate",
    "ReleaseRecording",
    "admit_release",
    "install_release_gate",
    "release_view",
]

RELEASE_URI = "connector://release"


class ReleaseRecording(Protocol):
    """Records the release a connector serves (``ReleaseRecorder``)."""

    async def record(self, served: Any, send: RecordSender) -> Mapping[str, Any]: ...


class _ReleaseView:
    """The two MCP reads verification needs, over an MCP client or a fastmcp session."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_resources(self) -> Sequence[Any]:
        listed = await self._session.list_resources()
        return list(getattr(listed, "resources", listed))

    async def read_resource(self, uri: str) -> str:
        result = await self._session.read_resource(uri)
        if isinstance(result, str):
            return result
        blocks = getattr(result, "contents", result)
        texts = [getattr(block, "text", None) for block in blocks]
        if not texts or not all(isinstance(text, str) for text in texts):
            raise ReleaseDigestError("release evidence has no text content")
        return "".join(texts)


def release_view(session: Any) -> Any:
    """``session`` as the read-only view :func:`verify_release` takes."""
    return _ReleaseView(session)


@dataclass(frozen=True, slots=True)
class ReleaseGate:
    """Record, then verify, the release one connector session serves."""

    recorder: ReleaseRecording
    send: RecordSender
    pins: ReleasePins

    async def admit(self, session: Any, connector: str) -> Mapping[str, Any]:
        """Admit the connector behind ``session`` or raise ``ReleaseVerificationError``.

        The report is the recording outcome; verification has no soft outcome.
        """
        view = release_view(session)
        report: Mapping[str, Any] = {"state": "unreported"}
        served = await self._served(view)
        if served is not None:
            report = await self.recorder.record(served, self.send)
        await verify_release(view, connector, self.pins)
        return {**report, "verified": True}

    @staticmethod
    async def _served(view: _ReleaseView) -> Any:
        """The evidence the connector serves, or ``None`` when it serves none or bad evidence.

        ``verify_release`` refuses both with their own typed codes.
        """
        listed = await view.list_resources()
        if RELEASE_URI not in {str(getattr(r, "uri", "")) for r in listed}:
            return None
        try:
            return read_release_evidence(await view.read_resource(RELEASE_URI))
        except ReleaseDigestError:
            return None


class _Unset:
    """Marks an argument the caller left out."""


_UNSET = _Unset()
_installed: Callable[[], ReleaseGate | None] | None = None


def install_release_gate(factory: Callable[[], ReleaseGate | None] | None) -> None:
    """Install the process's gate factory; ``None`` removes it (the gate then refuses)."""
    global _installed
    _installed = factory


async def admit_release(
    session: Any, connector: str, *, gate: ReleaseGate | None | _Unset = _UNSET
) -> Mapping[str, Any]:
    """The one admission of a connector session: record, verify, or raise.

    ``gate`` is the caller's own gate (``None`` means it has none and refuses);
    left out, the process's installed gate is used. A process with no gate has no pin source, so it refuses with
    ``RELEASE_PIN_UNAVAILABLE`` like an unreadable record; it never admits.
    """
    if isinstance(gate, _Unset):
        gate = _installed() if _installed is not None else None
    resolved = gate
    if resolved is None:
        raise ReleaseVerificationError("RELEASE_PIN_UNAVAILABLE")
    return await resolved.admit(session, connector)
