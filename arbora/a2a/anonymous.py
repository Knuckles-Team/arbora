"""Explicit anonymous task reads; never an authenticated session or fallback.

Only the configured shared-local namespace is available. Native tokenless client
provisioning remains the host's responsibility; this module creates no client,
credentials, verified claims or deployment settings. Mutation is not supported.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import ipaddress
import json
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .authority import WorkItemA2AAuthority
from .service import A2AService


@dataclass(frozen=True, slots=True)
class AnonymousReadPolicy:
    origin: str
    tenant: str
    revision: str
    operations: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
            port = parsed.port
        except ValueError:
            local, port = False, None
        if (
            not local
            or parsed.scheme != "http"
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or self.origin != f"http://{parsed.netloc}"
            or type(self.operations) is not frozenset
            or not self.operations <= {"tasks/get"}
        ):
            raise ValueError("Explicit loopback read policy required")
        if self.tenant != "local":
            raise ValueError("Anonymous namespace is local only")
        for value in (self.tenant, self.revision):
            if (
                type(value) is not str
                or not value
                or len(value) > 128
                or not value.isascii()
                or not all(c.isalnum() or c in "-_" for c in value)
            ):
                raise ValueError("Explicit namespace and revision required")


@dataclass(frozen=True, slots=True)
class AnonymousCaller:
    """Descriptive shared namespace, not a person, scope grant or GraphSession."""

    tenant: str
    policy_revision: str
    task_id: str
    owner_ref: str

    @property
    def authenticated(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class AnonymousTaskStatus:
    """Required native ownership evidence, not the existing EG caller view.

    The current EG WorkItem view omits tenant and native ownership evidence.
    An adapter must not fill those fields from its request to claim support.
    """

    work_item_id: str
    run_id: str
    tenant: str
    owner_ref: str
    status: str
    authority_mode: str
    context_id: str

    def __post_init__(self) -> None:
        for value in (
            self.work_item_id,
            self.run_id,
            self.tenant,
            self.owner_ref,
            self.status,
            self.authority_mode,
            self.context_id,
        ):
            if type(value) is not str or not value or len(value) > 256:
                raise ValueError("Invalid native anonymous evidence")
        WorkItemA2AAuthority._work_item_id(self.run_id)
        from .authority import _STATE_MAP
        from .models import A2ATaskStatus

        if self.status not in _STATE_MAP or self.authority_mode not in {
            "none",
            "verified",
        }:
            raise ValueError("Invalid native status")
        from .models import A2ATask

        A2ATask(
            id=self.run_id,
            context_id=self.context_id,
            status=A2ATaskStatus(state=_STATE_MAP[self.status]),
        )


class AnonymousNativeReadPort(ABC):
    """Tokenless native contract; generic signed EG clients do not implement it.

    The producer must enforce the bound namespace and anonymous owner before
    reading. No released implementation is qualified by this source contract.
    """

    @abstractmethod
    async def get(self, caller: AnonymousCaller) -> AnonymousTaskStatus | None:
        raise NotImplementedError


@dataclass(slots=True)
class _ReadBinding:
    caller: AnonymousCaller
    task: Any
    request: Request
    snapshot: tuple[Any, ...]
    used: bool = False


class AnonymousTaskReadOwner:
    """Own one exact public WorkItem reader and a revocable local read policy."""

    def __init__(
        self, *, mode: str, policy: AnonymousReadPolicy, work_items: Any
    ) -> None:
        if mode != "none" or type(policy) is not AnonymousReadPolicy:
            raise ValueError("Explicit anonymous mode required")
        if not isinstance(
            work_items, AnonymousNativeReadPort
        ) or not inspect.iscoroutinefunction(work_items.get):
            raise ValueError("Native WorkItem read port required")
        self._policy = policy
        self._work_items = work_items
        self._revoked = False
        self._active = 0
        self._request: contextvars.ContextVar[Any] = contextvars.ContextVar(
            "anonymous_request", default=None
        )
        self._binding: contextvars.ContextVar[_ReadBinding | None] = (
            contextvars.ContextVar("anonymous_task_read", default=None)
        )

    @property
    def policy(self) -> AnonymousReadPolicy:
        return self._policy

    def revoke(self) -> None:
        self._revoked = True

    def _deadline(self) -> float:
        binding = self._request.get()
        if binding is None:
            raise PermissionError("Anonymous request binding required")
        task = asyncio.current_task()
        if task is None:
            raise PermissionError("Anonymous request task required")
        if task.cancelling():
            raise asyncio.CancelledError
        if asyncio.get_running_loop().time() >= binding[3]:
            raise TimeoutError("Anonymous request expired")
        return binding[3]

    def _check(self, binding: _ReadBinding) -> None:
        self._deadline()
        if (
            self._revoked
            or self._binding.get() is not binding
            or asyncio.current_task() is not binding.task
            or self._snapshot(binding.request) != binding.snapshot
            or self._request.get()[:3]
            != (binding.request, binding.task, binding.snapshot)
        ):
            raise PermissionError("Anonymous read unavailable")

    @staticmethod
    def _snapshot(request: Request) -> tuple[Any, ...]:
        scope = request.scope
        return (
            (id(scope),)
            + tuple(
                scope.get(key)
                for key in (
                    "type",
                    "scheme",
                    "method",
                    "path",
                    "root_path",
                    "query_string",
                    "client",
                    "server",
                )
            )
            + (tuple(scope.get("headers", ())),)
        )

    @contextmanager
    def request_scope(self, request: Request):
        self._validate_request(request)
        if self._revoked or self._active >= 4 or self._request.get() is not None:
            raise PermissionError("Anonymous read capacity unavailable")
        snapshot = self._snapshot(request)
        self._active += 1
        token = self._request.set(
            (
                request,
                asyncio.current_task(),
                snapshot,
                asyncio.get_running_loop().time() + 5,
            )
        )
        try:
            yield snapshot
        finally:
            self._request.reset(token)
            self._active -= 1

    def _validate_request(self, request: Request) -> None:
        self._validate_ingress(request, self.policy.origin)

    @staticmethod
    def _validate_ingress(request: Request, origin: str) -> None:
        scope = request.scope
        try:
            local = ipaddress.ip_address(scope["client"][0]).is_loopback
        except (KeyError, TypeError, ValueError):
            local = False
        headers = request.headers
        forbidden = {
            "authorization",
            "cookie",
            "forwarded",
            "x-forwarded-host",
            "x-forwarded-for",
            "x-forwarded-proto",
        }
        expected = urlsplit(origin)
        if (
            not local
            or scope.get("scheme") != expected.scheme
            or any(name in headers for name in forbidden)
            or headers.getlist("host") != [expected.netloc]
            or headers.getlist("origin") != [origin]
            or headers.getlist("x-arbora-anonymous") != ["1"]
            or headers.getlist("content-type") != ["application/json"]
        ):
            raise PermissionError("Anonymous ingress refused")

    @contextmanager
    def admission(self, request: Request, task_id: str):
        self._validate_request(request)
        expected = self._request.get()
        if expected is None or expected[:3] != (
            request,
            asyncio.current_task(),
            self._snapshot(request),
        ):
            raise PermissionError("Anonymous request binding required")
        WorkItemA2AAuthority._work_item_id(task_id)
        if (
            self._revoked
            or "tasks/get" not in self.policy.operations
            or self._binding.get() is not None
        ):
            raise PermissionError("Anonymous read unavailable")
        owner_ref = (
            "anonymous:sha256:"
            + hashlib.sha256(self.policy.tenant.encode()).hexdigest()
        )
        binding = _ReadBinding(
            AnonymousCaller(
                self.policy.tenant, self.policy.revision, task_id, owner_ref
            ),
            asyncio.current_task(),
            request,
            self._snapshot(request),
        )
        token = self._binding.set(binding)
        try:
            yield binding.caller
        finally:
            self._binding.reset(token)

    async def dispatch(
        self,
        *,
        message: Any,
        idempotency_key: str,
        decision: Any,
        blocking: bool = True,
    ):
        raise PermissionError("Anonymous mutation is unavailable")

    async def list(self, *, cursor: str | None, limit: int):
        raise PermissionError("Anonymous listing is unavailable")

    async def cancel(self, task_id: str):
        raise PermissionError("Anonymous cancellation is unavailable")

    async def get(self, task_id: str):
        binding = self._binding.get()
        if binding is None:
            raise PermissionError("Anonymous admission required")
        self._check(binding)
        if binding.used or task_id != binding.caller.task_id:
            raise PermissionError("Anonymous read binding changed")
        binding.used = True
        from arbora.api.contracts.session import suspend_session
        from arbora.security.actor_context import suspend_actor

        # Stay on the admitting task. Cancellation holds capacity until the
        # provider has actually unwound; no detached thread retains authority.
        with suspend_session(), suspend_actor():
            async with asyncio.timeout_at(self._deadline()) as timeout:
                try:
                    item = await self._work_items.get(binding.caller)
                except ValueError as exc:
                    raise RuntimeError("Native read evidence unavailable") from exc
            if timeout.expired():
                raise TimeoutError("Anonymous request expired")
        self._check(binding)
        caller = binding.caller
        if (
            type(item) is not AnonymousTaskStatus
            or item.run_id != task_id
            or item.tenant != caller.tenant
            or item.owner_ref != caller.owner_ref
            or item.authority_mode != "none"
        ):
            return None
        from .authority import _STATE_MAP
        from .models import A2ATask, A2ATaskStatus

        state = _STATE_MAP.get(item.status)
        if state is None:
            raise ValueError("Unknown task state")
        # Status only: no reply cache, routing metadata or context ownership
        # has been qualified by this bounded native read contract.
        return A2ATask(
            id=task_id, context_id=item.context_id, status=A2ATaskStatus(state=state)
        )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


async def _read_payload(
    request: Request, owner: AnonymousTaskReadOwner, service: A2AService
) -> JSONResponse:
    from .application import _error, _GetTaskParams, _unary_response

    request_id = None
    try:
        owner._validate_request(request)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 16384:
                return _error(None, -32600, "Request too large", 413)
            raw.extend(chunk)
        body = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            type(body) is not dict
            or set(body) != {"jsonrpc", "id", "method", "params"}
            or body["jsonrpc"] != "2.0"
        ):
            raise ValueError("Invalid envelope")
        request_id = body["id"]
        if type(request_id) not in {str, int} or len(str(request_id)) > 128:
            request_id = None
            raise ValueError("Invalid identifier")
        if body["method"] != "tasks/get":
            raise PermissionError("Operation unavailable")
        params = _GetTaskParams.model_validate(body["params"])
        if params.include_committed_event:
            raise PermissionError("Committed reply authority unavailable")
        with owner.admission(request, params.id):
            result = await service.get_task(params.id)
        return (
            _unary_response(result, request_id)
            if result is not None
            else _error(request_id, -32001, "Task not found", 404)
        )
    except PermissionError:
        return _error(request_id, -32001, "Anonymous read refused", 403)
    except (ValueError, TypeError):
        return _error(request_id, -32602, "Invalid params")
    except Exception:
        return _error(request_id, -32014, "Task read unavailable", 503)


def create_anonymous_a2a_application(
    *, owner: AnonymousTaskReadOwner, service: A2AService
) -> FastAPI:
    """Create an opt-in read-only served seam; never modify the verified app."""
    if (
        type(owner) is not AnonymousTaskReadOwner
        or type(service) is not A2AService
        or service.authority is not owner
    ):
        raise ValueError("The exact same anonymous task owner is required")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.post("/a2a")
    async def read_task(request: Request):
        from .application import _error

        try:
            with owner.request_scope(request) as snapshot:
                async with asyncio.timeout_at(owner._deadline()) as timeout:
                    result = await _read_payload(request, owner, service)
                if timeout.expired():
                    raise TimeoutError("Anonymous request expired")
                owner._deadline()
                if owner._snapshot(request) != snapshot or owner._revoked:
                    raise PermissionError("Anonymous request changed")
                return result
        except PermissionError:
            return _error(None, -32001, "Anonymous read refused", 403)
        except TimeoutError:
            return _error(None, -32014, "Task read unavailable", 503)

    return app
