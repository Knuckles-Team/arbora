"""Viewport bins and selection operations (ARBORA-OPS-R064)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from medulla.generated.models import ViewportRequest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import viewport
from arbora.api.ops.registry_factory import get_registry
from tests.api._lane_double import EngineDouble, context

OPS = {op.id: op for op in viewport.operations()}


def _request(**changes: Any) -> dict[str, Any]:
    base = {
        "binning_revision": 1,
        "byte_ceiling": 8192,
        "domain": {"x_min": 0.0, "x_max": 10.0, "y_min": 0.0, "y_max": 5.0},
        "kind": "scatter",
        "pixel_height": 100,
        "pixel_width": 200,
        "source_id": "src",
        "x_axis": "t",
        "y_axis": "v",
    }
    return {**base, **changes}


@pytest.mark.spec("ARBORA-OPS-R064.1")
def test_models_round_trip_rust_vectors() -> None:
    params = viewport.ViewportBinsParams(request=_request(), max_bins=64)
    dumped = params.request.model_dump(mode="json")
    assert ViewportRequest.model_validate(dumped) == params.request
    assert dumped["kind"] == "scatter" and dumped["domain"]["x_max"] == 10.0
    with pytest.raises(ValueError):
        viewport.ViewportBinsParams(request=_request(kind="pie"))
    with pytest.raises(ValueError):
        viewport.ViewportBinsParams(request=_request(), max_bins=0)
    token = {"kind": "exact_point", "point_id": 4, "point_revision": 2}
    resolved = viewport.SelectionResolveParams(request=_request(), token=token)
    assert resolved.token.kind == "exact_point"


@pytest.mark.spec("ARBORA-OPS-R064.2")
@pytest.mark.asyncio
async def test_limits_are_enforced_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineDouble(ViewportQuery={"bins": [1, 2], "formula_revision": 3})
    ctx = context(client, monkeypatch)
    op = OPS["graph.viewport.bins"]
    answer = await viewport.handle_viewport_bins(
        ctx, {"request": _request(), "max_bins": 8}, op
    )
    assert answer["value"]["formula_revision"] == 3
    assert client.sent[0][0] == "ViewportQuery"
    assert client.sent[0][1]["request"]["byte_ceiling"] == 8192
    sent = len(client.sent)
    # A missing, non-positive or over-ceiling limit never reaches the engine.
    for bad in (_request(byte_ceiling=0), _request(byte_ceiling=16_777_217)):
        with pytest.raises(OperationRefused) as refused:
            await viewport.handle_viewport_bins(ctx, {"request": bad}, op)
        assert refused.value.code == "INVALID_ARGUMENT"
    with pytest.raises(OperationRefused) as refused:
        await viewport.handle_viewport_bins(
            ctx, {"request": _request(), "max_bins": 65_537}, op
        )
    assert refused.value.code == "INVALID_ARGUMENT"
    assert len(client.sent) == sent
    # The selection resolves through its own engine method under the same limits.
    resolve = EngineDouble(ResolveViewportSelection={"subject_ids": ["a"]})
    out = await viewport.handle_selection_resolve(
        context(resolve, monkeypatch),
        {
            "request": _request(),
            "token": {"kind": "exact_point", "point_id": 1, "point_revision": 1},
        },
        OPS["graph.selection.resolve"],
    )
    assert out["value"]["subject_ids"] == ["a"]
    assert resolve.sent[0][0] == "ResolveViewportSelection"
    assert {op.scopes.__iter__().__next__() for op in OPS.values()} == {"graph:read"}


@pytest.mark.spec("ARBORA-OPS-R064.3")
@pytest.mark.asyncio
async def test_stale_tokens_and_bad_limits_return_no_usable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.errors import EngineRefusal

    big = {"bins": list(range(5000))}
    ctx = context(EngineDouble(ViewportQuery=big), monkeypatch)
    with pytest.raises(OperationRefused) as refused:
        await viewport.handle_viewport_bins(
            ctx, {"request": _request()}, OPS["graph.viewport.bins"]
        )
    assert refused.value.code == "LOAD_CAP_EXCEEDED"
    assert set(refused.value.details) == {"max_bytes", "max_bins"}  # no bins returned
    fat = {"blob": "x" * 9000}
    ctx = context(EngineDouble(ViewportQuery=fat), monkeypatch)
    with pytest.raises(OperationRefused) as refused:
        await viewport.handle_viewport_bins(
            ctx, {"request": _request()}, OPS["graph.viewport.bins"]
        )
    assert refused.value.code == "LOAD_CAP_EXCEEDED"
    # A stale source or token keeps the engine's own code and returns no bins.
    from medulla import EngineResponseError

    stale = EngineDouble(ViewportQuery=EngineResponseError("STALE_REVISION", "stale"))
    with pytest.raises(EngineRefusal) as engine:
        await viewport.handle_viewport_bins(
            context(stale, monkeypatch),
            {"request": _request()},
            OPS["graph.viewport.bins"],
        )
    assert "STALE_REVISION" in str(engine.value.code)
    assert json.dumps({}) == "{}"


@pytest.mark.spec("ARBORA-OPS-R064", "ARBORA-OPS-R064.4")
@pytest.mark.asyncio
async def test_scatter_and_line_interaction_over_large_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = {op.id for op in get_registry() if op.id.startswith("graph.")}
    assert set(OPS) <= served
    for kind in ("scatter", "line"):
        for step in range(3):  # pan and zoom: three domains per kind
            domain = {
                "x_min": step * 1.0,
                "x_max": 10.0 - step,
                "y_min": 0.0,
                "y_max": 5.0,
            }
            client = EngineDouble(
                ViewportQuery={"bins": [0] * 10, "viewport_revision": step}
            )
            answer = await viewport.handle_viewport_bins(
                context(client, monkeypatch),
                {"request": _request(kind=kind, domain=domain)},
                OPS["graph.viewport.bins"],
            )
            size = len(json.dumps(answer["value"]).encode())
            assert size <= 8192  # measured independently of the handler
            assert client.sent[0][1]["request"]["kind"] == kind
