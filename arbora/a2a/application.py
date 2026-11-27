"""Authenticated Agent Card and JSON-RPC unary A2A transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import partial
from secrets import token_hex
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arbora.api.http.auth import HTTPAuthenticationError
from arbora.modality.models import ModalityRefused
from arbora.security.key_cache import JwksCache

from .authority import (
    A2AContextUnavailable,
    A2AIdempotencyConflict,
    A2AStreamingUnavailable,
    A2ATaskNotCancelable,
    A2ATransitionHistoryUnavailable,
)
from .confirm import UNQUALIFIED, ApprovalChain, ApprovalRefused, confirm_dispatch
from .critique_gate import release_output
from .event_log import A2AEventGap, A2AEventStream, wire_event
from .media_refs import MediaRefRefused, MediaStoreUnavailable
from .models import (
    TRANSITION_PAGE_LIMIT,
    A2AMessage,
    A2AOperationInvokeParams,
)
from .parts import FilePartRefused, MediaCapabilityRefused
from .push import PushRefused
from .routing import A2AAssemblyUnavailable, A2AOperationBridgeUnavailable
from .service import A2AService
from .terminal_event import A2ATerminalEventUnavailable
from .tool_action_admission import (
    REFUSAL_WIRE_CODE,
    ConfirmationKindInvalid,
    ConfirmRefusal,
)

if TYPE_CHECKING:
    from arbora.api.http.auth import AmbientHTTPAuthenticator
    from arbora.api.invoke.pipeline import InvokeServices
    from arbora.api.invoke.steps import RequestCaller

__all__ = [
    "A2AAuthenticator",
    "AmbientA2AAuthenticator",
    "create_a2a_application",
    "create_a2a_handlers",
]


@runtime_checkable
class A2AAuthenticator(Protocol):
    async def authenticate(
        self, request: Request, *, scope: str
    ) -> RequestCaller | None:
        """Verify authority; an injected adapter may own its dispatch binding."""


class AmbientA2AAuthenticator:
    """Resolve each HTTP request through AU's verified bearer/session flow."""

    def __init__(self, keys: JwksCache) -> None:
        self._keys = keys

    async def authenticate(self, request: Request, *, scope: str) -> RequestCaller:
        caller = await _request_caller(self._keys, request)
        return _require_caller_scope(caller, scope)


async def _request_caller(keys: JwksCache, request: Request) -> RequestCaller:
    from arbora.api.http.auth import (
        AmbientHTTPAuthenticator,
        verified_bearer_session_for_request,
    )

    return await AmbientHTTPAuthenticator(
        session_for_request=partial(verified_bearer_session_for_request, keys)
    ).authenticate(request)


def _require_caller_scope(caller: RequestCaller, scope: str) -> RequestCaller:
    if scope not in caller.effective_scopes:
        raise PermissionError("Required A2A scope unavailable")
    return caller


async def _refresh_unary_caller(caller: RequestCaller | None) -> None:
    """Refuse changed authority before entering the task service's session."""
    if caller is None:
        return
    from arbora.api.invoke.bootstrap import (
        SessionRequiredError,
        verify_current_from_session,
    )
    from arbora.api.invoke.steps import authenticate

    try:
        current = await verify_current_from_session(caller)
    except SessionRequiredError:
        raise HTTPAuthenticationError("Current A2A authority required") from None
    if current != caller or authenticate(current) is not None:
        raise HTTPAuthenticationError("Current A2A authority required")


@contextmanager
def caller_session_scope(caller: RequestCaller | None) -> Iterator[None]:
    """Bind a returned caller through legacy dispatch; never resolve a fallback."""
    if caller is None:
        # Explicit injected authenticators already own their admission/binding.
        yield
        return
    from arbora.security.actor_context import use_actor
    from arbora.security.session import use_session

    with use_actor(caller.session.actor), use_session(caller.session):
        yield


