"""Source executor over the governed fleet gateway (ARBORA-FLEET-R043.7)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.registry import Effect
from arbora.fleet.source_executor import (
    FleetSourceBinding,
    FleetSourceExecutor,
    ForeignReadSourceRequest,
    SourceReadRequest,
)


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def call(self, caller, server, tool, arguments, *, expected_effect):
        self.calls.append((caller, server, tool, dict(arguments), expected_effect))
        if server == "denied":
            raise PermissionError("no grant")
        if server == "slow":
            await asyncio.sleep(5)
        if server == "write":
            raise RuntimeError("fleet effect changed before dispatch")
        if server == "bad":
            return {"value": {"oops": 1}}
        page = int(arguments.get("cursor", "0"))
        nxt = {"next_cursor": str(page + 1)} if page < 9 else {}
        return {"value": {"items": [{"n": page * 2}, {"n": page * 2 + 1}], **nxt}}


class CappedGateway:
    def __init__(self, *, delay: float = 0.0, rows_per_page: int = 1) -> None:
        self.delay = delay
        self.rows_per_page = rows_per_page
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def call(self, _caller, _server, _tool, arguments, *, expected_effect):
        assert expected_effect is Effect.READ
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            page = int(arguments.get("cursor", "0"))
            return {
                "value": {
                    "items": [
                        {"n": page * self.rows_per_page + i}
                        for i in range(self.rows_per_page)
                    ],
                    "next_cursor": str(page + 1),
                }
            }
        finally:
            self.active -= 1


class ForeignGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def call(self, caller, server, tool, arguments, *, expected_effect):
        self.calls.append((caller, server, tool, dict(arguments), expected_effect))
        page = int(arguments.get("cursor", "0"))
        payload = {
            "value": {
                "items": [
                    {"id": f"issue-{page}-a", "name": "Ada", "state": "open"},
                    {"id": f"issue-{page}-b", "name": "Bea", "state": "closed"},
                ],
            }
        }
        if page == 0:
            payload["value"]["next_cursor"] = "1"
        return payload


class RecordingForeignClient:
    def __init__(self, models) -> None:
        self.models = models
        self.calls: list[tuple[str, Any]] = []
        self.execution = models.ForeignReadExecutionRef(
            execution_id=41, query_digest="a" * 64
        )
        self.source_identity = models.ForeignReadSourceIdentity(
            source_ref="mcp://connector/issues",
            source_revision=7,
            schema_digest="b" * 64,
            snapshot_ref="c" * 64,
        )
        self.total_rows = 0
        self.total_bytes = 0

    async def open(self, request):
        self.calls.append(("open", request))
        return self.models.ForeignReadOpenResult(
            schema_version=1,
            execution=self.execution,
            expires_at_ms=4_000_000_000_000,
            next_sequence=0,
            source_identity=self.source_identity,
        )

    async def chunk(self, request):
        self.calls.append(("chunk", request))
        self.total_rows += len(request.rows)
        self.total_bytes += 100
        return self.models.ForeignReadChunkResult(
            execution=self.execution,
            sequence=request.sequence,
            accepted_rows=len(request.rows),
            total_rows=self.total_rows,
            total_bytes=self.total_bytes,
            next_sequence=request.sequence + 1,
        )

    async def complete(self, request):
        self.calls.append(("complete", request))
        return self.models.ForeignReadCompleteResult(
            execution=self.execution,
            sequence=request.sequence,
            total_rows=self.total_rows,
            total_bytes=self.total_bytes,
        )

    async def abort(self, request):
        self.calls.append(("abort", request))
        return self.models.ForeignReadAbortResult(
            execution=self.execution,
            sequence=request.sequence,
            reason=request.reason,
        )


def _foreign_request() -> ForeignReadSourceRequest:
    return ForeignReadSourceRequest(
        source_id="issues",
        connector="github-mcp",
        tool="list_issues",
        source_name="issues",
        contract=SimpleNamespace(
            contract_iri="https://example.invalid/contracts/issues",
            source_id="issues",
            entity="issue",
            class_iri="https://example.invalid/Issue",
            key_field="id",
            access_kind="mcp_tool",
            operation="list_issues",
            predicates=(
                ("https://example.invalid/name", "name"),
                ("https://example.invalid/state", "state"),
            ),
        ),
        approval={
            "connector": "github-mcp",
            "contract_digest": "a" * 64,
            "tenant_id": "tenant-a",
            "registration": {
                "approval": "approved",
                "class_iri": "https://example.invalid/Issue",
                "connector": "github-mcp",
                "contract_iri": "https://example.invalid/contracts/issues",
                "entity": "issue",
                "key_field": "id",
                "operation": "list_issues",
                "resource_uri": "ontology://issues",
                "source_id": "issues",
                "source_kind": "mcp_tool",
            },
        },
        plan_digest="b" * 64,
        query_digest="a" * 64,
        limits={
            "max_rows": 100,
            "max_bytes": 4096,
            "chunk_rows": 2,
            "chunk_bytes": 1024,
            "deadline_ms": 10_000,
        },
        max_pages=3,
        max_rows=10,
    )


@pytest.mark.spec("ARBORA-FLEET-R043.7")
async def test_reads_page_through_fleet_call_with_caller_delegation_and_per_source_errors() -> (
    None
):
    gateway, caller = FakeGateway(), object()
    executor = FleetSourceExecutor(gateway, caller)
    out = await executor.read_all(
        [
            SourceReadRequest(
                "a", "github-mcp", "list_issues", {"state": "open"}, max_pages=3
            ),
            SourceReadRequest("b", "gitlab-mcp", "list_issues", max_rows=3),
            SourceReadRequest("c", "denied", "t"),
            SourceReadRequest("d", "slow", "t", timeout_s=0.05),
            SourceReadRequest("e", "write", "t"),
            SourceReadRequest("f", "bad", "t"),
        ]
    )
    a, b, c, d, e, f = out
    # pagination is bounded by pages and by rows
    assert a.ok and len(a.rows) == 6 and a.pages == 3 and a.truncated
    assert b.ok and len(b.rows) == 3 and b.truncated
    # every call carries the caller and a READ effect expectation
    assert all(call[0] is caller and call[4] is Effect.READ for call in gateway.calls)
    assert gateway.calls[0][3] == {"state": "open"} and {
        "state": "open",
        "cursor": "1",
    } in [c[3] for c in gateway.calls]
    # a failing source returns its own error and does not fail the others
    assert (c.error_code, d.error_code, e.error_code, f.error_code) == (
        "SOURCE_NOT_GRANTED",
        "SOURCE_TIMEOUT",
        "SOURCE_UNAVAILABLE",
        "SOURCE_BAD_RESPONSE",
    )


@pytest.mark.spec("ARBORA-FLEET-R043.7")
async def test_source_and_row_caps_are_enforced() -> None:
    gateway = CappedGateway()
    executor = FleetSourceExecutor(gateway, object())

    result = await executor.read(
        SourceReadRequest("pages", "server", "read", max_pages=99, max_rows=99_999)
    )
    assert result.ok and result.pages == 20 and len(result.rows) == 20
    assert gateway.calls == 20

    gateway = CappedGateway(rows_per_page=2_000)
    result = await FleetSourceExecutor(gateway, object()).read(
        SourceReadRequest("rows", "server", "read", max_pages=99, max_rows=99_999)
    )
    assert (
        result.ok
        and result.pages == 5
        and len(result.rows) == 10_000
        and result.truncated
    )
    assert gateway.calls == 5


@pytest.mark.spec("ARBORA-FLEET-R043.7")
async def test_query_source_count_concurrency_and_deadline_caps_are_enforced() -> None:
    requests = [
        SourceReadRequest(str(i), "server", "read", max_pages=1) for i in range(32)
    ]
    gateway = CappedGateway(delay=0.001)
    executor = FleetSourceExecutor(gateway, object(), overall_timeout_s=1.0)
    results = await executor.read_all(requests)
    assert len(results) == 32 and all(result.ok for result in results)
    assert gateway.calls == 32 and gateway.max_active <= 8

    with pytest.raises(ValueError, match="too many sources"):
        await executor.read_all(
            requests + [SourceReadRequest("too-many", "server", "read")]
        )

    slow = CappedGateway(delay=0.1)
    results = await FleetSourceExecutor(
        slow, object(), overall_timeout_s=0.001
    ).read_all([SourceReadRequest(str(i), "server", "read") for i in range(8)])
    assert len(results) == 8
    assert all(result.error_code == "SOURCE_TIMEOUT" for result in results)
    await asyncio.sleep(0)
    assert slow.active == 0


@pytest.mark.spec("ARBORA-FLEET-R043.7")
@pytest.mark.asyncio
async def test_foreign_read_maps_contract_columns_and_correlates_after_complete() -> (
    None
):
    models = pytest.importorskip("agent_connector_sdk.adapters.foreign_read")
    gateway, caller = ForeignGateway(), object()
    foreign = RecordingForeignClient(models)
    executor = FleetSourceExecutor(gateway, caller, foreign_client=foreign)
    correlated: list[object] = []

    async def correlate(execution: object) -> dict[str, object]:
        correlated.append(execution)
        return {"query": "completed"}

    result = await executor.read_foreign(_foreign_request(), correlate=correlate)

    assert result.ok and result.rows == 4 and result.pages == 2
    assert result.query_result == {"query": "completed"}
    assert correlated == [foreign.execution]
    assert [name for name, _ in foreign.calls] == [
        "open",
        "chunk",
        "chunk",
        "complete",
    ]
    first_chunk = foreign.calls[1][1]
    assert first_chunk.source_identity == foreign.source_identity
    assert [row.id for row in first_chunk.rows] == ["issue-0-a", "issue-0-b"]
    assert first_chunk.rows[0].columns == {"name": "Ada", "state": "open"}
    assert first_chunk.rows[0].provenance == [
        "connector:github-mcp",
        "contract:https://example.invalid/contracts/issues",
        "source:issues",
    ]
    assert all(call[0] is caller and call[4] is Effect.READ for call in gateway.calls)


@pytest.mark.spec("ARBORA-FLEET-R043.7")
@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [{"max_pages": 1}, {"max_rows": 1}])
async def test_incomplete_foreign_read_aborts_before_query(
    limits: dict[str, int],
) -> None:
    models = pytest.importorskip("agent_connector_sdk.adapters.foreign_read")
    foreign = RecordingForeignClient(models)
    original = _foreign_request()
    request = ForeignReadSourceRequest(**{**original.__dict__, **limits})
    correlated: list[object] = []

    async def correlate(execution: object) -> None:
        correlated.append(execution)

    result = await FleetSourceExecutor(
        ForeignGateway(), object(), foreign_client=foreign
    ).read_foreign(request, correlate=correlate)

    assert result.error_code == "SOURCE_BAD_RESPONSE" and result.truncated
    assert [name for name, _ in foreign.calls][-1] == "abort"
    assert all(name != "complete" for name, _ in foreign.calls)
    assert correlated == []


@pytest.mark.spec("ARBORA-FLEET-R043.7")
@pytest.mark.asyncio
async def test_read_approved_requires_catalog_projection_and_owner_source_name() -> (
    None
):
    models = pytest.importorskip("agent_connector_sdk.adapters.foreign_read")
    gateway, caller = ForeignGateway(), object()
    foreign = RecordingForeignClient(models)
    request = _foreign_request()
    mapping = SimpleNamespace(
        mapping_id=request.contract.contract_iri,
        approved=True,
        source_id=request.contract.source_id,
        entity=request.contract.entity,
        class_iri=request.contract.class_iri,
        key_field=request.contract.key_field,
        predicates=request.contract.predicates,
    )
    result = await FleetSourceExecutor(
        gateway, caller, foreign_client=foreign
    ).read_approved(
        request,
        mapping=mapping,
        resource_uri="ontology://issues",
    )
    assert result.ok and result.rows == 4

    held = await FleetSourceExecutor(
        gateway, caller, foreign_client=foreign
    ).read_approved(
        request,
        mapping=SimpleNamespace(
            mapping_id=request.contract.contract_iri,
            approved=False,
            source_id=request.contract.source_id,
            entity=request.contract.entity,
            class_iri=request.contract.class_iri,
            key_field=request.contract.key_field,
            predicates=request.contract.predicates,
        ),
        resource_uri="ontology://issues",
    )
    assert held.error_code == "SOURCE_HELD"


@pytest.mark.spec("ARBORA-FLEET-R043.7")
@pytest.mark.asyncio
async def test_read_approved_refuses_receipt_resource_or_owner_name_mismatch() -> None:
    models = pytest.importorskip("agent_connector_sdk.adapters.foreign_read")
    gateway, caller = ForeignGateway(), object()
    foreign = RecordingForeignClient(models)
    request = _foreign_request()
    mapping = SimpleNamespace(
        mapping_id=request.contract.contract_iri,
        approved=True,
        source_id=request.contract.source_id,
        entity=request.contract.entity,
        class_iri=request.contract.class_iri,
        key_field=request.contract.key_field,
        predicates=request.contract.predicates,
    )
    mismatch = await FleetSourceExecutor(
        gateway, caller, foreign_client=foreign
    ).read_approved(
        request,
        mapping=mapping,
        resource_uri="ontology://wrong",
    )
    assert mismatch.error_code == "SOURCE_HELD"
    assert [name for name, _ in foreign.calls] == []

    wrong_tool = ForeignReadSourceRequest(
        **{**request.__dict__, "tool": "unapproved_operation"}
    )
    mismatch = await FleetSourceExecutor(
        gateway, caller, foreign_client=foreign
    ).read_approved(
        wrong_tool,
        mapping=mapping,
        resource_uri="ontology://issues",
    )
    assert mismatch.error_code == "SOURCE_HELD"
    assert foreign.calls == [] and gateway.calls == []

    wrong_name = ForeignReadSourceRequest(
        **{**request.__dict__, "source_name": "other-owner/issues"}
    )
    mismatch = await FleetSourceExecutor(
        gateway, caller, foreign_client=foreign
    ).read_approved(
        wrong_name,
        mapping=mapping,
        resource_uri="ontology://issues",
    )
    assert mismatch.error_code == "SOURCE_HELD"
    assert [name for name, _ in foreign.calls] == []


@pytest.mark.spec("ARBORA-FLEET-R043.7")
def test_from_engine_composes_the_graph_scoped_sdk_adapter() -> None:
    foreign_read = pytest.importorskip("agent_connector_sdk.adapters.foreign_read")
    seen: list[str] = []
    client = object()

    class GraphCompute:
        def for_graph(self, graph: str):
            seen.append(graph)
            return SimpleNamespace(async_client=client)

    engine = SimpleNamespace(graph_compute=GraphCompute())
    executor = FleetSourceExecutor.from_engine(
        object(), object(), engine, graph="tenant:g"
    )

    assert seen == ["tenant:g"]
    assert isinstance(executor._foreign_client, foreign_read.ForeignReadClient)
    assert isinstance(
        executor._foreign_client._transport,
        foreign_read.NativeRequestResponseAdapter,
    )


@pytest.mark.asyncio
async def test_approved_source_binding_builds_request_from_catalog_identity() -> None:
    request = _foreign_request()
    mapping = SimpleNamespace(
        source_id="issues", mapping_id=request.contract.contract_iri
    )
    source = SimpleNamespace(
        mapping=mapping,
        contract=request.contract,
        receipt=request.approval,
        connector="github-mcp",
        resource_uri="ontology://issues",
    )
    seen: dict[str, Any] = {}

    class StubExecutor:
        async def read_approved(self, built, *, mapping, resource_uri, correlate):
            seen.update(
                request=built,
                mapping=mapping,
                resource_uri=resource_uri,
                correlate=correlate,
            )
            return "result"

    binding = FleetSourceBinding(StubExecutor(), source)  # type: ignore[arg-type]
    correlate = object()
    result = await binding.read(
        plan_digest="d" * 64,
        query_digest="e" * 64,
        limits={
            "max_rows": 10,
            "max_bytes": 1000,
            "chunk_rows": 2,
            "chunk_bytes": 500,
            "deadline_ms": 1000,
        },
        arguments={"state": "open"},
        correlate=correlate,  # type: ignore[arg-type]
    )

    assert result == "result"
    built = seen["request"]
    assert built.source_name == "issues"
    assert built.source_id == "issues"
    assert built.connector == "github-mcp"
    assert built.tool == "list_issues"
    assert built.arguments == {"state": "open"}
    assert built.approval is request.approval
    assert seen["mapping"] is mapping
    assert seen["resource_uri"] == "ontology://issues"
    assert seen["correlate"] is correlate


@pytest.mark.spec("ARBORA-FLEET-R043.7")
@pytest.mark.asyncio
async def test_foreign_read_aborts_on_bad_source_row_without_leaking_payload() -> None:
    models = pytest.importorskip("agent_connector_sdk.adapters.foreign_read")

    class BadGateway(ForeignGateway):
        async def call(self, *args, **kwargs):
            self.calls.append(
                (args[1], args[2], dict(args[3]), kwargs["expected_effect"])
            )
            return {"value": {"items": [{"name": "missing-id"}]}}

    foreign = RecordingForeignClient(models)
    result = await FleetSourceExecutor(
        BadGateway(), object(), foreign_client=foreign
    ).read_foreign(_foreign_request())

    assert not result.ok
    assert (result.error_code, result.error_reason) == (
        "SOURCE_BAD_RESPONSE",
        "the source row or foreign-read request was invalid",
    )
    assert [name for name, _ in foreign.calls] == ["open", "abort"]
    assert "missing-id" not in (result.error_reason or "")


@pytest.mark.spec("ARBORA-FLEET-R043.7")
@pytest.mark.asyncio
async def test_foreign_read_timeout_covers_open_and_aborts_after_open() -> None:
    models = pytest.importorskip("agent_connector_sdk.adapters.foreign_read")

    class SlowOpen(RecordingForeignClient):
        async def open(self, request):
            self.calls.append(("open", request))
            await asyncio.sleep(1)
            return await super().open(request)

    slow_open = SlowOpen(models)
    opened_result = await FleetSourceExecutor(
        ForeignGateway(), object(), foreign_client=slow_open
    ).read_foreign(_foreign_request_for_timeout())
    assert opened_result.error_code == "SOURCE_TIMEOUT"
    # No server handle was returned, so there is no safe remote abort to send.
    assert [name for name, _ in slow_open.calls] == ["open"]

    class SlowChunk(RecordingForeignClient):
        def __init__(self, models):
            super().__init__(models)
            self.started = asyncio.Event()

        async def chunk(self, request):
            self.calls.append(("chunk", request))
            self.started.set()
            await asyncio.sleep(1)
            return await super().chunk(request)

    slow_chunk = SlowChunk(models)
    chunk_result = await FleetSourceExecutor(
        ForeignGateway(), object(), foreign_client=slow_chunk
    ).read_foreign(_foreign_request_for_timeout())
    assert chunk_result.error_code == "SOURCE_TIMEOUT"
    assert [name for name, _ in slow_chunk.calls] == ["open", "chunk", "abort"]


def _foreign_request_for_timeout() -> ForeignReadSourceRequest:
    request = _foreign_request()
    return ForeignReadSourceRequest(
        **{
            **request.__dict__,
            "timeout_s": 0.01,
        }
    )


@pytest.mark.spec("ARBORA-FLEET-R043.7")
@pytest.mark.asyncio
async def test_foreign_read_cancellation_sends_remote_abort_before_reraising() -> None:
    models = pytest.importorskip("agent_connector_sdk.adapters.foreign_read")

    class BlockingChunk(RecordingForeignClient):
        def __init__(self, models):
            super().__init__(models)
            self.started = asyncio.Event()

        async def chunk(self, request):
            self.calls.append(("chunk", request))
            self.started.set()
            await asyncio.Event().wait()

    foreign = BlockingChunk(models)
    task = asyncio.create_task(
        FleetSourceExecutor(
            ForeignGateway(), object(), foreign_client=foreign
        ).read_foreign(_foreign_request())
    )
    await foreign.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [name for name, _ in foreign.calls] == ["open", "chunk", "abort"]
