"""The migration record machines are described in the ontology (ARBORA-HOST-R041.1)."""

from __future__ import annotations

import pyoxigraph as ox
import pytest

from arbora.control_plane.migrations import (
    CohortReadCutover,
    CutoverEvent,
    CutoverState,
    LegacyRetirement,
    MigrationEvent,
    MigrationPlan,
    MigrationState,
    RetirementEvent,
    RetirementState,
    WriteCutoverFence,
)
from tests.ontology.machine_description import check_machine, load_store


@pytest.fixture(scope="module")
def store() -> ox.Store:
    return load_store()


def _edges(table: dict) -> set[tuple[str, str, str]]:  # type: ignore[type-arg]
    return {(s.value, e.value, t.value) for (s, e), t in table.items()}


@pytest.mark.spec("ARBORA-HOST-R041.1")
@pytest.mark.parametrize(
    ("name", "states", "events", "terminal", "table", "initial"),
    [
        pytest.param(
            "MigrationPlan",
            MigrationState,
            MigrationEvent,
            {"retired"},
            MigrationPlan,
            "planned",
            id="migration_plan",
        ),
        pytest.param(
            "CohortReadCutover",
            CutoverState,
            CutoverEvent,
            {"rolled_back"},
            CohortReadCutover,
            "active",
            id="cohort_read_cutover",
        ),
        pytest.param(
            "WriteCutoverFence",
            CutoverState,
            CutoverEvent,
            {"rolled_back"},
            WriteCutoverFence,
            "active",
            id="write_cutover_fence",
        ),
        pytest.param(
            "LegacyRetirement",
            RetirementState,
            RetirementEvent,
            {"retired", "rolled_back"},
            LegacyRetirement,
            "ready",
            id="legacy_retirement",
        ),
    ],
)
def test_machine_matches_its_ontology_description(
    store: ox.Store,
    name,
    states,
    events,
    terminal,
    table,
    initial,  # type: ignore[no-untyped-def]
) -> None:
    check_machine(
        store,
        name=name,
        states={s.value for s in states},
        terminal=terminal,
        events={e.value for e in events},
        edges=_edges(table.EDGES),
        initial=initial,
    )
    assert {s.value for s in table.TERMINALS} == terminal
    assert table.INITIAL.value == initial