def _authentication_error(request_id: Any, error: PermissionError) -> JSONResponse:
    status = 401 if isinstance(error, HTTPAuthenticationError) else 403
    return _error(request_id, -32001, "A2A authority refused", status)


class _Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class _SendConfiguration(_Params):
    """The A2A ``MessageSendConfiguration`` fields arbora serves."""

    blocking: bool | None = None
    accepted_output_modes: list[str] | None = Field(
        default=None, alias="acceptedOutputModes"
    )
    history_length: int | None = Field(default=None, alias="historyLength", ge=0)


class _SendParams(_Params):
    message: A2AMessage
    configuration: _SendConfiguration | None = None
    context_budget_tokens: int | None = Field(
        default=None, alias="contextBudgetTokens", ge=256, le=1_000_000
    )


class _TaskParams(_Params):
    id: str = Field(min_length=1, max_length=80)


class _GetTaskParams(_TaskParams):
    include_committed_event: bool = Field(
        default=False, alias="includeCommittedEvent", strict=True
    )
    include_transition_history: bool = Field(
        default=False, alias="includeTransitionHistory", strict=True
    )
    history_cursor: str | None = Field(
        default=None, alias="historyCursor", min_length=1, max_length=512
    )
    history_limit: int = Field(
        default=TRANSITION_PAGE_LIMIT,
        alias="historyLimit",
        ge=1,
        le=TRANSITION_PAGE_LIMIT,
    )


class _ResubscribeParams(_Params):
    """``after_seq`` is the last ``event_seq`` the caller saw; 0 replays from the start."""

    id: str = Field(min_length=1, max_length=80)
    after_seq: int = Field(default=0, alias="afterSeq", ge=0, le=2**53)


class _PushSetParams(_Params):
    """``tasks/pushNotificationConfig/set``: one webhook for one of the caller's tasks."""

    id: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2048)
    token_ref: str | None = Field(
        default=None, alias="tokenRef", min_length=1, max_length=512
    )


class _ListParams(_Params):
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=50, ge=1, le=100)


def _error(
    request_id: Any,
    code: int,
    message: str,
    status: int = 400,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": error},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


async def _request_envelope(
    request: Request,
) -> tuple[Any, str, dict[str, Any]] | JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _error(None, -32700, "Parse error")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        request_id = body.get("id") if isinstance(body, dict) else None
        return _error(request_id, -32600, "Invalid Request")
    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return _error(request_id, -32602, "Invalid params")
    return request_id, method, params


async def _send_message(
    service: A2AService, request: Request, raw_params: Any, *, stream: bool = False
) -> Any:
    send_params = _SendParams.model_validate(raw_params)
    key = request.headers.get("Idempotency-Key", "")
    if not key:
        raise ValueError("Idempotency-Key is required")
    configuration = send_params.configuration
    # ARBORA-A2A-R001.6: ``blocking: false`` answers with the admitted task at
    # once; an absent ``blocking`` keeps the blocking behavior.
    blocking = not stream and (
        configuration is None or configuration.blocking is not False
    )
    return await service.send_message(
        message=send_params.message,
        idempotency_key=key,
        context_budget_tokens=send_params.context_budget_tokens,
        blocking=blocking,
    )


async def _get_task(service: A2AService, raw_params: Any, request_id: Any) -> Any:
    task_params = _GetTaskParams.model_validate(raw_params)
    result = await service.get_task(
        task_params.id,
        include_committed_event=task_params.include_committed_event,
        include_transition_history=task_params.include_transition_history,
        history_cursor=task_params.history_cursor,
        history_limit=task_params.history_limit,
    )
    return result or _error(request_id, -32001, "Task not found", 404)


_STREAM_UNAVAILABLE = {
    "message/stream": "message/stream is not available; use message/send",
    "tasks/resubscribe": "durable task streaming and resubscribe are not available yet",
}


def _sse_frame(payload: dict[str, Any], event_seq: int | None = None) -> bytes:
    prefix = "" if event_seq is None else f"id: {event_seq}\n"
    return f"{prefix}data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


async def _event_frames(
    first: Any,
    stream: A2AEventStream,
    caller: RequestCaller | None,
    request_id: Any,
) -> AsyncIterator[bytes]:
    """Replay the durable events after the cursor, then follow live ones.

    Each batch is read inside the caller's session scope; the scope is never
    held across a ``yield``.
    """

    def envelope(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    try:
        if first is not None:
            yield _sse_frame(envelope(first.model_dump(mode="json", by_alias=True)))
        while True:
            with caller_session_scope(caller):
                records = await stream.batch()
            for record in records:
                yield _sse_frame(envelope(wire_event(record)), int(record["event_seq"]))
            if stream.finished:
                return
            if not records:
                await stream.wait()
    except A2AEventGap as gap:
        yield _sse_frame(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32010, "message": str(gap)},
            }
        )
    finally:
        stream.close()


