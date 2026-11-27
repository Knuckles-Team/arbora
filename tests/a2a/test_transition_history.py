"""Transition history reads through to the engine ledger (ARBORA-A2A-R011, R012.3, R015).

``tasks/get`` with ``includeTransitionHistory`` reads one ledger page after the ownership
check and returns the engine entries unchanged. A missing or failing reader refuses with
the typed history refusal, never an empty or partial history.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.history import EngineTransitionHistory
from arbora.a2a.service import A2AService
from arbora.core.settings import RuntimeConfig
from tests.a2a.test_application import Authenticator, Authority, Router

HEADERS = {"Authorization": "Bearer verified"}
LEDGER = [
    {"state": "submitted", "at_ms": 1_000, "revision": 1},
    {"state": "working", "at_ms": 2_000, "revision": 2},
    {"state": "completed", "at_ms": 3_000, "revision": 3},
]


class _Ledger:
    """Structural double of ``work_items.list_transitions``: pages in commit order."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list_transitions(self, **fields: Any) -> dict[str, Any] | None:
        self.calls.append(fields)
        if fields["tenant"] != "tenant:one":
            return None
        start = int(fields["cursor"] or 0)
        end = start + fields["limit"]
        more = end < len(LEDGER)
        return {
            "entries": LEDGER[start:end],
            "next_cursor": str(end) if more else None,
        }


class _Owned:
    """The ownership check and engine of ``WorkItemA2AAuthority`` for one tenant."""

    def __init__(self, tenant: str = "tenant:one", owned: bool = True) -> None:
        self.ledger = _Ledger()
        self.tenant, self.owned = tenant, owned
        self.work_items = self.ledger

    def _authorized_item(self, task_id: str, scope: str) -> dict[str, Any] | None:
        return {"id": task_id} if self.owned else None

    def _engine(self) -> tuple[Any, Any]:
        return object(), self

    def _session(self, scope: str) -> Any:
        return SimpleNamespace(tenant=self.tenant)

    _work_item_id = staticmethod(WorkItemA2AAuthority._work_item_id)


def _client(history: Any = None, *, streaming: Any = None) -> tuple[TestClient, Any]:
    authority = Authority()
    app = create_a2a_application(
        service=A2AService(
            authority=authority,
            router=Router(),
            history=history,
            streaming=streaming,
            config=RuntimeConfig(),
        ),
        authenticator=Authenticator(),
    )
    return TestClient(app), authority


def _get(client: TestClient, task: Any, **extra: Any) -> Any:
    return client.post(
        "/a2a",
        headers=HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": "get",
            "method": "tasks/get",
            "params": {"id": task.id, **extra},
        },
    )


@pytest.mark.spec("ARBORA-A2A-R011")
def test_history_matches_engine_ledger_exactly() -> None:
    owned = _Owned()
    client, authority = _client(EngineTransitionHistory(owned))  # type: ignore[arg-type]
    plain = _get(client, authority.task).json()["result"]
    assert "transitionHistory" not in plain or plain["transitionHistory"] is None

    first = _get(
        client, authority.task, includeTransitionHistory=True, historyLimit=2
    ).json()["result"]
    assert first["transitionHistory"] == [
        {"state": "submitted", "atMs": 1_000, "revision": 1},
        {"state": "working", "atMs": 2_000, "revision": 2},
    ]
    assert first["transitionHistoryNextCursor"] == "2"
    last = _get(
        client,
        authority.task,
        includeTransitionHistory=True,
        historyLimit=2,
        historyCursor="2",
    ).json()["result"]
    assert last["transitionHistory"] == [
        {"state": "completed", "atMs": 3_000, "revision": 3}
    ]
    assert last["transitionHistoryNextCursor"] is None
    assert owned.ledger.calls[0]["work_item_id"].startswith("workitem:orchestrator:")
    assert {c["tenant"] for c in owned.ledger.calls} == {"tenant:one"}
    # Strict bool and the page bound.
    assert (
        _get(client, authority.task, includeTransitionHistory="yes").status_code == 400
    )
    assert (
        _get(
            client, authority.task, includeTransitionHistory=True, historyLimit=201
        ).status_code
        == 400
    )


@pytest.mark.spec("ARBORA-A2A-R011")
def test_a_foreign_task_reads_no_ledger() -> None:
    foreign = _Owned(owned=False)
    history = EngineTransitionHistory(foreign)  # type: ignore[arg-type]
    import asyncio

    assert asyncio.run(history.transition_history("a2a-" + "1" * 64)) is None
    assert foreign.ledger.calls == []
    other_tenant = _Owned(tenant="tenant:other")
    refused = EngineTransitionHistory(other_tenant)  # type: ignore[arg-type]
    with pytest.raises(Exception) as unavailable:
        asyncio.run(refused.transition_history("a2a-" + "1" * 64))
    assert type(unavailable.value).__name__ == "A2ATransitionHistoryUnavailable"


@pytest.mark.spec("ARBORA-A2A-R012.3")
def test_get_task_raises_typed_refusal_without_authority() -> None:
    client, authority = _client(None)
    absent = _get(client, authority.task, includeTransitionHistory=True)
    assert absent.status_code == 501 and absent.json()["error"]["code"] == -32012

    class Raising:
        async def transition_history(self, task_id: str, **kw: Any) -> Any:
            raise RuntimeError("secret engine detail")

    client, authority = _client(Raising())
    failing = _get(client, authority.task, includeTransitionHistory=True)
    assert failing.status_code == 501 and failing.json()["error"]["code"] == -32012
    assert "secret" not in failing.text and "result" not in failing.json()
