"""Explicit source-shaped native opt-in; not installed producer evidence."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from epistemic_graph.generated import models

from arbora import work_items
from arbora.a2a.native_history import native_read
from arbora.api.invoke.pipeline import OperationRefused

pytestmark = pytest.mark.spec("ARBORA-A2A-R001.3.3")


class CandidateClaim(models.MethodClaimWorkItemParams):
    completed_text_history: bool = False


class CandidateRead(models.MethodGetRunWorkItemParams):
    completed_text_history: bool = False


class CandidateView(models.RunWorkItemView):
    checkpoint_selection: dict | None = None


@pytest.mark.parametrize("enabled", [False, True])
def test_claim_optin_is_wrapper_only_and_legacy_omits_it(monkeypatch, enabled):
    monkeypatch.setattr(models, "MethodClaimWorkItemParams", CandidateClaim)
    monkeypatch.setattr(work_items, "_tenant", lambda: "tenant")
    calls = []

    def claim(request, **kwargs):
        calls.append((request, kwargs))
        return {"claimed": False}

    engine = SimpleNamespace(work_items=SimpleNamespace(claim=claim))
    assert (
        work_items.claim_specific(
            engine,
            "item",
            token="worker",  # sanitizer:ignore - synthetic worker identity for local claim stub
            lease_ttl_s=5,
            completed_text_history=enabled,
        )
        is None
    )
    request, kwargs = calls[0]
    assert "completed_text_history" not in request
    assert kwargs == ({"completed_text_history": True} if enabled else {})


def test_missing_claim_schema_refuses_before_native_call(monkeypatch):
    monkeypatch.delitem(
        models.MethodClaimWorkItemParams.model_fields,
        "completed_text_history",
        raising=False,
    )
    with pytest.raises(work_items.NativeWorkItemRequired):
        work_items.claim_specific(
            None,
            "item",
            token="worker",  # sanitizer:ignore - synthetic worker identity in refusal fixture
            lease_ttl_s=5,
            completed_text_history=True,
        )


def test_missing_claim_model_refuses_before_native_call(monkeypatch):
    monkeypatch.delattr(models, "MethodClaimWorkItemParams")
    with pytest.raises(work_items.NativeWorkItemRequired):
        work_items.claim_specific(
            None,
            "item",
            token="worker",  # sanitizer:ignore - synthetic worker identity in refusal fixture
            lease_ttl_s=5,
            completed_text_history=True,
        )


@pytest.mark.asyncio
async def test_missing_owner_read_model_is_typed_refusal(monkeypatch):
    from arbora.a2a.run_usage import read_run_usage

    monkeypatch.delattr(models, "MethodGetRunWorkItemParams")
    with pytest.raises(OperationRefused):
        await read_run_usage(
            None,
            session=None,
            task=None,
            item={},
            owner_ref="owner",
            owner_graph="graph",
            completed_text_history=True,
        )


@pytest.mark.asyncio
async def test_profile_read_opts_in_without_mutating_input(monkeypatch):
    from epistemic_graph.generated import coordination

    from arbora import current_plan_publication
    from arbora.api.mcp import caller
    from arbora.mcp_server import task_plan_runtime

    monkeypatch.setattr(models, "MethodGetRunWorkItemParams", CandidateRead)
    monkeypatch.setattr(models, "RunWorkItemView", CandidateView)
    monkeypatch.setattr(caller, "caller_from_session", lambda *a, **k: object())
    monkeypatch.setattr(
        current_plan_publication, "_session_client_scope", lambda *a: nullcontext()
    )

    async def admit(*args):
        return None

    calls = []

    async def send(client, args, graph):
        calls.append((args, graph))
        return object()

    monkeypatch.setattr(task_plan_runtime, "_admit", admit)
    monkeypatch.setattr(coordination, "send_get_run_work_item", send)
    monkeypatch.setattr(coordination, "decode_get_run_work_item", lambda _: None)
    engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda _: SimpleNamespace(async_client=object())
        )
    )
    session = SimpleNamespace(ensure_authority_current=lambda: None)
    args = {"tenant": "tenant", "run_id": "run"}
    assert (
        await native_read(engine, session, "graph", object(), "get_run_work_item", args)
        is None
    )
    assert calls == [({**args, "completed_text_history": True}, "graph")]
    assert "completed_text_history" not in args


@pytest.mark.asyncio
async def test_missing_read_optin_refuses_before_dispatch(monkeypatch):
    monkeypatch.delitem(
        models.MethodGetRunWorkItemParams.model_fields,
        "completed_text_history",
        raising=False,
    )
    with pytest.raises(OperationRefused) as error:
        await native_read(None, None, "graph", object(), "get_run_work_item", {})
    assert error.value.code == "UPGRADE_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_historical_owner_read_uses_explicit_client_optin(monkeypatch, enabled):
    from arbora.a2a import run_usage

    monkeypatch.setattr(models, "MethodGetRunWorkItemParams", CandidateRead)
    monkeypatch.setattr(run_usage, "project_run_usage", lambda *a, **k: {})
    calls = []

    async def get_run(**kwargs):
        calls.append(kwargs)
        return None

    client = SimpleNamespace(work_items=SimpleNamespace(get_run=get_run))
    engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda _: SimpleNamespace(async_client=client)
        )
    )
    assert (
        await run_usage.read_run_usage(
            engine,
            session=SimpleNamespace(tenant="tenant"),
            task=object(),
            item={"metadata": {"a2a_run_id": "run"}},
            owner_ref="owner",
            owner_graph="graph",
            completed_text_history=enabled,
        )
        == {}
    )
    expected = {"tenant": "tenant", "run_id": "run"}
    if enabled:
        expected["completed_text_history"] = True
    assert calls == [expected]