async def _stream_response(
    service: A2AService,
    request: Request,
    method: str,
    raw_params: Any,
    request_id: Any,
    caller: RequestCaller | None,
) -> Any:
    """``message/stream`` and ``tasks/resubscribe`` (ARBORA-A2A-R001.4.1).

    Both serve the durable events of one task from the event cursor. An absent
    task and another tenant's task answer the same ``-32001``.
    """
    if method == "message/stream":
        _SendParams.model_validate(raw_params)
    else:
        resubscribe = _ResubscribeParams.model_validate(raw_params)
    streaming = service.streaming
    if streaming is None:
        raise A2AStreamingUnavailable(_STREAM_UNAVAILABLE[method])
    first = None
    if method == "message/stream":
        first = await _send_message(service, request, raw_params, stream=True)
        task_id, after_seq = first.id, 0
    else:
        task_id, after_seq = resubscribe.id, resubscribe.after_seq
    stream = await streaming.resume_from_cursor(task_id, after_seq)
    if stream is None:
        return _error(request_id, -32001, "Task not found", 404)
    return StreamingResponse(
        _event_frames(first, stream, caller, request_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _op_invoke_stub(raw_params: Any) -> Any:
    # Registered in the method table (ARBORA-A2A-R006) but fail-closed:
    # the shared hosted-operation registry's invoke path is not bridged in
    # yet. Params are still validated so a malformed call is rejected as
    # invalid, not treated as an unreachable method.
    A2AOperationInvokeParams.model_validate(raw_params)
    raise A2AOperationBridgeUnavailable(
        "arbora.op/invoke is not bridged to the shared operation registry yet"
    )


_STREAM_METHODS = frozenset({"message/stream", "tasks/resubscribe"})

_SYNC_METHODS: dict[str, Callable[[Any], Any]] = {
    "arbora.op/invoke": _op_invoke_stub,
}


async def _invoke_method(
    service: A2AService,
    request: Request,
    method: str,
    raw_params: dict[str, Any],
    request_id: Any,
    *,
    caller: Any = None,
    approvals: ApprovalChain | None = None,
) -> Any:
    """Validate and invoke one unary method on the shared service."""
    if method == "arbora.plan/confirm":
        return await confirm_dispatch(raw_params, caller=caller, chain=approvals)
    if method == "message/send":
        sent = await _send_message(service, request, raw_params)
        # ARBORA-A2A-R022: the reply is held until the engine admits its exact output.
        return await release_output(
            sent, approvals, tenant=str(getattr(caller, "tenant", ""))
        )
    if method == "tasks/get":
        return await _get_task(service, raw_params, request_id)
    if method == "tasks/list":
        list_params = _ListParams.model_validate(raw_params)
        return await service.list_tasks(
            cursor=list_params.cursor, limit=list_params.limit
        )
    if method == "tasks/cancel":
        cancel_params = _TaskParams.model_validate(raw_params)
        return await service.cancel_task(cancel_params.id)
    if method == "tasks/pushNotificationConfig/set" and service.push is not None:
        push_params = _PushSetParams.model_validate(raw_params)
        try:
            target = await service.set_push_target(
                push_params.id, push_params.url, push_params.token_ref
            )
        except LookupError:
            return _error(request_id, -32001, "Task not found", 404)
        return {"id": target.task_id, "url": target.url, "tokenRef": target.token_ref}
    sync_handler = _SYNC_METHODS.get(method)
    if sync_handler is not None:
        return sync_handler(raw_params)
    return _error(request_id, -32601, "Method not found", 404)


_ERROR_CODES: dict[type[Exception], tuple[int, int]] = {
    A2AContextUnavailable: (-32014, 503),
    A2ATerminalEventUnavailable: (-32013, 503),
    A2AIdempotencyConflict: (-32009, 409),
    A2AAssemblyUnavailable: (-32003, 503),
    A2ATaskNotCancelable: (-32002, 409),
    A2AStreamingUnavailable: (-32010, 501),
    A2AEventGap: (-32010, 409),
    A2AOperationBridgeUnavailable: (-32011, 501),
    A2ATransitionHistoryUnavailable: (-32012, 501),
}


def _application_error(request_id: Any, error: Exception) -> JSONResponse:
    """Translate known service failures without echoing caller input."""
    from arbora.api.invoke.pipeline import OperationRefused

    if isinstance(error, OperationRefused) and error.code == "UPGRADE_REQUIRED":
        return _error(request_id, -32013, "UPGRADE_REQUIRED: native run ownership", 501)
    if isinstance(error, ModalityRefused):
        # ARBORA-OPS-R899.3: the typed modality refusal; its text names the
        # code and a fixed reason, never caller input.
        unavailable = error.arbora_code == "UNAVAILABLE"
        return _error(request_id, -32015, str(error), 503 if unavailable else 422)
    if isinstance(error, FilePartRefused):
        # ARBORA-A2A-R001.5: a closed reason, never caller input.
        return _error(
            request_id, -32602, "Invalid params", 400, {"reason": error.reason}
        )
    if isinstance(error, MediaRefRefused):
        # ARBORA-A2A-R001.5.5: a closed reason; a foreign reference reads as unknown.
        return _error(
            request_id, -32602, "Invalid params", 400, {"reason": error.reason}
        )
    if isinstance(error, MediaStoreUnavailable):
        return _error(request_id, -32015, "UNAVAILABLE", 503)
    if isinstance(error, MediaCapabilityRefused):
        # ARBORA-A2A-R001.5.2: the typed code and a closed reason.
        unavailable = error.code == "UNAVAILABLE"
        return _error(
            request_id,
            -32015 if unavailable else -32016,
            error.code,
            503 if unavailable else 403,
            {"reason": error.reason},
        )
    if isinstance(error, ConfirmRefusal):
        # R005.6, R006.6: the reason is a closed code and the consumer treats it as deny.
        return _error(
            request_id,
            REFUSAL_WIRE_CODE,
            "confirmation refused",
            403,
            {"reason": error.reason},
        )
    if isinstance(error, PushRefused):
        # ARBORA-A2A-R013.1: a closed code, never the URL.
        return _error(request_id, -32602, "Invalid params", 400, {"reason": error.code})
    if isinstance(error, ConfirmationKindInvalid):
        return _error(request_id, -32602, error.code, 400)
    if isinstance(error, ApprovalRefused):
        # ARBORA-A2A-R005.2.5: the closed refusal code, never caller input.
        unqualified = error.code == UNQUALIFIED
        return _error(
            request_id,
            -32011 if unqualified else -32017,
            error.code,
            501 if unqualified else 403,
        )
    mapped = _ERROR_CODES.get(type(error))
    if mapped is not None:
        code, status = mapped
        reason = getattr(error, "reason", None)
        data = (
            {"reason": reason}
            if isinstance(error, A2AAssemblyUnavailable) and reason
            else None
        )
        return _error(request_id, code, str(error), status, data)
    # Pydantic errors may echo caller text in ``input_value``. Keep the wire
    # error stable and privacy-safe; details belong in local logs.
    return _error(request_id, -32602, "Invalid params")


def _operation_response(
    outcome: Any, *, op: str, request_id: Any, caller_id: str, digest: str
) -> JSONResponse:
    """Project shared operation outcomes without reclassifying refusals."""
    from arbora.api.errors import a2a_error_status, to_envelope
    from arbora.api.invoke.steps import OpError

    status = 200
    if outcome.code == "OK":
        payload = {
            "ok": True,
            "result": jsonable_encoder(outcome.value),
            "meta": {"registry_digest": digest, "api_version": "v1"},
        }
    else:
        status, payload = to_envelope(
            outcome,
            op=op,
            request_id=caller_id or token_hex(12),
            registry_digest=digest,
        )
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if isinstance(outcome, OpError):
        code, status = a2a_error_status(outcome.code, source=outcome.source)
        body["error"] = {
            "code": code,
            "message": payload["error"]["message"],
            "data": payload,
        }
    else:
        body["result"] = payload
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


async def _invoke_operation(
    request: Request,
    raw_params: dict[str, Any],
    request_id: Any,
    services: InvokeServices,
    authenticator: AmbientHTTPAuthenticator,
) -> JSONResponse:
    from arbora.api.http.auth import HTTPAuthenticationError
    from arbora.api.invoke.pipeline import invoke
    from arbora.api.invoke.steps import OpError, OpResult
    from arbora.api.registry import Surface

    params = A2AOperationInvokeParams.model_validate(raw_params)
    caller_id = ""
    outcome: OpError | OpResult
    try:
        caller = await authenticator.authenticate(request)
    except HTTPAuthenticationError:
        outcome = OpError("UNAUTHENTICATED")
    else:
        # A plan reference cannot substitute for the missing signed A2A exchange.
        if params.plan_ref is not None:
            raise A2AOperationBridgeUnavailable(
                "A2A plan consumption requires the signed approval exchange"
            )
        caller_id = caller.request_id
        outcome = await invoke(
            params.op,
            params.params,
            caller,
            Surface.A2A,
            services=services,
            idempotency_key=params.idempotency_key,
        )
    return _operation_response(
        outcome,
        op=params.op,
        request_id=request_id,
        caller_id=caller_id,
        digest=services.registry.digest,
    )


def _validate_operation_composition(
    services: InvokeServices | None,
    authenticator: AmbientHTTPAuthenticator | None,
) -> None:
    if (services is None) != (authenticator is None):
        raise ValueError(
            "operation services and authenticator must be composed together"
        )


async def _operation_request(
    request: Request,
    raw_params: dict[str, Any],
    request_id: Any,
    services: InvokeServices,
    authenticator: AmbientHTTPAuthenticator,
) -> JSONResponse:
    try:
        return await _invoke_operation(
            request, raw_params, request_id, services, authenticator
        )
    except (A2AOperationBridgeUnavailable, ValidationError) as error:
        return _application_error(request_id, error)


async def _unary_request(
    service: A2AService,
    authenticator: A2AAuthenticator,
    request: Request,
    method: str,
    raw_params: dict[str, Any],
    request_id: Any,
    approvals: ApprovalChain | None = None,
) -> Any:
    write_methods = {
        "message/send",
        "message/stream",
        "tasks/cancel",
        "tasks/pushNotificationConfig/set",
        "arbora.plan/confirm",
    }
    scope = "kg:write" if method in write_methods else "kg:read"
    try:
        caller = await authenticator.authenticate(request, scope=scope)
        await _refresh_unary_caller(caller)
        with caller_session_scope(caller):
            if method in _STREAM_METHODS:
                result = await _stream_response(
                    service, request, method, raw_params, request_id, caller
                )
            else:
                result = await _invoke_method(
                    service,
                    request,
                    method,
                    raw_params,
                    request_id,
                    caller=caller,
                    approvals=approvals,
                )
    except PermissionError as error:
        return _authentication_error(request_id, error)
    except (
        ApprovalRefused,
        ConfirmRefusal,
        ConfirmationKindInvalid,
        A2AContextUnavailable,
        A2ATerminalEventUnavailable,
        A2AIdempotencyConflict,
        A2AAssemblyUnavailable,
        A2AOperationBridgeUnavailable,
        A2AStreamingUnavailable,
        A2AEventGap,
        A2ATransitionHistoryUnavailable,
        A2ATaskNotCancelable,
        ModalityRefused,
        MediaCapabilityRefused,
        MediaStoreUnavailable,
        ValidationError,
        TypeError,
        ValueError,
    ) as error:
        return _application_error(request_id, error)
    return _unary_response(result, request_id)


def _unary_response(result: Any, request_id: Any) -> Any:
    if isinstance(result, (JSONResponse, StreamingResponse)):
        return result
    # ``arbora.plan/confirm`` answers a plain mapping; the task methods answer models.
    payload = (
        dict(result)
        if isinstance(result, Mapping)
        else result.model_dump(mode="json", by_alias=True)
    )
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "result": payload},
        headers={"Cache-Control": "no-store"},
    )


