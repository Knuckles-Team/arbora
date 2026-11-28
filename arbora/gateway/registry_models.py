"""The typed registry models and the per-kind specs that route and validate them (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from arbora.security.key_cache import JwksCache
from arbora.security.run_token import RunTokenSigner

logger = logging.getLogger(__name__)


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _safe_ident(name: str) -> str:
    """Admit only a plain SQL identifier for the fixed catalog tables."""
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        raise ValueError("invalid SQL identifier")
    return name


class _RegistryModel(BaseModel):
    """Strict public projection model; catalog shape errors fail closed."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RegistryServer(_RegistryModel):
    """Public desired registration metadata (never credentials)."""

    id: str
    name: str
    transport: str = ""
    url: str = ""
    enabled: bool = False


class RegistryDiscovery(_RegistryModel):
    """Privacy-safe observed discovery state for one server."""

    id: str
    server_id: str
    server_name: str
    reachable: bool = False
    last_error: str = ""
    tool_count: int = 0
    skill_count: int = 0
    prompt_count: int = 0
    resource_count: int = 0
    observed_at: str = ""


class RegistryTool(_RegistryModel):
    """Public tool contract metadata; raw schema is intentionally omitted."""

    id: str
    server_id: str
    server_name: str
    name: str
    description: str = ""
    schema_digest: str = ""
    tool_mode: str = ""
    enabled: bool = False


class RegistryPrompt(_RegistryModel):
    id: str
    server_id: str
    server_name: str
    name: str
    description: str = ""
    uri: str = ""


class RegistryResource(_RegistryModel):
    id: str
    server_id: str
    server_name: str
    uri: str = ""
    name: str = ""
    description: str = ""
    mime_type: str = ""
    resource_kind: str = ""


class RegistrySkill(_RegistryModel):
    id: str
    name: str
    description: str = ""
    uri: str = ""
    skill_type: str = ""
    classification: str = ""
    provider: str = ""
    mcp_server: str = ""
    enabled: bool = False


RegistryItem = TypeVar("RegistryItem", bound=BaseModel)


class RegistryOk(StrEnum):
    """The one status of a registry envelope that read its catalog."""

    OK = "ok"


class RegistryPage[RegistryItem: BaseModel](BaseModel):
    """Typed, bounded page envelope shared by every registry collection."""

    model_config = ConfigDict(extra="forbid")

    status: RegistryOk = RegistryOk.OK
    kind: str
    items: list[RegistryItem]
    count: int = Field(ge=0)
    next_cursor: str | None = None


class RegistryItemEnvelope[RegistryItem: BaseModel](BaseModel):
    status: RegistryOk = RegistryOk.OK
    item: RegistryItem


class RegistryKindStatus(StrEnum):
    """Whether one kind's catalog read succeeded or is unavailable."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


class RegistryKindResult(BaseModel):
    """One kind's slice of a multi-kind page — degrades independently.

    ``status="unavailable"`` (with ``reason`` set) means THIS kind's catalog
    read failed; it is never conflated with "zero items", which is a
    genuine, successful empty page (``status="ok"``, ``items=[]``). Mirrors
    ``overstory.api_extensions._read_fleet_catalog``'s own per-kind
    ``None``-on-failure contract, translated into this route's typed
    envelope shape instead of a raw ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    status: RegistryKindStatus = RegistryKindStatus.OK
    items: list[dict[str, Any]] = Field(default_factory=list)
    count: int = Field(default=0, ge=0)
    next_cursor: str | None = None
    reason: str | None = None


class RegistryMultiKindPage(BaseModel):
    """``GET /api/registry?kinds=...`` envelope: one request, N kinds.

    ``kinds`` is keyed by the requested kind name. Pagination is PER KIND —
    each ``RegistryKindResult.next_cursor`` is fed back independently as
    that same kind's ``cursor_<kind>`` query parameter on the next request;
    there is no single global cursor advancing every kind in lockstep (a
    caller may be on page 3 of ``skills`` while still on page 1 of
    ``servers``). See ``_parse_multi_kind_request`` for the exact query
    parameter contract.
    """

    model_config = ConfigDict(extra="forbid")

    status: RegistryOk = RegistryOk.OK
    kinds: dict[str, RegistryKindResult]


class _KindSpec:
    __slots__ = (
        "table",
        "columns",
        "model",
        "name_column",
        "authority_column",
        "principal_column",
        "grant_column",
    )

    def __init__(
        self,
        table: str,
        columns: tuple[str, ...],
        model: type[BaseModel],
        *,
        name_column: str = "name",
        authority_column: str | None = None,
        principal_column: str | None = None,
        grant_column: str | None = None,
    ) -> None:
        self.table = _safe_ident(table)
        self.columns = tuple(_safe_ident(column) for column in columns)
        self.model = model
        self.name_column = _safe_ident(name_column)
        self.authority_column = (
            _safe_ident(authority_column) if authority_column else None
        )
        self.principal_column = (
            _safe_ident(principal_column) if principal_column else None
        )
        self.grant_column = _safe_ident(grant_column) if grant_column else None


