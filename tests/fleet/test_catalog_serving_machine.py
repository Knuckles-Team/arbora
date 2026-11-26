"""Catalog serving readiness is an explicit state machine (ARBORA-FLEET-R081)."""

from __future__ import annotations

import inspect
import re

import pytest

from arbora.fleet import catalog_machine as cm
from arbora.fleet import multiplexer as multiplexer_module
from arbora.fleet.catalog_machine import (
    CATALOG_EDGES,
    CatalogEvent,
    CatalogState,
    IllegalCatalogTransition,
    transition,
)

_LEGAL = [(s, e, g, t) for (s, e, g), t in CATALOG_EDGES.items()]


def _edge_id(edge: tuple) -> str:
    source, event, guard, target = edge
    return f"{source.value}-{event.value}-{target.value}"


@pytest.mark.spec("ARBORA-FLEET-R081.1")
@pytest.mark.parametrize("edge", _LEGAL, ids=[_edge_id(e) for e in _LEGAL])
def test_legal_edge(edge) -> None:
    source, event, guard, target = edge
    assert transition(source, event, has_snapshot=bool(guard)) is target


@pytest.mark.spec("ARBORA-FLEET-R081.1")
def test_every_illegal_pair_is_refused() -> None:
    legal = {(s, e) for s, e, _, _ in _LEGAL}
    for source in CatalogState:
        for event in CatalogEvent:
            if (source, event) in legal:
                continue
            with pytest.raises(IllegalCatalogTransition) as refused:
                transition(source, event)
            assert refused.value.code == "CATALOG_TRANSITION_ILLEGAL"
            assert refused.value.machine == "catalog_serving"
            assert (refused.value.from_state, refused.value.event) == (source, event)


@pytest.mark.spec("ARBORA-FLEET-R081.2")
@pytest.mark.parametrize("state", list(CatalogState), ids=lambda s: s.value)
def test_readiness_flags_are_functions_of_state(state: CatalogState) -> None:
    expected = state is CatalogState.SERVING
    assert cm.autoscaling_ready(state) is expected
    assert cm.convergence_ready(state) is expected


class _FailingReader:
    async def read(self) -> object:
        raise ConnectionError("engine unavailable")


@pytest.mark.spec("ARBORA-FLEET-R081")
@pytest.mark.spec("ARBORA-FLEET-R081.4")
async def test_failed_first_read_is_cold_not_empty() -> None:
    mux = object.__new__(multiplexer_module.MCPMultiplexer)
    mux._catalog = None
    mux._catalog_state = CatalogState.COLD
    mux._fleet_catalog_reader = _FailingReader()
    with pytest.raises(ConnectionError):
        await mux.refresh_engine_catalog()
    assert mux.catalog_state is CatalogState.COLD
    assert mux._catalog is None  # not an empty catalog
    assert not cm.autoscaling_ready(mux.catalog_state)


@pytest.mark.spec("ARBORA-FLEET-R081.4")
async def test_served_failed_first_read_reports_cold_in_status() -> None:
    mux = object.__new__(multiplexer_module.MCPMultiplexer)
    mux._catalog = None
    mux._catalog_state = CatalogState.COLD
    mux._fleet_catalog_reader = _FailingReader()
    with pytest.raises(ConnectionError):
        await mux.refresh_engine_catalog()
    # The state the status and readiness reads derive from is Cold, and a later
    # successful read leaves Cold only through Loading.
    assert (mux.catalog_state, cm.convergence_ready(mux.catalog_state)) == (
        CatalogState.COLD,
        False,
    )
    mux._catalog_event(CatalogEvent.LOAD_START)
    assert mux.catalog_state is CatalogState.LOADING


@pytest.mark.spec("ARBORA-FLEET-R081.4")
def test_only_transition_writes_catalog_state() -> None:
    source = inspect.getsource(multiplexer_module)
    writes = re.findall(r"^\s*self\._catalog_state\s*=(.*)$", source, flags=re.M)
    assert sorted("transition(" in line for line in writes) == [False, True]
    assert [line for line in writes if "transition(" not in line] == [
        " _catalog_machine.CatalogState.COLD"
    ]


@pytest.mark.spec("ARBORA-FLEET-R081.2")
def test_bootstrap_stores_no_readiness_boolean() -> None:
    """Readiness has one authority, the catalog state machine; bootstrap keeps none."""
    from arbora.mcp_server import bootstrap

    assert not hasattr(bootstrap, "_BUNDLED_SKILL_READINESS")
    assert not hasattr(bootstrap, "bundled_skill_readiness")
    source = inspect.getsource(bootstrap)
    assert "skill_readiness" not in source