async def _agent_card_request(
    service: A2AService, authenticator: A2AAuthenticator, request: Request
) -> JSONResponse:
    try:
        await authenticator.authenticate(request, scope="kg:read")
    except PermissionError as error:
        return _authentication_error(None, error)
    endpoint = str(request.url.replace(path="/a2a", query=""))
    return JSONResponse(
        service.agent_card(endpoint).model_dump(mode="json", by_alias=True),
        headers={"Cache-Control": "no-store"},
    )


def create_a2a_handlers(
    *,
    service: A2AService,
    authenticator: A2AAuthenticator,
    operation_services: InvokeServices | None = None,
    operation_authenticator: AmbientHTTPAuthenticator | None = None,
    approvals: ApprovalChain | None = None,
) -> tuple[Any, Any]:
    """Create route handlers reusable by standalone FastAPI and FastMCP."""
    if not isinstance(authenticator, A2AAuthenticator):
        raise TypeError("authenticator does not implement A2AAuthenticator")
    _validate_operation_composition(operation_services, operation_authenticator)

    async def agent_card(request: Request) -> JSONResponse:
        return await _agent_card_request(service, authenticator, request)

    async def json_rpc(request: Request) -> JSONResponse:
        envelope = await _request_envelope(request)
        if isinstance(envelope, JSONResponse):
            return envelope
        request_id, method, raw_params = envelope
        if method == "arbora.op/invoke" and operation_services is not None:
            assert operation_authenticator is not None
            return await _operation_request(
                request,
                raw_params,
                request_id,
                operation_services,
                operation_authenticator,
            )
        return await _unary_request(
            service, authenticator, request, method, raw_params, request_id, approvals
        )

    return agent_card, json_rpc


def create_a2a_application(
    *,
    service: A2AService,
    authenticator: A2AAuthenticator,
    operation_services: InvokeServices | None = None,
    operation_authenticator: AmbientHTTPAuthenticator | None = None,
    approvals: ApprovalChain | None = None,
) -> FastAPI:
    metadata = service.card_metadata
    app = FastAPI(title=f"{metadata.name} A2A", version=metadata.version)
    card_handler, rpc_handler = create_a2a_handlers(
        service=service,
        authenticator=authenticator,
        operation_services=operation_services,
        operation_authenticator=operation_authenticator,
        approvals=approvals,
    )
    app.add_api_route("/.well-known/agent-card.json", card_handler, methods=["GET"])
    app.add_api_route("/a2a", rpc_handler, methods=["POST"])
    return app
