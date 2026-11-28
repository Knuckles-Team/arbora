"""The Web UI identity machines are described in the ontology (ARBORA-HOST-R041.2)."""

from __future__ import annotations

import pytest

from arbora.control_plane.webui import (
    ConversationEvent,
    ConversationIdentity,
    ConversationState,
    NotificationEvent,
    NotificationIdentity,
    NotificationState,
    SupportEvent,
    SupportIdentity,
    SupportState,
)
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-HOST-R041.2")
@pytest.mark.parametrize(
    ("name", "states", "events", "table"),
    [
        pytest.param(
            "ConversationIdentity",
            ConversationState,
            ConversationEvent,
            ConversationIdentity,
            id="conversation",
        ),
        pytest.param(
            "NotificationIdentity",
            NotificationState,
            NotificationEvent,
            NotificationIdentity,
            id="notification",
        ),
        pytest.param(
            "SupportIdentity", SupportState, SupportEvent, SupportIdentity, id="support"
        ),
    ],
)
def test_machine_matches_its_ontology_description(name, states, events, table) -> None:  # type: ignore[no-untyped-def]
    check_machine(
        load_store(),
        name=name,
        states={s.value for s in states},
        terminal={s.value for s in table.TERMINALS},
        events={e.value for e in events},
        edges={(s.value, e.value, t.value) for (s, e), t in table.EDGES.items()},
        initial=table.INITIAL.value,
    )
