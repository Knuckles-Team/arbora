"""Engine-method passthrough operations for SPARQL, SQL, ontology and federation.

ARBORA-OPS-R018.1 / R019.1: Medulla owns every fact, schema and query
language, so these operations are thin curated names over the installed engine
contract.  Each binds one wire method through the registry's engine-method
passthrough and takes its scope, effect and parameter/result schemas from that
method; Arbora keeps no local ontology store and builds no SQL strings.
"""

from __future__ import annotations

from arbora.api.registry import (
    AuditClass,
    Effect,
    EgMethod,
    EgSchemaRef,
    Idempotency,
    OpSpec,
    PrincipalRule,
    Surface,
    Verb,
)

_REQUEST = "contract/schemas/method.request.json#/methods/{method}/properties/params"
_RESULT = "contract/schemas/result.{domain}.json#/methods/{method}"


def _read(
    op_id: str, method: str, domain: str, scope: str, summary: str, example: str
) -> OpSpec:
    return OpSpec(
        id=op_id,
        verb=Verb.ASK,
        summary=summary,
        examples=(example,),
        params=EgSchemaRef(path=_REQUEST.format(method=method)),
        result=EgSchemaRef(path=_RESULT.format(domain=domain, method=method)),
        binding=EgMethod(service=method, op=method),
        scopes=frozenset({scope}),
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        _read(
            "query.sparql",
            "Sparql",
            "reasoning",
            "sparql:read",
            "Evaluate a SPARQL SELECT over the caller's graph",
            "run a SPARQL query",
        ),
        _read(
            "query.sql",
            "Sql",
            "query",
            "query:sql",
            "Run a parameterised SQL query over the caller's graph",
            "run a SQL query",
        ),
        _read(
            "ontology.inspect",
            "OntologyInspect",
            "reasoning",
            "validation:read",
            "Inspect ontology documents or the graph's composed schema",
            "inspect this ontology",
        ),
        _read(
            "ontology.schema.classes",
            "GraphSchemaClasses",
            "reasoning",
            "owl:read",
            "Page the classes and properties the graph's schema declares",
            "list the ontology classes",
        ),
        _read(
            "ontology.validate",
            "ShaclValidate",
            "reasoning",
            "validation:read",
            "Validate graph data against SHACL shapes",
            "validate the graph against its shapes",
        ),
        _read(
            "federation.sources.columns",
            "QueryForeignColumns",
            "query",
            "federation:read",
            "Read the columns of a registered foreign source",
            "show the columns of this federated source",
        ),
        OpSpec(
            id="federation.sources.register",
            verb=Verb.MANAGE,
            summary="Register a foreign source with the engine (governed write)",
            examples=("register a federated source",),
            params=EgSchemaRef(path=_REQUEST.format(method="RegisterForeignSource")),
            result=EgSchemaRef(
                path=_RESULT.format(domain="cluster", method="RegisterForeignSource")
            ),
            binding=EgMethod(
                service="RegisterForeignSource", op="RegisterForeignSource"
            ),
            scopes=frozenset({"federation:admin"}),
            effect=Effect.ADMIN,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
            principals=PrincipalRule.HUMAN_UNDELEGATED,
            surfaces=frozenset(
                {Surface.MCP, Surface.HTTP, Surface.A2A, Surface.CONSOLE}
            ),
        ),
    )


specs = operations
