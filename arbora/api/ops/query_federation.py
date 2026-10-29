"""The composed Arbora half of the R043.6 federated-query seam.

Natural-language planning remains an owner operation of Medulla/EG.  This
module only consumes the planner's exact prepared-query DTO, selects an
already-approved caller-scoped source binding, and sends the opaque execution
reference back through the generated EG UQL client.  In particular, there is
no NL-to-UQL translation, digest calculation, approval construction, or
connector transport in this module.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from arbora.api.errors import FleetRefusal
from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_LIMIT_FIELDS = frozenset(
    {"chunk_bytes", "chunk_rows", "deadline_ms", "max_bytes", "max_rows"}
)
_MAX_PAGES = 20
_MAX_ROWS = 10_000
_MAX_SOURCES = 32

# ``NlQueryPlanResult`` deliberately contains only the server-grounded UQL
# and its two native pins.  The source identity therefore has to be recovered
# from that exact UQL, after planning, before the already-approved Arbora
# catalog can select a binding.  This is a syntax selector, never an
# NL-to-UQL planner: unsupported/ambiguous foreign syntax is refused.
_FOREIGN_TOKEN = re.compile(r"\bFOREIGN\b", re.IGNORECASE)
_FOREIGN_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]*")

# These are Arbora's bounded transport limits.  They are deliberately kept
# separate from the native plan/query digests: the native result pins the
# query, while this process owns the maximum source-read envelope.
_DEFAULT_SOURCE_LIMITS = {
    "chunk_bytes": 64 * 1024,
    "chunk_rows": 256,
    "deadline_ms": 30_000,
    "max_bytes": 4 * 1024 * 1024,
    "max_rows": 1_000,
}


class ForeignReadDescriptor(BaseModel):
    """A non-authoritative source-read request emitted by the central planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=256)
    limits: dict[str, int]
    arguments: dict[str, Any] = Field(default_factory=dict)
    rows_key: str = Field(default="items", min_length=1, max_length=128)
    cursor_key: str = Field(default="next_cursor", min_length=1, max_length=128)
    cursor_arg: str = Field(default="cursor", min_length=1, max_length=128)
    max_pages: int = Field(default=5, ge=1, le=_MAX_PAGES)
    max_rows: int = Field(default=1000, ge=1, le=_MAX_ROWS)
    timeout_s: float = Field(default=30.0, gt=0.0, le=300.0)

    @field_validator("limits")
    @classmethod
    def _finite_limits(cls, value: dict[str, int]) -> dict[str, int]:
        if set(value) != _LIMIT_FIELDS:
            raise ValueError("foreign-read limits must match the EG contract")
        if any(isinstance(item, bool) or item <= 0 for item in value.values()):
            raise ValueError("foreign-read limits must be positive finite integers")
        if value["max_rows"] > _MAX_ROWS:
            raise ValueError("foreign-read row limit exceeds Arbora's cap")
        return value


