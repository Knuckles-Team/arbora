"""Explicit local browser transport checks, without operation authority."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit

from arbora.api.contracts.session import GraphSession
from arbora.security import anonymous


def _local_origin_authority(parsed: SplitResult) -> bool:
    return (
        parsed.scheme in {"http", "https"}
        and anonymous.loopback(parsed.hostname or "")
        and parsed.port is not None
        and parsed.username is None
        and parsed.password is None
    )


def _canonical_origin(origin: str, parsed: SplitResult) -> bool:
    return (
        not parsed.path
        and not parsed.query
        and not parsed.fragment
        and origin == f"{parsed.scheme}://{parsed.netloc}"
        and origin.isascii()
        and not any(character.isspace() for character in origin)
    )


@dataclass(frozen=True, slots=True)
class LocalBrowserTransport:
    """Owner-configured transport only; never an admitted caller or grant.

    This boundary is deliberately separate from raw anonymous clients, which
    continue to reject every Origin header. It cannot register a channel or
    authorize a proposal. The composed owner must separately qualify those
    operations, their bounded durable authority and exact-request policy.
    """

    origin: str

    def __post_init__(self) -> None:
        if not anonymous.enabled():
            raise PermissionError("Local browser mode is not configured")
        if type(self.origin) is not str:
            raise ValueError("Exact literal-loopback browser origin required")
        try:
            parsed = urlsplit(self.origin)
            valid = _local_origin_authority(parsed) and _canonical_origin(
                self.origin, parsed
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Exact literal-loopback browser origin required")

    def validate(self, scope: Any, session: GraphSession) -> None:
        """Recheck transport and unchanged anonymous provenance on every use."""
        if not isinstance(session, GraphSession):
            raise PermissionError("Canonical anonymous session required")
        anonymous.validate_session(session)
        session.ensure_authority_current()
        parsed = urlsplit(self.origin)
        kind = scope.get("type")
        scheme = (
            parsed.scheme
            if kind == "http"
            else {"http": "ws", "https": "wss"}[parsed.scheme]
        )
        peer = scope.get("client") or ()
        if (
            kind not in {"http", "websocket"}
            or scope.get("scheme") != scheme
            or not peer
            or not anonymous.loopback(str(peer[0]))
        ):
            raise PermissionError("Local browser transport mismatch")
        self._validate_headers(tuple(scope.get("headers", ())), parsed.netloc)

    def _validate_headers(self, headers: tuple[Any, ...], host: str) -> None:
        expected = {
            b"host": host.encode("ascii"),
            b"origin": self.origin.encode("ascii"),
        }
        seen: dict[bytes, list[bytes]] = {name: [] for name in expected}
        for name, value in headers:
            name = name.lower()
            if name in {b"authorization", b"cookie", b"forwarded"} or name.startswith(
                b"x-forwarded-"
            ):
                raise PermissionError(
                    "Local browser credentials or proxy headers refused"
                )
            if name in seen:
                seen[name].append(value)
        if any(seen[name] != [value] for name, value in expected.items()):
            raise PermissionError("Exact browser Origin and Host required")
