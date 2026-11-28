"""The deploy run machine is described in the ontology (ARBORA-DEPLOY-R602.5)."""

from __future__ import annotations

import pytest

from arbora.deployment.run import (
    EDGES,
    INITIAL,
    TERMINALS,
    DeployEvent,
    DeployState,
)
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-DEPLOY-R602.5")
def test_deploy_run_machine_matches_its_ontology_description() -> None:
    check_machine(
        load_store(),
        name="DeployRun",
        states={s.value for s in DeployState},
        terminal={s.value for s in TERMINALS},
        events={e.value for e in DeployEvent},
        edges={(s.value, e.value, t.value) for (s, e), t in EDGES.items()},
        initial=INITIAL.value,
    )