class PreparedFederatedQuery(BaseModel):
    """Exact output of the Medulla-owned NL planner.

    The model is deliberately closed.  A planner cannot smuggle a tenant,
    actor, credential, approval, endpoint, or authority object into the
    Arbora caller path; those values come only from the verified request and
    the owner-scoped catalog binding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uql: str = Field(min_length=1, max_length=1_000_000)
    params: dict[str, Any] = Field(default_factory=dict)
    plan_digest: str = Field(min_length=64, max_length=64)
    query_digest: str = Field(min_length=64, max_length=64)
    sources: tuple[ForeignReadDescriptor, ...] = Field(
        min_length=1, max_length=_MAX_SOURCES
    )

    @field_validator("plan_digest", "query_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("planner digest must be a lowercase SHA-256")
        return value

    @field_validator("sources")
    @classmethod
    def _unique_sources(
        cls, value: tuple[ForeignReadDescriptor, ...]
    ) -> tuple[ForeignReadDescriptor, ...]:
        ids = [item.source_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("planner returned duplicate source identities")
        return value


class NaturalLanguageFederationPlanner(Protocol):
    """The only NL planning port Arbora consumes."""

    async def prepare(
        self, *, text: str, graph: str, caller: Any
    ) -> PreparedFederatedQuery: ...


def _native_source_ids(uql: str) -> tuple[str, ...]:
    """Select named foreign sources from server-produced UQL.

    The native ``NlQueryPlan`` contract intentionally carries no connector
    authority or copied catalog object.  Only the exact named-source token in
    that already-grounded UQL is used here.  HTTP/inline/ambiguous source
    forms fail closed and never become an Arbora-side interpretation.
    """

    if not isinstance(uql, str) or not uql.strip():
        raise OperationRefused("UNAVAILABLE", {"reason": "native NL plan has no UQL"})
    tokens = tuple(
        match
        for match in _FOREIGN_TOKEN.finditer(uql)
        if _uql_outside_string(uql, match.start())
    )
    matches: list[str] = []
    for token in tokens:
        position = token.end()
        while position < len(uql) and uql[position].isspace():
            position += 1
        scan = re.match(r"SCAN\b", uql[position:], re.IGNORECASE)
        if scan is not None:
            position += scan.end()
            while position < len(uql) and uql[position].isspace():
                position += 1
        if position >= len(uql):
            continue
        quote = uql[position]
        if quote in {"'", '"'}:
            end = position + 1
            while end < len(uql):
                if uql[end] == "\\":
                    end += 2
                    continue
                if uql[end] == quote:
                    break
                end += 1
            if end >= len(uql):
                continue
            name = uql[position + 1 : end]
        elif uql[position] == "`":
            end = uql.find("`", position + 1)
            if end < 0:
                continue
            name = uql[position + 1 : end]
        else:
            bare = _FOREIGN_ID.match(uql[position:])
            if bare is None:
                continue
            name = bare.group(0)
        if name.upper() not in {"HTTP", "ENGINE", "SQL"}:
            matches.append(name)
    if tokens and len(matches) != len(tokens):
        raise OperationRefused(
            "UNAVAILABLE",
            {"reason": "native NL plan has an unsupported foreign-source form"},
        )
    if len(matches) != len(set(matches)):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native NL plan repeats a foreign source"}
        )
    if len(matches) > 1:
        # The current EG UQL correlation wire carries one execution handle.
        # ``dispatch_federated_nl`` emits the canonical combined-contract
        # refusal; keep this parser from silently selecting one source.
        return tuple(matches)
    return tuple(matches)


def _uql_outside_string(text: str, offset: int) -> bool:
    """Return whether ``offset`` is outside a quoted UQL literal/identifier."""

    quote: str | None = None
    position = 0
    while position < offset:
        current = text[position]
        if quote is None:
            if current in {"'", '"'}:
                quote = current
            position += 1
            continue
        if current == "\\":
            position += 2
        elif current == quote:
            # UQL permits a doubled quote inside a literal.  Treat the pair
            # as content rather than closing the string.
            if position + 1 < offset and text[position + 1] == quote:
                position += 2
            else:
                quote = None
                position += 1
        else:
            position += 1
    return quote is None


class NativeNlQueryPlanPlanner:
    """Adapter for the generated, read-only ``NlQueryPlan`` method.

    The generated sender/decoder are injected by served composition so an
    Arbora checkout without the native artifact remains unbound.  Once the
    producer is present, this adapter sends the verified caller graph and
    consumes only native UQL/pins.  It never calls ``NlQueryExplain`` or
    computes/relabels a digest.
    """

    def __init__(
        self, send_plan: Callable[..., Awaitable[Any]], decode_plan: Callable[..., Any]
    ):
        if not callable(send_plan) or not callable(decode_plan):
            raise TypeError("native NL plan sender and decoder are required")
        self._send_plan = send_plan
        self._decode_plan = decode_plan

    async def prepare_for(
        self, *, text: str, graph: str, context: ExecutionContext
    ) -> PreparedFederatedQuery:
        from arbora.api.invoke.steps import claim_values
        from arbora.security.anonymous import client_scope

        # The generated method carries graph in both its typed params and its
        # route argument.  The caller graph was selected by the verified
        # dispatch boundary; no request graph is accepted here.
        with client_scope(context.client, claim_values(context.caller.engine_claims)):
            result = await self._send_plan(
                context.client,
                {"text": text, "graph": graph},
                graph,
                idempotency_key=context.idempotency_key,
            )
        try:
            decoded = self._decode_plan(result)
        except Exception:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native NL plan result is unavailable"}
            ) from None
        raw = _dump(decoded)
        if not isinstance(raw, Mapping):
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native NL plan result is malformed"}
            )
        # Validate only the fields owned by the generated result.  Unknown
        # authority/catalog fields are never copied into the Arbora DTO.
        if set(raw) != {"uql", "plan_digest", "query_digest"}:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native NL plan contract is unsupported"}
            )
        uql = raw["uql"]
        plan_digest = raw["plan_digest"]
        query_digest = raw["query_digest"]
        source_ids = _native_source_ids(uql)
        sources = tuple(
            ForeignReadDescriptor(
                source_id=source_id,
                limits=dict(_DEFAULT_SOURCE_LIMITS),
            )
            for source_id in source_ids
        )
        if not sources:
            raise OperationRefused(
                "UNAVAILABLE",
                {"reason": "native NL plan has no approved foreign source"},
            )
        try:
            return PreparedFederatedQuery(
                uql=uql,
                params={},
                plan_digest=plan_digest,
                query_digest=query_digest,
                sources=sources,
            )
        except ValueError:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native NL plan pins are malformed"}
            ) from None

    async def prepare(
        self, *, text: str, graph: str, caller: Any
    ) -> PreparedFederatedQuery:
        # ``dispatch_federated_nl`` uses ``prepare_for`` for this production
        # adapter.  Keeping the old protocol method fail-closed prevents a
        # caller from invoking the adapter without its verified context.
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native NL planner requires admitted context"}
        )


class SourceProvenance(BaseModel):
    """The source identity attached to a federated answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: str = Field(min_length=1, max_length=256)
    contract_iri: str = Field(min_length=1, max_length=4096)
    mapping_iri: str = Field(min_length=1, max_length=4096)


