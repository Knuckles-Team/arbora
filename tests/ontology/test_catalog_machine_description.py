"""The catalog serving machine is described in the ontology (ARBORA-FLEET-R081.3)."""

from __future__ import annotations

import pytest

from arbora.api.domain.fleet.catalog_machine import (
    CATALOG_EDGES,
    CatalogEvent,
    CatalogState,
)
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-FLEET-R081.3")
def test_classes_roll_up_to_the_core_classes() -> None:
    check_machine(
        load_store(),
        name="Catalog",
        machine_name="CatalogServingMachine",
        states={s.value for s in CatalogState},
        terminal={CatalogState.CLOSING.value},
        events={e.value for e in CatalogEvent},
        edges={(s.value, e.value, t.value) for (s, e, _), t in CATALOG_EDGES.items()},
        guards={"HasSnapshot": "has_snapshot"},
        initial=CatalogState.COLD.value,
    )
