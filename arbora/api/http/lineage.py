"""OpenLineage-compatible run-event route (ARBORA-OPS-R889).

``POST /lineage`` accepts one OpenLineage run event or an array of them and
forwards them to the engine ``LineageIngest`` method through the one invoke
chokepoint, under the caller's tenant and the ``ingest:write`` scope. Arbora
maps the wire shape to ``LineageEvent`` and stores nothing and decides nothing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from secrets import token_hex
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from arbora.api.errors import ArboraErrorCode, ArboraRefusal, to_envelope

from .auth import HTTPAuthenticationError
from .routes import MAX_BODY_BYTES, _surface_for, caller_session_scope

LINEAGE_OP = "eg.ingestion.LineageIngest"
#: One request carries at most this many events (the engine drain batch).
MAX_LINEAGE_EVENTS = 500
_ENDED = frozenset({"COMPLETE", "FAIL", "ABORT"})


class LineageWireError(ValueError):
    """The body is not an OpenLineage run event or an array of them."""


def _millis(value: Any) -> int:
    if not isinstance(value, str):
        raise LineageWireError("eventTime")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LineageWireError("eventTime") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def _dataset(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise LineageWireError("dataset")
    name, namespace = raw.get("name"), raw.get("namespace")
    if not isinstance(name, str) or not isinstance(namespace, str):
        raise LineageWireError("dataset")
    return {"name": name, "namespace": namespace}


def lineage_event(raw: Any) -> dict[str, Any]:
    """One OpenLineage run event as the engine's ``LineageEvent`` record."""
    if not isinstance(raw, dict):
        raise LineageWireError("event")
    run, job = raw.get("run"), raw.get("job")
    if not isinstance(run, dict) or not isinstance(job, dict):
        raise LineageWireError("run")
    run_id, job_name = run.get("runId"), job.get("name")
    if not isinstance(run_id, str) or not isinstance(job_name, str):
        raise LineageWireError("run")
    at = _millis(raw.get("eventTime"))
    namespace = job.get("namespace")
    etl_run: dict[str, Any] = {
        "run_id": run_id,
        "job": f"{namespace}/{job_name}" if isinstance(namespace, str) else job_name,
        "started_at_ms": at,
    }
    if raw.get("eventType") in _ENDED:
        etl_run["ended_at_ms"] = at
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "event_id": f"openlineage:{digest}",
        "run": etl_run,
        "inputs": [_dataset(item) for item in raw.get("inputs") or []],
        "outputs": [_dataset(item) for item in raw.get("outputs") or []],
    }


def lineage_events(body: Any) -> list[dict[str, Any]]:
    """The mapped events of one request body; a non-array, non-object body refuses."""
    if isinstance(body, dict):
        body = [body]
    if not isinstance(body, list) or not body:
        raise LineageWireError("body")
    if len(body) > MAX_LINEAGE_EVENTS:
        raise OverflowError(len(body))
    return [lineage_event(item) for item in body]


def _refusal(outcome: Any, *, digest: str, request_id: str) -> JSONResponse:
    status, envelope = to_envelope(
        outcome,
        op=LINEAGE_OP,
        request_id=request_id or token_hex(12),
        registry_digest=digest,
    )
    return JSONResponse(
        envelope, status_code=status, headers={"Cache-Control": "no-store"}
    )


def mount_lineage_route(
    app: FastAPI,
    *,
    services: Any,
    authenticate: Any,
    invoke: Any,
    is_console_request: Any = None,
) -> None:
    """Add ``POST /lineage`` to ``app``; ``invoke`` is the one chokepoint."""

    async def lineage(request: Request) -> JSONResponse:
        digest = services.registry.digest

        def refuse(code: str, status: int | None = None) -> JSONResponse:
            response = _refusal(
                ArboraRefusal(ArboraErrorCode(code)), digest=digest, request_id=""
            )
            if status is not None:
                response.status_code = status
            return response

        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return refuse("INVALID_ARGUMENT", 413)
        try:
            events = lineage_events(json.loads(raw))
        except OverflowError:
            return refuse("INVALID_ARGUMENT", 413)
        except (LineageWireError, ValueError):
            return refuse("INVALID_ARGUMENT", 400)
        try:
            caller = await authenticate(request)
        except HTTPAuthenticationError:
            return refuse("UNAUTHENTICATED")
        key = request.headers.get("idempotency-key") or (
            "lineage:" + hashlib.sha256(raw).hexdigest()
        )
        with caller_session_scope(caller):
            outcome = await invoke(
                LINEAGE_OP,
                {"events": events},
                caller,
                _surface_for(request, caller, is_console_request),
                services=services,
                plan_ref=request.headers.get("arbora-plan-ref"),
                idempotency_key=key,
            )
        if hasattr(outcome, "source") or getattr(outcome, "code", "OK") != "OK":
            return _refusal(outcome, digest=digest, request_id=caller.request_id)
        value = getattr(outcome, "value", outcome)
        return JSONResponse(
            value.get("value", value) if isinstance(value, dict) else value,
            headers={"Cache-Control": "no-store"},
        )

    lineage.__name__ = "http_lineage_run_events"
    app.add_api_route("/lineage", lineage, methods=["POST"], include_in_schema=False)


__all__ = [
    "LINEAGE_OP",
    "MAX_LINEAGE_EVENTS",
    "LineageWireError",
    "lineage_event",
    "lineage_events",
    "mount_lineage_route",
]
