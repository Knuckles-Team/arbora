"""The fleet endpoints and the capture of one server's pack surface (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from arbora.core.settings import RuntimeConfig
from arbora.fleet.declarations import (
    RESOURCES_URI,
    Declarations,
    entry_bytes,
    parse_declaration,
)

logger = logging.getLogger(__name__)


#: The per-request timeout of one child catalog capture.
CAPTURE_TIMEOUT_S = 60.0


_HTTP_TRANSPORTS = frozenset({"", "http", "streamable-http"})


@dataclass(frozen=True, slots=True)
class FleetEndpoint:
    """One configured fleet server: registry name and served MCP URL."""

    name: str
    url: str


def _endpoint(name: object, spec: object) -> FleetEndpoint | None:
    if not isinstance(name, str) or not isinstance(spec, dict):
        return None
    url = spec.get("url")
    transport = str(spec.get("transport") or spec.get("type") or "")
    if spec.get("disabled") or not isinstance(url, str):
        return None
    if not url.startswith(("http://", "https://")):
        return None
    if transport.replace("_", "-") not in _HTTP_TRANSPORTS:
        return None
    return FleetEndpoint(name=name, url=url)


def fleet_endpoints(
    raw: bytes, *, exclude: Collection[str] = ()
) -> tuple[FleetEndpoint, ...]:
    """Return every enabled streamable-HTTP server in an MCP config payload."""

    from arbora.fleet.mcp_catalog import parse_mcp_servers

    endpoints = (_endpoint(name, spec) for name, spec in parse_mcp_servers(raw).items())
    return tuple(
        sorted(
            (item for item in endpoints if item and item.name not in exclude),
            key=lambda item: item.name,
        )
    )


def configured_fleet_endpoints(
    *, exclude: Collection[str] = (), path: Path | None = None
) -> tuple[FleetEndpoint, ...]:
    """Read the deployment's MCP config; an absent file has no fleet."""

    from arbora.core.settings import mcp_config_path

    config = path or mcp_config_path()
    if not config.is_file():
        return ()
    return fleet_endpoints(config.read_bytes(), exclude=exclude)


def self_served_connectors() -> tuple[str, ...]:
    """The connectors Arbora serves in process; never fleet children."""

    from arbora.semantic_content import required_content_connectors

    return required_content_connectors()


def _resource_scheme(resource: Any) -> str:
    scheme, separator, _ = str(getattr(resource, "uri", "")).partition("://")
    return scheme if separator else ""


class _PackResourceSession:
    """A session view that lists only ConnectorPack-schemed resources.

    A fleet server may also serve live data resources. Those are not catalog
    content, so the capture never reads them.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_resources(self) -> list[Any]:
        from scion_sdk.artifacts.resources import _RESOURCE_SCHEMES

        listed = await self._session.list_resources()
        kept = [
            resource
            for resource in listed
            if _resource_scheme(resource) in _RESOURCE_SCHEMES
            or str(getattr(resource, "uri", "")) == RESOURCES_URI
        ]
        # The catalog names a resource by its pack entry name and reads it by
        # that name. The declaration is served as ``connector-resources``; name
        # it by its URI so the relays read it by URI like every other resource.
        return [
            SimpleNamespace(
                uri=resource.uri,
                name=str(resource.uri),
                mimeType=getattr(resource, "mimeType", None),
                annotations=getattr(resource, "annotations", None),
            )
            if str(getattr(resource, "uri", "")) == RESOURCES_URI
            else resource
            for resource in kept
        ]

    async def read_resource(self, uri: str) -> str:
        return await self._session.read_resource(uri)


#: Seconds between capture requests to one connector. The connector SDK's
#: default per-caller limit is 10 requests/s with a burst of 20, and a pack
#: capture makes one request per skill, prompt and resource, so an unpaced
#: capture of a large pack is throttled mid-way (ARBORA-FLEET-R041.12).
CAPTURE_REQUEST_INTERVAL_S = 0.125


class _PacedSession:
    """Spaces the capture's MCP requests so a connector never throttles them."""

    def __init__(
        self, session: Any, interval_s: float = CAPTURE_REQUEST_INTERVAL_S
    ) -> None:

        self._session = session
        self._interval_s = interval_s
        self._lock = asyncio.Lock()
        self._last = float("-inf")

    def __getattr__(self, name: str) -> Any:
        import inspect

        target = getattr(self._session, name)
        if not inspect.iscoroutinefunction(target):
            return target

        async def paced(*args: Any, **kwargs: Any) -> Any:
            async with self._lock:
                wait = self._last + self._interval_s - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            return await target(*args, **kwargs)

        return paced


