"""The SPARQL 1.1 Protocol and Graph Store HTTP Protocol endpoints (ARBORA-OPS-R857.1).

``/sparql`` serves the W3C SPARQL 1.1 Protocol for query and update and
``/sparql/graphs`` serves the Graph Store HTTP Protocol for the caller's graph.
The routes carry no authority logic of their own: every request authenticates
through the shared HTTP authenticator and then drives a registered operation
through the single invocation chokepoint (ARBORA-OPS-R007), exactly as a direct
operation call would. A query runs ``query.sparql`` (engine ``Sparql``); an
update and every graph-store write run ``eg.graph.ApplyMutation`` with the
engine's SPARQL update event type (``MEDULLA-FEDERATED-QUERY-R707``). Admission
and scope therefore come from those operations: a caller without the write
scope is refused HTTP 403 before any engine call.

Results are negotiated from the ``Accept`` header: SPARQL results JSON, XML,
CSV or TSV for a query, N-Triples for a graph read. An unsupported type is
HTTP 406 and makes no engine call.
"""

from __future__ import annotations

import csv
import io
import json
import re
from secrets import token_hex
from typing import Any
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from arbora.api.errors import ArboraErrorCode, ArboraRefusal, to_envelope

from .auth import HTTPAuthenticationError
from .routes import MAX_BODY_BYTES, _surface_for, caller_session_scope

QUERY_OP = "query.sparql"
UPDATE_OP = "eg.graph.ApplyMutation"
#: The engine event type that applies a SPARQL 1.1 Update request as one batch.
SPARQL_UPDATE_EVENT = "sparql_http_update_v1"

JSON_RESULTS = "application/sparql-results+json"
XML_RESULTS = "application/sparql-results+xml"
CSV_RESULTS = "text/csv"
TSV_RESULTS = "text/tab-separated-values"
N_TRIPLES = "application/n-triples"

#: Accept media type -> served result type, in server preference order.
RESULT_TYPES: dict[str, str] = {
    JSON_RESULTS: JSON_RESULTS,
    "application/json": JSON_RESULTS,
    XML_RESULTS: XML_RESULTS,
    "application/xml": XML_RESULTS,
    CSV_RESULTS: CSV_RESULTS,
    TSV_RESULTS: TSV_RESULTS,
}
GRAPH_TYPES: dict[str, str] = {N_TRIPLES: N_TRIPLES, "text/plain": N_TRIPLES}

_GRAPH_READ = "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"
_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s<>\"]*$")
_ASK = re.compile(
    r"^\s*(?:(?:PREFIX\s+\S*\s*<[^>]*>|BASE\s*<[^>]*>)\s*)*ASK\b", re.IGNORECASE
)


class _BadRequest(Exception):
    """A malformed protocol request: HTTP 400, no engine call."""


class _NotAcceptable(Exception):
    """No served media type satisfies ``Accept``: HTTP 406, no engine call."""


def negotiate(accept: str | None, served: dict[str, str]) -> str:
    """Pick the served media type for ``accept`` (RFC 9110 q-values honoured)."""

    default = next(iter(served.values()))
    if not accept or not accept.strip():
        return default
    ranked: list[tuple[float, int, str]] = []
    for index, part in enumerate(accept.split(",")):
        fields = [field.strip() for field in part.split(";")]
        media = fields[0].lower()
        quality = 1.0
        for field in fields[1:]:
            if field.lower().startswith("q="):
                try:
                    quality = float(field[2:])
                except ValueError:
                    quality = 0.0
        if quality > 0 and media:
            ranked.append((-quality, index, media))
    for _quality, _index, media in sorted(ranked):
        if media in served:
            return served[media]
        if media in ("*/*", "application/*", "text/*"):
            prefix = media.split("/", 1)[0]
            for candidate in served.values():
                if prefix == "*" or candidate.startswith(prefix + "/"):
                    return candidate
    raise _NotAcceptable


def _term(value: str | None) -> dict[str, str] | None:
    """One SPARQL results JSON term for an engine table cell."""

    if value is None:
        return None
    if value.startswith("_:"):
        return {"type": "bnode", "value": value[2:]}
    if value.startswith("<") and value.endswith(">"):
        return {"type": "uri", "value": value[1:-1]}
    if _IRI.match(value):
        return {"type": "uri", "value": value}
    return {"type": "literal", "value": value}


