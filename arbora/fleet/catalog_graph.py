"""The agent-library catalog an onboarding pass writes into EG (ARBORA-FLEET-R041.17).

Each onboarded connector's tools, prompts, skills and data resources become
typed graph nodes under the EG core metadata-catalog vocabulary
(``core:metadata_catalog@1``): ``cat:ToolAsset``, ``cat:PromptAsset``,
``cat:SkillAsset`` and ``cat:DataResource``, each ``cp:ofConnector`` its
``cp:Connector`` with its ``cp:digest``. A prompt that declares a
system-prompt target (its ``team.knuckles/prompt`` ``systemPromptFor`` or
``useCase`` meta, or the ``system`` tag) is also a ``:PromptCandidate`` with a
``:systemPromptFor`` edge to a ``:Task``. A data resource names the class its
records serve (``cp:servesClass``) and a lineage edge from the tool that reads
it. Catalog questions are then graph queries through ``ask`` and ``find``;
there is no catalog read operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any
from urllib.parse import quote

from scion_sdk.ontology_namespaces import CORE_NAMESPACE, module_namespace

logger = logging.getLogger(__name__)

KG = CORE_NAMESPACE
CAT = module_namespace("catalog")
CP = module_namespace("connector-pack")
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDFS_COMMENT = "http://www.w3.org/2000/01/rdf-schema#comment"
SCHEMA = "http://schema.org/"
PROMPT_META_KEY = "team.knuckles/prompt"
RESOURCES_URI = "connector://resources"
_CLASS_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")

Triple = tuple[str, str, str | tuple[str]]
"""``(subject, predicate, object)``; an IRI object is a str, a literal a 1-tuple."""


def connector_iri(connector: str) -> str:
    return f"urn:arbora:connector:{quote(connector, safe='')}"


def asset_iri(connector: str, kind: str, name: str) -> str:
    return f"urn:arbora:asset:{quote(connector, safe='')}:{kind}:{quote(name, safe='')}"


def task_iri(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "task"
    return f"urn:arbora:task:{slug}"


def _body(pack: Any, entry: Any) -> bytes:
    section = entry.body
    if isinstance(section, bytes | bytearray):
        return bytes(section)
    data = pack.archive.data
    return bytes(data[section.offset : section.offset + section.length])


def _json(body: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _kind(entry: Any) -> str:
    kind = getattr(entry, "kind", "")
    return str(getattr(kind, "value", kind))


def prompt_targets(definition: Mapping[str, Any]) -> list[str]:
    """The tasks a prompt declares it can frame as a system prompt.

    Read from the ``team.knuckles/prompt`` meta (``systemPromptFor``, then
    ``useCase``). A prompt tagged ``system`` with neither frames its own title.
    Nothing is inferred from the prompt text.
    """
    meta = definition.get("_meta") or definition.get("meta") or {}
    declared = meta.get(PROMPT_META_KEY) if isinstance(meta, Mapping) else None
    declared = declared if isinstance(declared, Mapping) else {}
    targets: list[str] = []
    for key in ("systemPromptFor", "useCase"):
        value = declared.get(key)
        values = value if isinstance(value, list) else [value]
        targets.extend(
            str(v).strip() for v in values if isinstance(v, str) and v.strip()
        )
    tags = set()
    fastmcp = (
        meta.get("fastmcp") or meta.get("_fastmcp")
        if isinstance(meta, Mapping)
        else None
    )
    if isinstance(fastmcp, Mapping):
        tags = {str(t) for t in fastmcp.get("tags") or ()}
    if not targets and "system" in tags:
        title = definition.get("title") or definition.get("name")
        if isinstance(title, str) and title.strip():
            targets.append(title.strip())
    return list(dict.fromkeys(targets))


def _tool_action(definition: Mapping[str, Any]) -> str:
    annotations = definition.get("annotations") or {}
    if isinstance(annotations, Mapping):
        if annotations.get("readOnlyHint") or annotations.get("read_only_hint"):
            return f"{SCHEMA}SearchAction"
        if annotations.get("destructiveHint") or annotations.get("destructive_hint"):
            return f"{SCHEMA}DeleteAction"
    return f"{SCHEMA}UpdateAction"


def catalog_triples(connector: str, pack: Any) -> list[Triple]:
    """The catalog triples for one captured pack, in a stable order."""
    triples: list[Triple] = []
    conn = connector_iri(connector)
    triples += [(conn, RDF_TYPE, f"{CP}Connector"), (conn, RDFS_LABEL, (connector,))]
    tools: dict[str, str] = {}
    for entry in pack.archive.entries:
        kind, name = _kind(entry), str(entry.name)
        body = _body(pack, entry)
        digest = hashlib.sha256(body).hexdigest()
        if kind == "tool":
            node = asset_iri(connector, "tool", name)
            tools[name] = node
            document = _json(body)
            triples += _asset(node, f"{CAT}ToolAsset", name, conn, digest)
            triples.append((node, f"{CAT}actionType", _tool_action(document)))
            description = document.get("description")
            if isinstance(description, str) and description.strip():
                triples.append((node, RDFS_COMMENT, (description.strip()[:500],)))
        elif kind == "prompt":
            node = asset_iri(connector, "prompt", name)
            triples += _asset(node, f"{CAT}PromptAsset", name, conn, digest)
            definition = _json(body).get("definition") or {}
            definition = definition if isinstance(definition, Mapping) else {}
            description = definition.get("description")
            if isinstance(description, str) and description.strip():
                triples.append((node, RDFS_COMMENT, (description.strip()[:500],)))
            targets = prompt_targets(definition)
            if targets:
                triples.append((node, RDF_TYPE, f"{KG}PromptCandidate"))
            for target in targets:
                task = task_iri(target)
                triples += [
                    (node, f"{KG}systemPromptFor", task),
                    (task, RDF_TYPE, f"{KG}Task"),
                    (task, RDFS_LABEL, (target,)),
                ]
                if _CLASS_NAME.match(target):
                    triples.append((task, f"{CP}servesClass", f"{KG}{target}"))
        elif kind == "skill":
            node = asset_iri(connector, "skill", name)
            triples += _asset(node, f"{CAT}SkillAsset", name, conn, digest)
    triples += _data_resources(connector, conn, pack, tools)
    return triples


def _asset(node: str, cls: str, name: str, conn: str, digest: str) -> list[Triple]:
    return [
        (node, RDF_TYPE, cls),
        (node, RDFS_LABEL, (name,)),
        (node, f"{CP}ofConnector", conn),
        (node, f"{CP}digest", (digest,)),
    ]


def _data_resources(
    connector: str, conn: str, pack: Any, tools: Mapping[str, str]
) -> Iterator[Triple]:
    """One ``cat:DataResource`` per declared source, with class and lineage."""
    for entry in pack.archive.entries:
        if str(getattr(entry, "uri", "")) != RESOURCES_URI:
            continue
        body = _body(pack, entry)
        declaration = _json(body)
        mappings = declaration.get("schema_mappings") or {}
        for source in declaration.get("sources") or ():
            if not isinstance(source, Mapping) or not source.get("source_id"):
                continue
            node = asset_iri(connector, "resource", str(source["source_id"]))
            digest = hashlib.sha256(
                json.dumps(source, sort_keys=True).encode("utf-8")
            ).hexdigest()
            yield from _asset(
                node, f"{CAT}DataResource", str(source["source_id"]), conn, digest
            )
            description = source.get("description")
            if isinstance(description, str) and description.strip():
                yield (node, RDFS_COMMENT, (description.strip()[:500],))
            record_type = str(source.get("record_type") or "")
            mapping = (
                mappings.get(record_type) if isinstance(mappings, Mapping) else None
            )
            cls = (
                (mapping or {}).get("ontology_class")
                if isinstance(mapping, Mapping)
                else None
            )
            for name in dict.fromkeys(
                c for c in (record_type, cls) if c and _CLASS_NAME.match(c)
            ):
                yield (node, f"{CP}servesClass", f"{KG}{name}")
            for tool in source.get("tools") or ():
                if tool in tools:
                    yield (tools[tool], f"{CAT}lineageTo", node)
        break


def _term(value: str | tuple[str]) -> str:
    if isinstance(value, tuple):
        text = value[0]
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    return f"<{value}>"


def to_ntriples(triples: Iterable[Triple]) -> str:
    """N-Triples for ``triples``, each line once."""
    lines = dict.fromkeys(f"<{s}> <{p}> {_term(o)} ." for s, p, o in triples)
    return "\n".join(lines) + "\n"


# -- R041.17.1: owner, version, hop, schema nodes ----------------------------------

PACK_VERSION = f"{CP}packVersion"
DECLARED_OWNER = f"{CP}declaredOwner"
FLAG = f"{CAT}flag"
_ASSET_KINDS = frozenset(
    f"{CAT}{name}"
    for name in (
        "ToolAsset",
        "PromptAsset",
        "SkillAsset",
        "DataResource",
        "Field",
        "SchemaAsset",
        "KeyAsset",
        "IndexAsset",
        "ViewAsset",
    )
)


def principal_iri(principal: str) -> str:
    if principal.startswith(("urn:", "http://", "https://")):
        return principal
    return f"urn:arbora:principal:{quote(principal, safe='')}"


def _declaration(pack: Any) -> Mapping[str, Any]:
    for entry in pack.archive.entries:
        if str(getattr(entry, "uri", "")) == RESOURCES_URI:
            return _json(_body(pack, entry))
    return {}


def declared_owner(pack: Any) -> str | None:
    """The owner the pack declares for itself, from its resources declaration."""
    owner = _declaration(pack).get("owner")
    return owner.strip() if isinstance(owner, str) and owner.strip() else None


def _stewardship(asset: str, principal: str) -> list[Triple]:
    node = asset.replace("urn:arbora:asset:", "urn:arbora:steward:", 1)
    return [
        (node, RDF_TYPE, f"{CAT}StewardshipAssignment"),
        (node, f"{CAT}assignedTo", asset),
        (node, f"{CAT}principal", principal_iri(principal)),
        (node, f"{CAT}role", f"{CAT}owner"),
    ]


def _hop(source: str, target: str, hop: str) -> list[Triple]:
    node = (
        f"urn:arbora:lineage:{hop}:{quote(source, safe='')}:to:{quote(target, safe='')}"
    )
    return [
        (node, RDF_TYPE, f"{CAT}LineageEdge"),
        (node, f"{CAT}from", source),
        (node, f"{CAT}to", target),
        (node, f"{CAT}hop", (hop,)),
        (source, f"{CAT}lineageTo", target),
    ]


def _node(connector: str, kind: str, name: str, cls: str, spec: Any) -> list[Triple]:
    node = asset_iri(connector, kind, name)
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode())
    return _asset(
        node, f"{CAT}{cls}", name, connector_iri(connector), digest.hexdigest()
    )


def _field_mapped(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(value.get(k) for k in ("property", "predicate", "ontology_property"))
    return bool(value)


def _source_nodes(
    connector: str, source: Mapping[str, Any], fields: Any
) -> list[Triple]:
    sid = str(source["source_id"])
    resource = asset_iri(connector, "resource", sid)
    out = _node(connector, "schema", sid, "SchemaAsset", source)
    out.append((asset_iri(connector, "schema", sid), f"{CAT}schemaTable", resource))
    for name in sorted(fields if isinstance(fields, Mapping) else ()):
        node = asset_iri(connector, "field", f"{sid}.{name}")
        out += _node(connector, "field", f"{sid}.{name}", "Field", [name, fields[name]])
        out += [(resource, f"{CAT}hasField", node), (node, f"{CAT}columnName", (name,))]
        out += _hop(resource, node, "resource")
        if not _field_mapped(fields[name]):
            out.append((node, FLAG, ("field_unmapped",)))
    return out


def _key_nodes(connector: str, source: Mapping[str, Any]) -> list[Triple]:
    key = source.get("key_field")
    if not isinstance(key, str) or not key:
        return []
    sid = str(source["source_id"])
    out = _node(connector, "key", f"{sid}.{key}", "KeyAsset", [sid, key])
    node = asset_iri(connector, "key", f"{sid}.{key}")
    return out + [
        (node, f"{CAT}keyKind", ("primary",)),
        (node, f"{CAT}keyOf", asset_iri(connector, "resource", sid)),
        (node, f"{CAT}keyField", asset_iri(connector, "field", f"{sid}.{key}")),
    ]


def _schema_nodes(connector: str, declaration: Mapping[str, Any]) -> list[Triple]:
    out: list[Triple] = []
    mappings = declaration.get("schema_mappings")
    for source in declaration.get("sources") or ():
        if not isinstance(source, Mapping) or not source.get("source_id"):
            continue
        mapping = (mappings or {}).get(str(source.get("record_type") or ""))
        fields = mapping.get("fields") if isinstance(mapping, Mapping) else None
        out += _source_nodes(connector, source, fields)
        out += _key_nodes(connector, source)
    return out


def _asset_subjects(triples: Iterable[Triple]) -> list[str]:
    return sorted({s for s, p, o in triples if p == RDF_TYPE and o in _ASSET_KINDS})


def _ownership(
    subjects: Iterable[str], owner: str | None, importer: str
) -> list[Triple]:
    out: list[Triple] = []
    for asset in subjects:
        out += _stewardship(asset, owner or importer)
        if owner is None:
            out.append((asset, FLAG, ("owner_missing",)))
    return out


def _hop_triples(conn: str, base: list[Triple]) -> list[Triple]:
    """The connector-to-tool and tool-to-resource lineage hops of ``base``."""
    out: list[Triple] = []
    for s, p, o in base:
        if p == f"{CAT}lineageTo" and isinstance(o, str):
            out += _hop(s, o, "operation")
        if p == RDF_TYPE and o == f"{CAT}ToolAsset":
            out += _hop(conn, s, "connector")
    return out


def enrichment_triples(
    connector: str,
    pack: Any,
    base: list[Triple],
    *,
    importer: str,
    version: str | None,
) -> list[Triple]:
    """Owner, version, lineage-hop and schema nodes for the asset set (R041.17.1)."""
    extra = _schema_nodes(connector, _declaration(pack))
    subjects = _asset_subjects(base + extra)
    out = extra + _ownership(subjects, declared_owner(pack), importer)
    out += _hop_triples(connector_iri(connector), base)
    return out + [(asset, PACK_VERSION, (version,)) for asset in subjects if version]


def item_digests(triples: Iterable[Triple]) -> dict[str, str]:
    """Asset id to its served-definition digest, for the delta receipts."""
    return {
        s: o[0] for s, p, o in triples if p == f"{CP}digest" and isinstance(o, tuple)
    }


def _chunks(lines: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(lines), size):
        yield lines[start : start + size]


def batch_max_triples() -> int:
    """The batch bound: the engine value ``catalog.onboard_batch_max_triples``."""
    from arbora.core.settings import setting

    return int(setting("ARBORA_CATALOG_ONBOARD_BATCH_MAX_TRIPLES", 50000))


def _term_from_row(term: Any) -> str | tuple[str]:
    value = str(term["value"] if isinstance(term, Mapping) else term)
    literal = isinstance(term, Mapping) and term.get("kind") == "literal"
    return (value,) if literal else value


async def _select(client: Any, graph: str, query: str) -> list[list[Any]]:
    from medulla.generated.reasoning import send_sparql

    result = await send_sparql(client, {"query": query}, graph)
    payload = getattr(result, "payload", result)
    rows = payload.get("rows", []) if isinstance(payload, Mapping) else []
    return [list(row) for row in rows]


def _departed_query(subjects: list[str]) -> str:
    values = " ".join(f"<{s}>" for s in subjects)
    links = " UNION ".join(
        f"{{ ?n <{CAT}{link}> ?s . ?n ?p ?o }}" for link in ("assignedTo", "from", "to")
    )
    return (
        f"SELECT ?n ?p ?o WHERE {{ VALUES ?s {{ {values} }} "
        f"{{ BIND(?s AS ?n) ?n ?p ?o }} UNION {links} }}"
    )


async def departed_triples(
    client: Any, graph: str, subjects: list[str]
) -> list[Triple]:
    """Every stored triple of nodes that left the pack, and of their attachments."""
    out: list[Triple] = []
    for start in range(0, len(subjects), 100):
        rows = await _select(
            client, graph, _departed_query(subjects[start : start + 100])
        )
        out += [(_value_iri(n), _value_iri(p), _term_from_row(o)) for n, p, o in rows]
    return out


def _value_iri(term: Any) -> str:
    return str(term["value"] if isinstance(term, Mapping) else term)


async def _prior_state(
    client: Any, graph: str, tenant: str
) -> tuple[Any, dict[str, str]]:
    from arbora.fleet.catalog_replay import read_receipts, replay_deltas

    receipts = await read_receipts(client, graph, tenant)
    state: dict[str, str] = {}
    replay_deltas(receipts, state=state, applied=set())
    return receipts, state


async def write_catalog(
    client: Any,
    graph: str,
    connector: str,
    pack: Any,
    *,
    tenant: str = "",
    importer: str = "",
    version: str | None = None,
) -> int:
    """Write ``pack``'s catalog into ``graph``; return the triple count.

    One pass sends ``RemoveTriples`` for nodes that left the pack, then the
    ``AddTriples`` batches (split above the batch bound), the last of which
    carries one delta receipt per change (ARBORA-FLEET-R022).
    """
    base = catalog_triples(connector, pack)
    triples = base + enrichment_triples(
        connector, pack, base, importer=importer, version=version
    )
    await _write_pass(client, graph, connector, triples, tenant)
    return len(triples)


async def _write_pass(
    client: Any, graph: str, connector: str, triples: list[Triple], tenant: str
) -> None:
    from medulla.generated.graph import send_add_triples, send_remove_triples

    from arbora.fleet.catalog_replay import next_position, plan_deltas

    current = item_digests(triples)
    receipts, state = await _prior_state(client, graph, tenant)
    prefix = f"urn:arbora:asset:{quote(connector, safe='')}:"
    prior = {k: v for k, v in state.items() if k.startswith(prefix)}
    deltas = plan_deltas(tenant, prior, current, next_position(receipts))
    departed = sorted(item for item in prior if item not in current)
    if departed:
        gone = await departed_triples(client, graph, departed)
        await send_remove_triples(client, {"ntriples": to_ntriples(gone)}, graph)
    receipt_triples = [t for delta in deltas for t in delta.triples()]
    lines = to_ntriples(triples + receipt_triples).splitlines()
    for chunk in _chunks(lines, batch_max_triples()):
        await send_add_triples(client, {"ntriples": "\n".join(chunk) + "\n"}, graph)