def _capture_kinds() -> tuple[Any, ...]:
    from scion_sdk.artifacts.prompts import PromptArtifactKind
    from scion_sdk.artifacts.resources import ResourceArtifactKind
    from scion_sdk.artifacts.skills import SkillArtifactKind
    from scion_sdk.artifacts.tools import ToolArtifactKind

    class PackResourceKind(ResourceArtifactKind):
        async def list_entries(self, session: Any, server: Any) -> Any:
            # The view implements the McpSession reads by duck typing.
            view = cast(Any, _PackResourceSession(session))
            return await super().list_entries(view, server)

    return (
        ToolArtifactKind(),
        SkillArtifactKind(),
        PromptArtifactKind(),
        PackResourceKind(),
    )


def _remember_declaration(
    declarations: Declarations, connector: str, pack: Any
) -> None:
    """Keep the pack's ``connector://resources`` declaration; absent clears it."""

    declaration = None
    for entry in pack.archive.entries:
        if str(getattr(entry, "uri", "")) == RESOURCES_URI:
            try:
                declaration = parse_declaration(entry_bytes(pack.archive, entry))
            except (ValueError, TypeError) as exc:
                logger.warning("%s: unreadable %s: %s", connector, RESOURCES_URI, exc)
            break
    declarations.remember(connector, declaration)


def _child_auth(config: RuntimeConfig) -> Any:
    """Arbora's service credential for a child, as the multiplexer sends it."""

    from arbora.core.httpx_boundary import coerce_httpx2_auth
    from arbora.security.client_credentials import child_auth

    return coerce_httpx2_auth(child_auth(None, config))


async def capture_server_pack(endpoint: FleetEndpoint, *, auth: Any = None) -> Any:
    """Capture one live fleet server's catalog as a ConnectorPack."""

    from scion_sdk.artifacts.pack import build_content_pack
    from scion_sdk.ports.session import TransportEndpoint
    from scion_sdk.transports.mcp import McpTransport

    # release-gate: covered by arbora.fleet.onboarding.FleetOnboarding._onboard_pass
    transport_endpoint = TransportEndpoint(
        url=endpoint.url, timeout_seconds=CAPTURE_TIMEOUT_S, auth=auth
    )
    async with McpTransport().session(transport_endpoint) as session:
        return await build_content_pack(
            cast(Any, _PacedSession(session)),
            connector=endpoint.name,
            kinds=_capture_kinds(),
        )


@contextlib.asynccontextmanager
async def open_release_session(
    endpoint: FleetEndpoint, *, auth: Any = None
) -> AsyncIterator[Any]:
    """One live session to a fleet server, over which its release is verified.

    The release gate reads ``connector://release`` through this session before
    the pack is captured (ARBORA-HOST-R037.3).
    """

    # release-gate: covered by arbora.fleet.onboarding.FleetOnboarding._admit_release

    from scion_sdk.ports.session import TransportEndpoint
    from scion_sdk.transports.mcp import McpTransport

    transport_endpoint = TransportEndpoint(
        url=endpoint.url, timeout_seconds=CAPTURE_TIMEOUT_S, auth=auth
    )
    async with McpTransport().session(transport_endpoint) as session:
        yield session