def render_results(table: dict[str, Any], media: str, *, ask: bool) -> str:
    """Serialize the engine's solution table in the negotiated results format."""

    variables = [str(name) for name in table.get("vars") or []]
    rows = [list(row) for row in table.get("rows") or []]
    if ask:
        flag = bool(rows and rows[0] and str(rows[0][0]).lower() == "true")
        if media == JSON_RESULTS:
            return json.dumps({"head": {}, "boolean": flag})
        if media == XML_RESULTS:
            return (
                '<?xml version="1.0"?>\n<sparql xmlns="http://www.w3.org/2005/'
                f'sparql-results#"><head/><boolean>{str(flag).lower()}</boolean>'
                "</sparql>\n"
            )
        return f"_askResult\r\n{str(flag).lower()}\r\n"
    if media == JSON_RESULTS:
        bindings = [
            {
                name: term
                for name, cell in zip(variables, row, strict=False)
                if (term := _term(cell)) is not None
            }
            for row in rows
        ]
        return json.dumps(
            {"head": {"vars": variables}, "results": {"bindings": bindings}}
        )
    if media == XML_RESULTS:
        return _xml_results(variables, rows)
    if media == CSV_RESULTS:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(variables)
        for row in rows:
            writer.writerow([(_term(cell) or {"value": ""})["value"] for cell in row])
        return out.getvalue()
    lines = ["\t".join("?" + name for name in variables)]
    for row in rows:
        lines.append("\t".join(_tsv_cell(cell) for cell in row))
    return "\n".join(lines) + "\n"


def _tsv_cell(cell: str | None) -> str:
    term = _term(cell)
    if term is None:
        return ""
    if term["type"] == "uri":
        return f"<{term['value']}>"
    if term["type"] == "bnode":
        return f"_:{term['value']}"
    if term["value"].startswith('"'):
        return term["value"]
    return json.dumps(term["value"], ensure_ascii=False)


def _xml_results(variables: list[str], rows: list[list[str | None]]) -> str:
    head = "".join(f'<variable name="{escape(name)}"/>' for name in variables)
    results: list[str] = []
    for row in rows:
        cells: list[str] = []
        for name, cell in zip(variables, row, strict=False):
            term = _term(cell)
            if term is None:
                continue
            tag = {"uri": "uri", "bnode": "bnode"}.get(term["type"], "literal")
            cells.append(
                f'<binding name="{escape(name)}"><{tag}>{escape(term["value"])}'
                f"</{tag}></binding>"
            )
        results.append("<result>" + "".join(cells) + "</result>")
    return (
        '<?xml version="1.0"?>\n<sparql xmlns="http://www.w3.org/2005/'
        f'sparql-results#"><head>{head}</head><results>{"".join(results)}'
        "</results></sparql>\n"
    )


def render_ntriples(table: dict[str, Any]) -> str:
    """N-Triples for a ``CONSTRUCT`` table whose cells are N-Triples terms."""

    lines: list[str] = []
    for row in table.get("rows") or []:
        if len(row) != 3 or any(cell is None for cell in row):
            continue
        subject, predicate, obj = (str(cell) for cell in row)
        lines.append(f"{_node(subject)} {_node(predicate)} {_object(obj)} .")
    return "\n".join(lines) + ("\n" if lines else "")


def _node(value: str) -> str:
    if value.startswith(("<", "_:")):
        return value
    return f"<{value}>"


def _object(value: str) -> str:
    if value.startswith(("<", "_:", '"')):
        return value
    if _IRI.match(value):
        return f"<{value}>"
    return json.dumps(value, ensure_ascii=False)


async def _body(request: Request) -> str:
    length = request.headers.get("content-length")
    if length is not None and int(length) > MAX_BODY_BYTES:
        raise _BadRequest("request body too large")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY_BYTES:
            raise _BadRequest("request body too large")
        raw.extend(chunk)
    return raw.decode("utf-8")


def _content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def protocol_request(request: Request) -> tuple[str, str]:
    """``("query" | "update", text)`` from one SPARQL 1.1 Protocol request."""

    if request.method == "GET":
        query = request.query_params.get("query")
        if not query:
            raise _BadRequest("missing query")
        return "query", query
    media = _content_type(request)
    if media == "application/sparql-query":
        return "query", await _body(request)
    if media == "application/sparql-update":
        return "update", await _body(request)
    if media == "application/x-www-form-urlencoded":
        form = parse_qs(await _body(request), keep_blank_values=True)
        if form.get("update"):
            return "update", form["update"][0]
        if form.get("query"):
            return "query", form["query"][0]
        raise _BadRequest("missing query or update")
    raise _BadRequest("unsupported content type")


def _refusal(outcome: Any, *, op: str, digest: str, request_id: str) -> Response:
    status, envelope = to_envelope(
        outcome, op=op, request_id=request_id or token_hex(12), registry_digest=digest
    )
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(envelope, status_code=status, headers=headers)


def _code(code: str, *, op: str, digest: str, request_id: str = "") -> Response:
    return _refusal(
        ArboraRefusal(ArboraErrorCode(code)),
        op=op,
        digest=digest,
        request_id=request_id,
    )


def _admitted(outcome: Any) -> bool:
    return not hasattr(outcome, "source") and getattr(outcome, "code", "OK") == "OK"


def _table(outcome: Any) -> dict[str, Any]:
    value = getattr(outcome, "value", outcome)
    if isinstance(value, dict) and "value" in value and "vars" not in value:
        value = value["value"]
    if not isinstance(value, dict):
        raise ValueError("engine SPARQL answer has no solution table")
    return value


