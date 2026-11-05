"""The fleet coverage gate: every registered connector is multiplexed.

ARBORA-FLEET-R060: the org registry is the ``connectors`` group of
``workspace.yml`` — the human-edited manifest that outranks the deployed MCP
config. The gate compares the registry against the enabled fleet endpoints
and the admitted catalog's tool counts: a registered connector with no
enabled endpoint is ``missing no_endpoint``, one that is not admitted is
``missing not_admitted``, and one whose catalog holds no tool item is
``zero_tools``. The check is pure and reads no clock.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: A registered connector's MCP server key, bounded like every other fleet name.
_SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")


class FleetCoverageError(Exception):
    """The org registry is missing or unparsable; the gate cannot run."""


@dataclass(frozen=True, slots=True)
class RegisteredConnector:
    """One ``workspace.yml`` ``connectors`` entry: repo plus server key."""

    repo: str
    server: str


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """The registry against the endpoints and the catalog tool counts.

    ``missing`` is a list of ``{server, reason}`` offenders; ``ok`` is true
    only with no missing connector and no zero-tool connector."""

    registered: tuple[str, ...]
    covered: tuple[str, ...]
    missing: tuple[dict[str, str], ...]
    zero_tools: tuple[str, ...]
    ok: bool


def _entry_repo_server(entry: Any) -> tuple[str, str] | None:
    """One ``connectors`` entry: a repo string, or a mapping with ``repo``.

    The MCP server key is the entry's ``mcp_server`` field, else the repo
    name; the registry owner adds the field where they differ."""
    repo: Any
    server: Any = None
    if isinstance(entry, str):
        repo = entry
    elif isinstance(entry, Mapping):
        repo = entry.get("repo")
        server = entry.get("mcp_server")
    else:
        return None
    if not isinstance(repo, str) or not _REPO.fullmatch(repo.strip()):
        return None
    repo = repo.strip()
    if server is not None:
        if not isinstance(server, str) or not _SERVER.fullmatch(server.strip()):
            return None
        server = server.strip()
    else:
        server = repo
    return repo, server


def registered_connectors(
    path: Path | None = None, exclude: Iterable[str] | None = None
) -> tuple[RegisteredConnector, ...]:
    """Read the ``connectors`` repositories of ``workspace.yml``.

    Self-served connectors (Arbora's own in-process servers) are excluded.
    A missing or unparsable file raises :class:`FleetCoverageError`; a
    parsable manifest without a ``connectors`` group registers nothing."""
    import yaml

    from arbora.core.settings import platform_config_file

    target = path or platform_config_file("workspace.yml")
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise FleetCoverageError("workspace.yml is missing or unparsable") from exc
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise FleetCoverageError("workspace.yml is missing or unparsable") from exc
    if document is None:
        document = {}
    if not isinstance(document, Mapping):
        raise FleetCoverageError("workspace.yml is missing or unparsable")
    entries = document.get("connectors")
    if entries is None:
        entries = ()
    if not isinstance(entries, (list, tuple)):
        raise FleetCoverageError("workspace.yml is missing or unparsable")
    excluded = {name for name in (exclude or ()) if isinstance(name, str)}
    registered: list[RegisteredConnector] = []
    seen: set[str] = set()
    for entry in entries:
        parsed = _entry_repo_server(entry)
        if parsed is None:
            raise FleetCoverageError("workspace.yml is missing or unparsable")
        repo, server = parsed
        if repo in excluded or server in excluded:
            continue
        if server in seen:
            raise FleetCoverageError("workspace.yml is missing or unparsable")
        seen.add(server)
        registered.append(RegisteredConnector(repo=repo, server=server))
    return tuple(sorted(registered, key=lambda item: item.server))


def check_fleet_coverage(
    registered: Iterable[RegisteredConnector],
    endpoints: Iterable[Any],
    tool_counts: Mapping[str, int],
    unadmitted: Iterable[str] = (),
) -> CoverageReport:
    """Compare the registry with the endpoints and the catalog, purely.

    Endpoint entries expose their server key as ``name``. A registered server
    with no enabled endpoint is ``no_endpoint``; one in ``unadmitted`` or
    with no admitted catalog entry is ``not_admitted``; one whose admitted
    catalog holds no tool item is ``zero_tools``. Healthy servers appear in
    ``covered`` only. The check never auto-disables or hides an offender."""
    endpoint_names = {
        name for endpoint in endpoints if (name := getattr(endpoint, "name", None))
    }
    unadmitted_set = {name for name in unadmitted if isinstance(name, str)}
    # the input order is the report order: the registry reader already returns
    # a stable order, and the check stays pure with respect to it
    rows = tuple(registered)
    covered: list[str] = []
    missing: list[dict[str, str]] = []
    zero_tools: list[str] = []
    for connector in rows:
        server = connector.server
        if server not in endpoint_names:
            missing.append({"server": server, "reason": "no_endpoint"})
        elif server in unadmitted_set or server not in tool_counts:
            missing.append({"server": server, "reason": "not_admitted"})
        elif tool_counts[server] == 0:
            zero_tools.append(server)
        else:
            covered.append(server)
    return CoverageReport(
        registered=tuple(connector.server for connector in rows),
        covered=tuple(covered),
        missing=tuple(missing),
        zero_tools=tuple(zero_tools),
        ok=not missing and not zero_tools,
    )
