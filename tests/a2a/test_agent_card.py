"""The served card equals the capabilities the fixture exercises (ARBORA-A2A-R015).

The test reads the served card, then exercises each advertised capability once: history
through ``tasks/get``, streaming through ``tasks/resubscribe`` and push through its
registration method. A capability advertised and not exercised, or exercised and not
advertised, fails. The card also withholds history a pinned engine would refuse.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arbora.a2a.history import EngineTransitionHistory
from arbora.core.settings import RuntimeConfig
from tests.a2a.test_transition_history import HEADERS, _client, _Owned


@pytest.mark.spec("ARBORA-A2A-R015")
def test_served_card_matches_wired_authorities() -> None:
    """Advertised capabilities equal the ones the fixture exercises, no more and no less."""

    class Stream:
        finished = True

        async def batch(self) -> list[Any]:
            return []

        async def wait(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Streaming:
        async def resume_from_cursor(self, task_id: str, after_seq: int = 0) -> Any:
            return Stream()

    def advertised(client: TestClient) -> dict[str, bool]:
        card = client.get("/.well-known/agent-card.json", headers=HEADERS).json()
        found = card["capabilities"]
        return {
            "streaming": found["streaming"],
            "push": found["pushNotifications"],
            "history": found["stateTransitionHistory"],
        }

    def exercised(client: TestClient, task: Any) -> dict[str, bool]:
        rpc = lambda method, params: client.post(  # noqa: E731
            "/a2a",
            headers=HEADERS,
            json={"jsonrpc": "2.0", "id": "x", "method": method, "params": params},
        )
        history = rpc("tasks/get", {"id": task.id, "includeTransitionHistory": True})
        stream = rpc("tasks/resubscribe", {"id": task.id})
        # Push has no registration method in the served table: an unknown method.
        push = rpc("tasks/pushNotificationConfig/set", {"id": task.id})
        return {
            "streaming": stream.status_code == 200,
            "push": push.status_code != 404,
            "history": history.status_code == 200
            and "transitionHistory" in history.json()["result"],
        }

    cases = (
        (None, None),
        (EngineTransitionHistory(_Owned()), None),  # type: ignore[arg-type]
        (None, Streaming()),
        (EngineTransitionHistory(_Owned()), Streaming()),  # type: ignore[arg-type]
    )
    for history, streaming in cases:
        client, authority = _client(history, streaming=streaming)
        assert advertised(client) == exercised(client, authority.task)
        assert advertised(client)["history"] is (history is not None)
        assert advertised(client)["streaming"] is (streaming is not None)


@pytest.mark.spec("ARBORA-A2A-R015")
def test_card_withholds_history_the_pinned_engine_would_refuse() -> None:
    engine = _Owned()
    engine.work_items = SimpleNamespace()  # no list_transitions on the engine in use
    client, _authority = _client(EngineTransitionHistory(engine))  # type: ignore[arg-type]
    card = client.get("/.well-known/agent-card.json", headers=HEADERS).json()
    assert card["capabilities"]["stateTransitionHistory"] is False
    assert inspect.iscoroutinefunction(EngineTransitionHistory.transition_history)


@pytest.mark.spec("ARBORA-A2A-R013.3")
def test_push_capability_follows_wired_worker() -> None:
    from arbora.a2a.application import create_a2a_application
    from arbora.a2a.service import A2AService
    from tests.a2a._push_support import world
    from tests.a2a.test_application import Authenticator, Authority, Router

    def card(service: A2AService) -> dict[str, Any]:
        client = TestClient(
            create_a2a_application(service=service, authenticator=Authenticator())
        )
        return client.get("/.well-known/agent-card.json", headers=HEADERS).json()[
            "capabilities"
        ]

    unwired = A2AService(authority=Authority(), router=Router(), config=RuntimeConfig())
    assert card(unwired)["pushNotifications"] is False
    worker = world().worker
    wired = A2AService(
        authority=Authority(), router=Router(), push=worker, config=RuntimeConfig()
    )
    assert card(wired)["pushNotifications"] is True
    # The unwired method is unknown, so the card and the table agree.
    client = TestClient(
        create_a2a_application(service=unwired, authenticator=Authenticator())
    )
    response = client.post(
        "/a2a",
        headers=HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": "x",
            "method": "tasks/pushNotificationConfig/set",
            "params": {"id": "t", "url": "https://hooks.example.test/a"},
        },
    )
    assert response.status_code == 404


@pytest.mark.spec("ARBORA-A2A-R014.1")
def test_card_has_no_harness_field_before_admission_e2e() -> None:
    from arbora.a2a.service import A2AService
    from arbora.api.domain.a2a.models import A2AAgentCapabilities, A2AAgentCard
    from tests.a2a.test_application import Authority, Router

    card = A2AService(
        authority=Authority(), router=Router(), config=RuntimeConfig()
    ).agent_card("https://x/a2a")
    dumped = card.model_dump(mode="json", by_alias=True)
    assert not [key for key in dumped if "harness" in key.lower()]
    assert not [key for key in dumped["capabilities"] if "harness" in key.lower()]
    for model in (A2AAgentCapabilities, A2AAgentCard):
        assert not [name for name in model.model_fields if "harness" in name.lower()]