def mount_sparql_protocol(
    app: FastAPI,
    *,
    services: Any,
    authenticate: Any,
    invoke: Any,
    is_console_request: Any = None,
    max_result_bytes: int | None = None,
) -> None:
    """Add ``/sparql`` and ``/sparql/graphs`` to ``app``.

    ``invoke`` is the single invocation chokepoint; the routes never admit a
    caller themselves. ``max_result_bytes`` bounds one serialized answer
    (``sparql_max_result_bytes``); a larger answer is refused, never truncated.
    """

    if max_result_bytes is None:
        from arbora.core.settings import runtime_config

        max_result_bytes = runtime_config.sparql_max_result_bytes

    async def run(
        request: Request,
        op: str,
        params: dict[str, Any],
        *,
        write: bool = False,
    ) -> tuple[Any, Any] | Response:
        digest = services.registry.digest
        try:
            caller = await authenticate(request)
        except HTTPAuthenticationError:
            return _code("UNAUTHENTICATED", op=op, digest=digest)
        surface = _surface_for(request, caller, is_console_request)
        with caller_session_scope(caller):
            outcome = await invoke(
                op,
                params,
                caller,
                surface,
                services=services,
                plan_ref=request.headers.get("arbora-plan-ref"),
                # A protocol client sends no key: each write is its own request.
                idempotency_key=(
                    request.headers.get("idempotency-key") or token_hex(16)
                    if write
                    else None
                ),
            )
        if not _admitted(outcome):
            return _refusal(outcome, op=op, digest=digest, request_id=caller.request_id)
        return caller, outcome

    def answer(body: str, media: str, op: str) -> Response:
        data = body.encode("utf-8")
        if len(data) > max_result_bytes:
            return _code("LOAD_CAP_EXCEEDED", op=op, digest=services.registry.digest)
        return Response(
            data,
            media_type=media,
            headers={"Cache-Control": "no-store", "Vary": "Accept"},
        )

    async def update(request: Request, text: str) -> Response:
        ran = await run(
            request,
            UPDATE_OP,
            {"event_type": SPARQL_UPDATE_EVENT, "query": text},
            write=True,
        )
        if isinstance(ran, Response):
            return ran
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    async def sparql_endpoint(request: Request) -> Response:
        digest = services.registry.digest
        try:
            kind, text = await protocol_request(request)
        except (_BadRequest, UnicodeDecodeError, ValueError):
            return _code("INVALID_ARGUMENT", op=QUERY_OP, digest=digest)
        if kind == "update":
            return await update(request, text)
        try:
            media = negotiate(request.headers.get("accept"), RESULT_TYPES)
        except _NotAcceptable:
            return Response(status_code=406, headers={"Cache-Control": "no-store"})
        ran = await run(request, QUERY_OP, {"query": text})
        if isinstance(ran, Response):
            return ran
        try:
            table = _table(ran[1])
        except ValueError:
            return _code("UNAVAILABLE", op=QUERY_OP, digest=digest)
        return answer(
            render_results(table, media, ask=bool(_ASK.match(text))),
            media,
            QUERY_OP,
        )

    async def graph_store(request: Request) -> Response:
        digest = services.registry.digest
        params = request.query_params
        # The engine addresses one graph per caller session: the default graph.
        if "default" not in params or "graph" in params:
            return _code("INVALID_ARGUMENT", op=UPDATE_OP, digest=digest)
        if request.method == "GET":
            try:
                media = negotiate(request.headers.get("accept"), GRAPH_TYPES)
            except _NotAcceptable:
                return Response(status_code=406, headers={"Cache-Control": "no-store"})
            ran = await run(request, QUERY_OP, {"query": _GRAPH_READ})
            if isinstance(ran, Response):
                return ran
            try:
                table = _table(ran[1])
            except ValueError:
                return _code("UNAVAILABLE", op=QUERY_OP, digest=digest)
            return answer(render_ntriples(table), media, QUERY_OP)
        if request.method == "DELETE":
            return await update(request, "CLEAR DEFAULT")
        if _content_type(request) not in (N_TRIPLES, "text/plain"):
            return Response(status_code=415, headers={"Cache-Control": "no-store"})
        try:
            triples = await _body(request)
        except (_BadRequest, UnicodeDecodeError):
            return _code("INVALID_ARGUMENT", op=UPDATE_OP, digest=digest)
        insert = f"INSERT DATA {{\n{triples}\n}}"
        text = f"CLEAR DEFAULT; {insert}" if request.method == "PUT" else insert
        return await update(request, text)

    sparql_endpoint.__name__ = "http_sparql_protocol"
    graph_store.__name__ = "http_sparql_graph_store"
    app.add_api_route(
        "/sparql",
        sparql_endpoint,
        methods=["GET", "POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/sparql/graphs",
        graph_store,
        methods=["GET", "PUT", "POST", "DELETE"],
        include_in_schema=False,
    )


__all__ = [
    "QUERY_OP",
    "SPARQL_UPDATE_EVENT",
    "UPDATE_OP",
    "mount_sparql_protocol",
    "negotiate",
    "render_ntriples",
    "render_results",
]