#
# PHASE A investigation — three sections `/api/enhanced/tools` still serves
# from the filesystem/KG (`builtin_tools`, `skill_graphs`, `skill_workflows`,
# see `arbora.api.domain.mcp_server.backing.engine_routing._build_tools_payload_sync`'s own
# docstring, "FIX LANE (collapse-tool-endpoints)") were evaluated as
# candidate additional `_KindSpec` entries here. None were added. Evidence:
#
# * ``builtin_tools`` — no SQL table exists at all. These are native,
#   in-process Python callables under ``meristem/tools/*.py``, never
#   MCP-discovered and never written to any fleet-catalog table. Adding a
#   `_KindSpec` for this would require fabricating a table, which is exactly
#   what this task was told not to do.
# * ``skill_graphs`` — the ``skills`` table's schema *can* represent a
#   skill-graph row (``skill_type='graph'``), but
#   ``knowledge_graph/ingestion/skill_workflow_ingest.py`` explicitly SKIPS
#   ``skill_type: graph`` files during ingestion ("left for its own
#   ingester"), and nothing schedules that ingester automatically (only a
#   manual, explicit-``root`` on-demand action reaches it). In a typical
#   deployment the catalog rows for this kind are simply absent even though
#   the on-disk ``skill-graphs`` corpus is real and populated — serving this
#   kind from the catalog would silently show an empty page for a genuinely
#   non-empty corpus, the "losing freshness" failure mode this task called
#   out by name. Left out.
# * ``skill_workflows`` — the ``skills`` table CAN represent
#   id/name/description/enabled for a ``skill_type='workflow'`` row (these
#   DO get ingested on the automatic ``package_install`` tick, unlike
#   ``skill_graphs``), but the table itself
#   (``knowledge_graph/core/fleet_catalog_tables.py``'s ``TABLE_SKILLS`` DDL)
#   has no ``domain``/``tags`` columns at all — real fields on the existing
#   filesystem-sourced payload, parsed from each ``SKILL.md``'s frontmatter.
#   Adding this kind would silently blank those two fields for every caller
#   that switches to it. A prior lane evaluated this EXACT trade-off for
#   this EXACT data (`_build_tools_payload_sync`'s own docstring, "Moving
#   these two sections would silently blank domain/tags ... exactly the
#   'fabricate or silently drop' failure mode this fix lane was told to
#   avoid") and chose to keep it filesystem-sourced; no new evidence here
#   overturns that call, so it stays out too.
#
# All three remain reachable only through the existing filesystem/KG path
# (`GET /tools` / `/api/enhanced/tools`) until a real ingester exists for
# skill-graphs and the `skills` table grows `domain`/`tags` columns.
#
KIND_SPECS: Mapping[str, _KindSpec] = MappingProxyType(
    {
        "servers": _KindSpec(
            "mcp_servers",
            ("id", "tenant_id", "name", "transport", "url", "enabled"),
            RegistryServer,
        ),
        "discoveries": _KindSpec(
            "mcp_server_discovery",
            (
                "id",
                "tenant_id",
                "server_id",
                "server_name",
                "reachable",
                "last_error",
                "tool_count",
                "skill_count",
                "prompt_count",
                "resource_count",
                "observed_at",
                "discovery_authority_kind",
                "discovery_principal",
                "discovery_grant_digest",
            ),
            RegistryDiscovery,
            name_column="server_name",
            authority_column="discovery_authority_kind",
            principal_column="discovery_principal",
            grant_column="discovery_grant_digest",
        ),
        "tools": _KindSpec(
            "mcp_tools",
            (
                "id",
                "tenant_id",
                "server_id",
                "server_name",
                "name",
                "description",
                "schema_digest",
                "tool_mode",
                "enabled",
                "discovery_authority_kind",
                "discovery_principal",
                "discovery_grant_digest",
            ),
            RegistryTool,
            authority_column="discovery_authority_kind",
            principal_column="discovery_principal",
            grant_column="discovery_grant_digest",
        ),
        "prompts": _KindSpec(
            "mcp_prompts",
            (
                "id",
                "tenant_id",
                "server_id",
                "server_name",
                "name",
                "description",
                "uri",
                "discovery_authority_kind",
                "discovery_principal",
                "discovery_grant_digest",
            ),
            RegistryPrompt,
            authority_column="discovery_authority_kind",
            principal_column="discovery_principal",
            grant_column="discovery_grant_digest",
        ),
        "resources": _KindSpec(
            "mcp_resources",
            (
                "id",
                "tenant_id",
                "server_id",
                "server_name",
                "uri",
                "name",
                "description",
                "mime_type",
                "resource_kind",
                "discovery_authority_kind",
                "discovery_principal",
                "discovery_grant_digest",
            ),
            RegistryResource,
            authority_column="discovery_authority_kind",
            principal_column="discovery_principal",
            grant_column="discovery_grant_digest",
        ),
        "skills": _KindSpec(
            "skills",
            (
                "id",
                "tenant_id",
                "name",
                "description",
                "uri",
                "skill_type",
                "classification",
                "provider",
                "mcp_server",
                "enabled",
                "discovery_authority_kind",
                "discovery_principal",
                "discovery_grant_digest",
            ),
            RegistrySkill,
            authority_column="discovery_authority_kind",
            principal_column="discovery_principal",
            grant_column="discovery_grant_digest",
        ),
    }
)


_RESPONSE_MODELS: Mapping[str, tuple[Any, Any]] = MappingProxyType(
    {
        "servers": (RegistryPage[RegistryServer], RegistryItemEnvelope[RegistryServer]),
        "discoveries": (
            RegistryPage[RegistryDiscovery],
            RegistryItemEnvelope[RegistryDiscovery],
        ),
        "tools": (RegistryPage[RegistryTool], RegistryItemEnvelope[RegistryTool]),
        "prompts": (RegistryPage[RegistryPrompt], RegistryItemEnvelope[RegistryPrompt]),
        "resources": (
            RegistryPage[RegistryResource],
            RegistryItemEnvelope[RegistryResource],
        ),
        "skills": (RegistryPage[RegistrySkill], RegistryItemEnvelope[RegistrySkill]),
    }
)


def _signer_of(request: Any) -> RunTokenSigner:
    """The run-token signer of the host that serves ``request``."""
    return request.app.state.host.authority.signer


def _keys_of(request: Any) -> JwksCache:
    """The key cache of the host that serves ``request``."""
    return request.app.state.host.caches.jwks
