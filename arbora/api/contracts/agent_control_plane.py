"""The verified, application-level RLM operation retained by Arbora.

Arbora owns transport registration, routing, and its public action manifest. This
module owns the RLM behavior and requires the caller to bind it to an injected,
session-scoped epistemic-graph client before any operation can run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from arbora.api.contracts.agent_control_contracts import (
    AgentControlPlaneUnavailable,
    AgentExecutionPort,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentOperationDescriptor,
    AgentTaskDispatchRequest,
    AgentTaskDispatchResult,
    CapabilityCandidate,
    CapabilityResolution,
    CapabilitySearchPort,
    CapabilitySearchRequest,
    SignedAgentDispatchPort,
    SignedAgentDispatchReceipt,
    SignedAgentDispatchRequest,
    WorkItemCancelRequest,
    WorkItemGetRequest,
    WorkItemListRequest,
    WorkItemPage,
    WorkItemSnapshot,
    WorkItemStorePort,
    WorkItemSubmission,
    WorkItemSubmissionResult,
)
from arbora.api.contracts.session import GraphSession, resolve_session
from arbora.security.anonymous import client_scope, session_context
from arbora.security.error_surface import public_error_payload

type _RequiredScope = Literal["kg:read", "kg:write"]
type _OperationSpec = tuple[
    str,
    Any,
    Any,
    _RequiredScope,
    tuple[tuple[str, _RequiredScope], ...],
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GraphRlmPromptExample(_StrictModel):
    query: str = Field(min_length=1, max_length=100_000)
    response: str = Field(max_length=100_000)


class GraphRlmRunRequest(_StrictModel):
    action: Literal["run"] = "run"
    task: str = Field(min_length=1, max_length=10_000)
    input_text: str = Field(default="", max_length=10_000_000)


type GraphRlmRequest = GraphRlmRunRequest


class GraphRlmError(_StrictModel):
    """Sanitized, correlation-safe failure metadata; never includes exception text."""

    code: Literal[
        "operation_failed",
        "invalid_request",
        "permission_denied",
        "dependency_unavailable",
        "engine_degraded",
    ]
    correlation_id: str
    detail_ref: str | None = None
    retryable: bool = False
    error_class: str
    failing_layer: str


class GraphRlmRunResult(_StrictModel):
    action: Literal["run"]
    ok: bool
    task: str
    result: str | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    max_depth: int = 0
    failure_class: str | None = None
    error: GraphRlmError | None = None


type GraphRlmResult = GraphRlmRunResult

_GRAPH_RLM_SCOPES: dict[type[BaseModel], Literal["kg:read", "kg:write"]] = {
    GraphRlmRunRequest: "kg:read",
}


class AgentControlPlane:
    """AU application operations bound to verified graph identity and policy.

    Work-item, capability, execution, and signed-dispatch behavior is supplied
    through explicit ports. The control plane deliberately has no legacy graph
    engine adapter and never reconstructs those operations with raw queries.
    """

    def __init__(
        self,
        eg_client: Any,
        session: GraphSession,
        *,
        capability_search: CapabilitySearchPort | None = None,
        agent_executor: AgentExecutionPort | None = None,
        work_item_store: WorkItemStorePort | None = None,
        signed_dispatch: SignedAgentDispatchPort | None = None,
    ) -> None:
        if eg_client is None:
            raise ValueError("a session-routed epistemic-graph client is required")
        if not isinstance(session, GraphSession):
            raise TypeError("a verified GraphSession is required")
        use_verified_context = getattr(eg_client, "use_verified_context", None)
        if not callable(use_verified_context):
            raise TypeError("the epistemic-graph client must support verified context")

        self._eg_client = eg_client
        self._session = session
        self._capability_search = capability_search
        self._agent_executor = agent_executor
        self._work_item_store = work_item_store
        self._signed_dispatch = signed_dispatch
        # Validate eagerly so invalid or expired authority never creates a usable
        # control plane. It is checked again immediately before every call.
        session_context(session)

    @property
    def operation_descriptors(self) -> tuple[AgentOperationDescriptor, ...]:
        """Schemas for direct operations; these are metadata, not a dispatcher."""
        specs: tuple[_OperationSpec, ...] = (
            (
                "graph_rlm",
                GraphRlmRequest,
                GraphRlmResult,
                "kg:read",
                (),
            ),
            (
                "resolve_capability",
                CapabilitySearchRequest,
                CapabilityResolution,
                "kg:read",
                (),
            ),
            (
                "execute_agent",
                AgentExecutionRequest,
                AgentExecutionResult,
                "kg:write",
                (),
            ),
            (
                "submit_agent_task",
                AgentTaskDispatchRequest,
                AgentTaskDispatchResult,
                "kg:write",
                (),
            ),
            (
                "get_work_item",
                WorkItemGetRequest,
                WorkItemSnapshot | None,
                "kg:read",
                (),
            ),
            ("list_work_items", WorkItemListRequest, WorkItemPage, "kg:read", ()),
            (
                "cancel_work_item",
                WorkItemCancelRequest,
                WorkItemSnapshot | None,
                "kg:write",
                (),
            ),
        )
        return tuple(
            AgentOperationDescriptor(
                name=name,
                request_schema=TypeAdapter(request_type).json_schema(),
                result_schema=TypeAdapter(result_type).json_schema(),
                required_scope=scope,
                action_scopes=action_scopes,
            )
            for name, request_type, result_type, scope, action_scopes in specs
        )

    def _verified_session(
        self, required_scope: Literal["kg:read", "kg:write"] | None
    ) -> GraphSession:
        session = resolve_session(self._session, required_scope=required_scope)
        session_context(session)
        return session

    @contextmanager
    def _verified_client_context(self, session: GraphSession) -> Iterator[None]:
        claims = session_context(session)
        with client_scope(self._eg_client, claims):
            yield

    @staticmethod
    def _require_port(port: Any, name: str) -> Any:
        if port is None:
            raise AgentControlPlaneUnavailable(
                f"the {name} application port is not configured"
            )
        return port

    async def resolve_capability(
        self, request: CapabilitySearchRequest
    ) -> CapabilityResolution:
        """Return only a capability authorized by the injected EG search port."""
        if not isinstance(request, CapabilitySearchRequest):
            raise TypeError("request must be a validated CapabilitySearchRequest")
        session = self._verified_session("kg:read")
        port = self._require_port(self._capability_search, "capability-search")
        with self._verified_client_context(session):
            candidates = tuple(await port.search(request, session=session))
        if any(not isinstance(item, CapabilityCandidate) for item in candidates):
            raise AgentControlPlaneUnavailable(
                "the capability-search port returned an invalid candidate"
            )

        if request.agent_name is not None:
            candidates = tuple(
                item for item in candidates if item.name == request.agent_name
            )
            if not candidates:
                raise LookupError("the requested agent is not an authorized capability")

        ranked = sorted(
            candidates,
            key=lambda item: (-item.score, item.name.casefold(), item.component_id),
        )
        if not ranked:
            raise LookupError("no authorized capability matched the task")
        selected = ranked[0]
        return CapabilityResolution(
            kind=selected.kind,
            name=selected.name,
            component_id=selected.component_id,
            score=selected.score,
            source="caller" if request.agent_name is not None else "eg_search",
            alternatives=tuple(ranked[1:4]),
        )

    async def _prepare_agent_task(
        self, task: str, agent_name: str | None
    ) -> tuple[str, CapabilityResolution]:
        from arbora.access.task_guard import screen_and_redact_agent_task

        sanitized_task = screen_and_redact_agent_task(task)
        capability = await self.resolve_capability(
            CapabilitySearchRequest(task=sanitized_task, agent_name=agent_name)
        )
        return sanitized_task, capability

    async def execute_agent(
        self, request: AgentExecutionRequest
    ) -> AgentExecutionResult:
        """Delegate execution only to a composed AU execution implementation."""
        if not isinstance(request, AgentExecutionRequest):
            raise TypeError("request must be a validated AgentExecutionRequest")
        session = self._verified_session("kg:write")
        port = self._require_port(self._agent_executor, "agent-execution")
        sanitized_task, capability = await self._prepare_agent_task(
            request.task, request.agent_name
        )
        authorized_request = request.model_copy(
            update={"agent_name": capability.name, "task": sanitized_task}
        )
        with self._verified_client_context(session):
            result = await port.execute_agent(authorized_request, session=session)
        if not isinstance(result, AgentExecutionResult):
            raise AgentControlPlaneUnavailable(
                "the agent-execution port returned an invalid result"
            )
        return result

    async def submit_agent_task(
        self, request: AgentTaskDispatchRequest
    ) -> AgentTaskDispatchResult:
        """Screen, admit, and signed-enqueue one typed agent task."""
        if not isinstance(request, AgentTaskDispatchRequest):
            raise TypeError("request must be a validated AgentTaskDispatchRequest")
        session = self._verified_session("kg:write")
        store = self._require_port(self._work_item_store, "work-item-store")
        dispatch = self._require_port(self._signed_dispatch, "signed-dispatch")
        self._reject_authority_metadata(request.metadata)
        sanitized_task, capability = await self._prepare_agent_task(
            request.task, request.agent_name
        )

        submission = WorkItemSubmission(
            work_item_id=request.work_item_id,
            idempotency_key=request.idempotency_key,
            kind="orchestrator_task",
            description=sanitized_task,
            metadata=dict(request.metadata),
        )
        with self._verified_client_context(session):
            admission = await store.submit(submission, session=session)
        if not isinstance(admission, WorkItemSubmissionResult):
            raise AgentControlPlaneUnavailable(
                "the work-item store returned an invalid admission result"
            )
        if admission.item.work_item_id != request.work_item_id:
            raise AgentControlPlaneUnavailable(
                "the work-item store returned an unrelated admission"
            )

        receipt: SignedAgentDispatchReceipt | None = None
        if admission.item.status in {"submitted", "ready"}:
            dispatch_request = SignedAgentDispatchRequest(
                job_id=request.job_id,
                work_item_id=admission.item.work_item_id,
                session_ref=request.session_ref,
                agent_name=capability.name,
            )
            with self._verified_client_context(session):
                receipt = await dispatch.enqueue(dispatch_request, session=session)
            if (
                not isinstance(receipt, SignedAgentDispatchReceipt)
                or receipt.job_id != request.job_id
                or not receipt.accepted
            ):
                raise AgentControlPlaneUnavailable(
                    "the signed-dispatch port did not accept the admitted work item"
                )
        return AgentTaskDispatchResult(
            capability=capability, admission=admission, dispatch=receipt
        )

    async def get_work_item(
        self, request: WorkItemGetRequest
    ) -> WorkItemSnapshot | None:
        if not isinstance(request, WorkItemGetRequest):
            raise TypeError("request must be a validated WorkItemGetRequest")
        session = self._verified_session("kg:read")
        store = self._require_port(self._work_item_store, "work-item-store")
        with self._verified_client_context(session):
            result = await store.get(request, session=session)
        if result is not None and not isinstance(result, WorkItemSnapshot):
            raise AgentControlPlaneUnavailable(
                "the work-item store returned an invalid snapshot"
            )
        return result

    async def list_work_items(self, request: WorkItemListRequest) -> WorkItemPage:
        if not isinstance(request, WorkItemListRequest):
            raise TypeError("request must be a validated WorkItemListRequest")
        session = self._verified_session("kg:read")
        store = self._require_port(self._work_item_store, "work-item-store")
        with self._verified_client_context(session):
            result = await store.list(request, session=session)
        if not isinstance(result, WorkItemPage):
            raise AgentControlPlaneUnavailable(
                "the work-item store returned an invalid page"
            )
        return result

    async def cancel_work_item(
        self, request: WorkItemCancelRequest
    ) -> WorkItemSnapshot | None:
        if not isinstance(request, WorkItemCancelRequest):
            raise TypeError("request must be a validated WorkItemCancelRequest")
        session = self._verified_session("kg:write")
        store = self._require_port(self._work_item_store, "work-item-store")
        with self._verified_client_context(session):
            result = await store.cancel(request, session=session)
        if result is not None and not isinstance(result, WorkItemSnapshot):
            raise AgentControlPlaneUnavailable(
                "the work-item store returned an invalid snapshot"
            )
        return result

    @classmethod
    def _reject_authority_metadata(cls, value: Any) -> None:
        authority_names = {
            "actor",
            "actor_id",
            "audience",
            "auth",
            "authorization",
            "graph",
            "owner",
            "owner_id",
            "policy",
            "policy_version",
            "principal",
            "scope",
            "scopes",
            "tenant",
            "tenant_id",
        }
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in authority_names:
                    raise ValueError("metadata may not contain caller authority fields")
                cls._reject_authority_metadata(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                cls._reject_authority_metadata(nested)

    async def graph_rlm(self, request: GraphRlmRequest) -> GraphRlmResult:
        """Execute one typed RLM action under the injected verified authority."""
        if not isinstance(
            request,
            GraphRlmRunRequest,
        ):
            raise TypeError("request must be a validated GraphRlmRequest")

        try:
            resolved_session = self._verified_session(_GRAPH_RLM_SCOPES[type(request)])
            with self._verified_client_context(resolved_session):
                return await self._execute(request)
        except PermissionError as exc:
            return self._failed_result(request, exc, code="permission_denied")
        except Exception as exc:  # noqa: BLE001 - public API returns typed failures
            return self._failed_result(request, exc)

    async def _execute(self, request: GraphRlmRequest) -> GraphRlmResult:
        from meristem.rlm import run_rlm

        outcome = await run_rlm(request.task, request.input_text)
        if outcome.get("ok"):
            return GraphRlmRunResult(
                action="run",
                ok=True,
                task=request.task,
                result=str(outcome.get("result") or ""),
                usage=dict(outcome.get("usage") or {}),
                max_depth=int(outcome.get("max_depth") or 0),
            )
        return GraphRlmRunResult(
            action="run",
            ok=False,
            task=request.task,
            failure_class=str(outcome.get("failure_class") or "") or None,
            error=self._failure(
                RuntimeError("recursive run failed"), code="operation_failed"
            ),
        )

    def _failed_result(
        self,
        request: GraphRlmRequest,
        exc: BaseException,
        *,
        code: str = "operation_failed",
    ) -> GraphRlmResult:
        error = self._failure(exc, code=code)
        return GraphRlmRunResult(action="run", ok=False, task=request.task, error=error)

    @staticmethod
    def _failure(exc: BaseException, *, code: str) -> GraphRlmError:
        payload = public_error_payload(
            exc, code=code, context={"operation": "graph_rlm"}
        )
        return GraphRlmError(
            code=payload["error"]["code"],
            correlation_id=payload["error"]["correlation_id"],
            detail_ref=payload["error"].get("detail_ref"),
            retryable=payload["error"]["retryable"],
            error_class=payload["error_class"],
            failing_layer=payload["failing_layer"],
        )


def compose_agent_control_plane(
    eg_client: Any,
    session: GraphSession,
    *,
    capability_search: CapabilitySearchPort | None = None,
    agent_executor: AgentExecutionPort | None = None,
    work_item_store: WorkItemStorePort | None = None,
    signed_dispatch: SignedAgentDispatchPort | None = None,
) -> AgentControlPlane:
    """Compose AU application operations from verified inputs and explicit ports."""
    if agent_executor is None:
        from arbora.agent_runs import AgentRunPort

        agent_executor = AgentRunPort()
    return AgentControlPlane(
        eg_client,
        session,
        capability_search=capability_search,
        agent_executor=agent_executor,
        work_item_store=work_item_store,
        signed_dispatch=signed_dispatch,
    )