class FederatedAnswerView(BaseModel):
    """Typed projection returned by the composed query caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: list[dict[str, Any]]
    sources: list[SourceProvenance]
    native_count: int = Field(ge=0)
    virtual_count: int = Field(ge=0)
    fetched_at: datetime
    budget_used: dict[str, int | bool]


class NaturalLanguageResult(
    RootModel[list[tuple[str, float | None]] | FederatedAnswerView]
):
    """The two truthful result shapes exposed by ``query.nl``.

    EG's existing ``NlQuery`` method is a stable rows-only operation.  A
    process without the native planner/foreign-read producer must continue to
    return that exact ``[(id, score | null), ...]`` body.  When the composed
    planner is present, Arbora returns the closed federated projection instead.
    Keeping both variants in one explicit schema prevents discovery from
    advertising only the federated object while the compatibility path still
    returns native rows; neither branch invents source provenance or counts.
    """


EngineUqlSend = Callable[[Mapping[str, Any]], Awaitable[Any]]


def _dump(value: Any) -> Any:
    """Convert a generated/Pydantic DTO without accepting arbitrary objects."""

    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="python")
        except (TypeError, ValueError):
            return None
    return None


def _prepared(value: Any) -> PreparedFederatedQuery:
    if isinstance(value, PreparedFederatedQuery):
        return value
    raw = _dump(value)
    if not isinstance(raw, Mapping):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "NL planner returned no prepared query"}
        )
    try:
        return PreparedFederatedQuery.model_validate(raw)
    except ValueError:
        raise OperationRefused(
            "UNAVAILABLE",
            {"reason": "NL planner returned an unsupported query contract"},
        ) from None


def _execution(value: Any, *, query_digest: str) -> dict[str, Any]:
    raw = _dump(value)
    if not isinstance(raw, Mapping):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "foreign read returned no execution reference"}
        )
    execution_id = raw.get("execution_id")
    returned_digest = raw.get("query_digest")
    if (
        isinstance(execution_id, bool)
        or not isinstance(execution_id, int)
        or execution_id < 0
        or returned_digest != query_digest
    ):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "foreign execution identity does not match query"}
        )
    return dict(raw)


def _result_mapping(value: Any) -> Mapping[str, Any] | None:
    payload = getattr(value, "payload", value)
    raw = _dump(payload)
    return raw if isinstance(raw, Mapping) else None


def _rows(value: Any) -> list[dict[str, Any]]:
    payload = _result_mapping(value)
    if payload is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "engine query result is malformed"}
        )
    body: Any = payload
    for key in ("Rows", "Profile"):
        if key in payload:
            body = payload[key]
            break
    if not isinstance(body, Mapping) or not isinstance(body.get("rows"), list):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "engine query result has no rows"}
        )
    result: list[dict[str, Any]] = []
    for row in body["rows"]:
        rendered = _dump(row)
        if not isinstance(rendered, Mapping):
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "engine query result row is malformed"}
            )
        result.append(dict(rendered))
    return result


def _metadata(value: Any) -> Mapping[str, Any]:
    """Read optional producer counters without deriving authority from rows."""

    payload = _result_mapping(value)
    if payload is None:
        return {}
    for key in ("federation", "metadata"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return payload


def _provenance(binding: Any) -> SourceProvenance:
    source = getattr(binding, "source", None)
    mapping = getattr(source, "mapping", None)
    contract = getattr(source, "contract", None)
    connector = getattr(source, "connector", None)
    contract_iri = getattr(contract, "contract_iri", None)
    mapping_iri = getattr(mapping, "mapping_id", None)
    if not isinstance(connector, str) or not connector:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "approved source has incomplete provenance"}
        )
    if not isinstance(contract_iri, str) or not contract_iri:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "approved source has incomplete provenance"}
        )
    if not isinstance(mapping_iri, str) or not mapping_iri:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "approved source has incomplete provenance"}
        )
    return SourceProvenance(
        connector=connector,
        contract_iri=contract_iri,
        mapping_iri=mapping_iri,
    )


def _source_refusal(binding: Any, result: Any) -> FleetRefusal:
    source = getattr(binding, "source", None)
    server = getattr(source, "connector", None)
    contract = getattr(source, "contract", None)
    tool = getattr(contract, "operation", None)
    if not isinstance(server, str) or not server:
        server = "fleet"
    if not isinstance(tool, str) or not tool:
        tool = "foreign_read"
    code = getattr(result, "error_code", None)
    if not isinstance(code, str) or not code:
        code = "SOURCE_UNAVAILABLE"
    detail = getattr(result, "error_reason", None)
    return FleetRefusal(code=code, server=server, tool=tool, detail=detail)


async def dispatch_federated_nl(
    *,
    planner: NaturalLanguageFederationPlanner,
    text: str,
    graph: str,
    context: ExecutionContext,
    send_uql: EngineUqlSend,
) -> dict[str, Any]:
    """Run one composed planner/source/EG query under the admitted caller."""

    prepare_for = getattr(planner, "prepare_for", None)
    if callable(prepare_for):
        prepared = _prepared(await prepare_for(text=text, graph=graph, context=context))
    else:
        prepare = getattr(planner, "prepare", None)
        if not callable(prepare):
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "NL planner is not composed"}
            )
        prepared = _prepared(
            await prepare(text=text, graph=graph, caller=context.caller)
        )

    # The current EG wire contract carries one opaque foreign execution per
    # UQL query.  Refuse a multi-handle producer response until EG publishes
    # its combined execution DTO; never concatenate handles in Arbora.
    if len(prepared.sources) != 1:
        raise OperationRefused(
            "UNAVAILABLE",
            {"reason": "provider has no combined foreign execution contract"},
        )
    # Planner parameters may be generated UQL DTOs rather than plain Python
    # mappings.  Use Pydantic's JSON projection for the wire payload so the
    # exact typed parameter structure reaches EG without passing model objects
    # through the client serializer.
    prepared_wire = prepared.model_dump(mode="json")
    prepared_params = prepared_wire["params"]
    binding_factory = context.services.get("fleet_source_binding")
    if not callable(binding_factory):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "approved fleet source binding is not composed"}
        )

    descriptor = prepared.sources[0]
    try:
        binding = binding_factory(
            context.caller,
            descriptor.source_id,
            overall_timeout_s=descriptor.timeout_s,
        )
    except OperationRefused:
        raise
    except (LookupError, TypeError, ValueError):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "approved source binding is unavailable"}
        ) from None
    read = getattr(binding, "read", None)
    if not callable(read):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "approved source reader is not composed"}
        )

    async def correlate(execution: Any) -> Any:
        return await send_uql(
            {
                "text": prepared.uql,
                "params": prepared_params,
                "foreign_execution": _execution(
                    execution, query_digest=prepared.query_digest
                ),
            }
        )

    result = await read(
        plan_digest=prepared.plan_digest,
        query_digest=prepared.query_digest,
        limits=prepared.sources[0].limits,
        arguments=prepared.sources[0].arguments,
        rows_key=prepared.sources[0].rows_key,
        cursor_key=prepared.sources[0].cursor_key,
        cursor_arg=prepared.sources[0].cursor_arg,
        max_pages=prepared.sources[0].max_pages,
        max_rows=prepared.sources[0].max_rows,
        timeout_s=prepared.sources[0].timeout_s,
        correlate=correlate,
    )
    if not bool(getattr(result, "ok", False)):
        raise _source_refusal(binding, result)
    query_result = getattr(result, "query_result", None)
    if query_result is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "foreign read completed without a query result"}
        )
    rows = _rows(query_result)
    metadata = _metadata(query_result)
    native_count = metadata.get("native_count")
    virtual_count = metadata.get("virtual_count")
    # Source/read row totals cannot establish origin counts: joins, filters,
    # projections, and deduplication can change cardinality before the rows
    # reach this boundary.  Only the producer's authoritative pair is valid;
    # refuse a result that does not publish both counters rather than exposing
    # a fabricated native/virtual split.
    if (
        isinstance(native_count, bool)
        or not isinstance(native_count, int)
        or native_count < 0
        or isinstance(virtual_count, bool)
        or not isinstance(virtual_count, int)
        or virtual_count < 0
    ):
        raise OperationRefused(
            "UNAVAILABLE",
            {"reason": "federated result lacks authoritative source counts"},
        )
    view = FederatedAnswerView(
        rows=rows,
        sources=[_provenance(binding)],
        native_count=native_count,
        virtual_count=virtual_count,
        fetched_at=datetime.now(UTC),
        budget_used={
            "pages": int(getattr(result, "pages", 0)),
            "rows": int(getattr(result, "rows", 0)),
            "max_pages": descriptor.max_pages,
            "max_rows": descriptor.max_rows,
            "truncated": bool(getattr(result, "truncated", False)),
        },
    )
    return {"value": view.model_dump(mode="json")}


__all__ = [
    "FederatedAnswerView",
    "ForeignReadDescriptor",
    "NativeNlQueryPlanPlanner",
    "NaturalLanguageFederationPlanner",
    "PreparedFederatedQuery",
    "SourceProvenance",
    "dispatch_federated_nl",
]
