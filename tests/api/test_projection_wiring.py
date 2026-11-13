"""Projection wiring: registry declaration and reader composition (ARBORA-OPS-R052.3)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke import bootstrap
from arbora.api.ops.projections import EngineProjectionReader, SchemaProjection
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Composite, EgMethod
from arbora.mcp_server.served_invocation import _with_projection_reader

pytestmark = pytest.mark.spec("ARBORA-OPS-R052.3")


async def test_registry_lists_operation_and_binder_composes_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import METHOD_IDS

    op = get_registry()["query.projections"]
    assert op.scopes == frozenset({"query:read"})
    assert isinstance(op.binding, Composite)
    assert op.binding.handler == "arbora.api.ops.query.handle_query_projections"
    assert "SchemaProjections" in METHOD_IDS

    composed = _with_projection_reader({"other": object()})
    assert isinstance(composed["projection_reader"], EngineProjectionReader)
    assert _with_projection_reader(None) is None
    bound = object()
    assert (
        _with_projection_reader({"projection_reader": bound})["projection_reader"]
        is bound
    )

    sent: list[tuple[Any, Any, Any]] = []
    entries = {
        "entries": [
            {
                "boundary_id": "sales",
                "required_scopes": ["data:sales"],
                "tables": ["orders"],
                "mapping_ref": "mapping:sales",
            }
        ]
    }

    async def dispatch(binding: Any, params: Any, context: Any) -> Any:
        sent.append((binding, params, context))
        return SimpleNamespace(payload=entries)

    monkeypatch.setattr(bootstrap, "dispatch_eg_method", dispatch)
    context = object()
    listed = await composed["projection_reader"].list(context)
    assert listed == (
        SchemaProjection(
            boundary="sales",
            required_scopes=("data:sales",),
            tables=("orders",),
            mapping_ref="mapping:sales",
        ),
    )
    assert sent == [
        (
            EgMethod(service="SchemaProjections", op="SchemaProjections"),
            {"op": {"op": "list"}},
            context,
        )
    ]
