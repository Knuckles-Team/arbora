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

logger = logging.getLogger(__name__)

KG = "http://knuckles.team/kg#"
CAT = "http://knuckles.team/kg/catalog#"
CP = "http://knuckles.team/kg/connector-pack#"
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


async def write_catalog(client: Any, graph: str, connector: str, pack: Any) -> int:
    """Write ``pack``'s catalog into ``graph`` with one ``AddTriples``; return the count."""
    from medulla.generated.graph import send_add_triples

    triples = catalog_triples(connector, pack)
    await send_add_triples(client, {"ntriples": to_ntriples(triples)}, graph)
    return len(triples)
