"""Truthful completed-text activation, not a promise of graph readiness."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.history_capability import (
    EXTENSION_HEADER,
    HISTORY_EXTENSION,
    native_history_contract_available,
    requested_history,
)
from arbora.a2a.routing import A2AAssemblyUnavailable
from tests.a2a.test_application import _app

pytestmark = pytest.mark.spec("ARBORA-A2A-R001.3.3")
HEADERS = {"Authorization": "Bearer verified", "Idempotency-Key": "send-key"}
BODY = {
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "do work"}],
            "messageId": "fresh-turn",
            "contextId": "a2a-context-" + "2" * 64,
        }
    },
}


def test_absent_support_refuses_activation_without_dispatch(monkeypatch):
    app, _, authority = _app()
    calls = []
    monkeypatch.setattr(authority, "dispatch", lambda **kwargs: calls.append(kwargs))
    client = TestClient(app)
    assert (
        "extensions"
        not in client.get("/.well-known/agent-card.json", headers=HEADERS).json()[
            "capabilities"
        ]
    )
    response = client.post(
        "/a2a", headers={**HEADERS, EXTENSION_HEADER: HISTORY_EXTENSION}, json=BODY
    )
    assert response.status_code == 503
    assert EXTENSION_HEADER not in response.headers
    assert calls == []


def test_activation_is_bound_to_dispatch_and_echoed_only_on_success(monkeypatch):
    app, _, authority = _app()
    monkeypatch.setattr(
        authority, "supports_completed_text_history", lambda: True, raising=False
    )
    calls = []

    async def dispatch(**kwargs):
        calls.append(kwargs)
        return authority.task

    monkeypatch.setattr(authority, "dispatch", dispatch)
    client = TestClient(app)
    card = client.get("/.well-known/agent-card.json", headers=HEADERS).json()
    assert card["capabilities"]["extensions"][0]["uri"] == HISTORY_EXTENSION
    assert card["capabilities"]["extensions"][0]["required"] is False
    response = client.post(
        "/a2a", headers={**HEADERS, EXTENSION_HEADER: HISTORY_EXTENSION}, json=BODY
    )
    assert response.status_code == 200
    assert response.headers[EXTENSION_HEADER] == HISTORY_EXTENSION
    assert calls[0]["require_completed_text_history"] is True
    monkeypatch.setattr(authority, "supports_completed_text_history", lambda: False)
    refused = client.post(
        "/a2a", headers={**HEADERS, EXTENSION_HEADER: HISTORY_EXTENSION}, json=BODY
    )
    assert refused.status_code == 503
    assert EXTENSION_HEADER not in refused.headers
    assert len(calls) == 1


@pytest.mark.parametrize(
    "value", ["", HISTORY_EXTENSION + "," + HISTORY_EXTENSION, "x" * 4097]
)
def test_malformed_activation_is_not_a_legacy_fallback(value):
    with pytest.raises(ValueError):
        requested_history(value)


def test_unknown_owned_version_refuses_and_unrelated_extension_does_not_activate():
    with pytest.raises(A2AAssemblyUnavailable):
        requested_history(HISTORY_EXTENSION[:-1] + "2")
    assert requested_history("https://example.com/other/v1") is False
    assert requested_history(None) is False


def test_enabled_flag_alone_cannot_advertise(monkeypatch):
    from arbora.mcp_server import harness_host

    authority = WorkItemA2AAuthority(lambda: None, completed_text_history=True)
    assert authority.supports_completed_text_history() is False
    monkeypatch.setattr(harness_host, "_CHECKPOINT_PROVIDER_ACTIVE", False)
    assert native_history_contract_available(SimpleNamespace()) is False


def test_schema_shaped_wiring_fixture_requires_every_boundary(monkeypatch):
    """Introspection fixture only; not native qualification or graph readiness."""
    from epistemic_graph import client
    from epistemic_graph.generated import coordination, models

    from arbora.mcp_server import harness_host

    fields = {
        "MethodGetRunWorkItemParams": {"completed_text_history", "selection"},
        "MethodClaimWorkItemParams": {"completed_text_history"},
        "RunWorkItemView": {"checkpoint_selection", "completed_checkpoint"},
        "ConversationTurnAdmission": {"history_profile"},
        "RunCompletionInput": {"completed_checkpoint"},
        "NativeClaimWorkItemResult": {"fresh_execution_grant"},
    }
    for name, names in fields.items():
        monkeypatch.setattr(
            models,
            name,
            SimpleNamespace(model_fields=dict.fromkeys(names)),
            raising=False,
        )

    def supported(self, *, completed_text_history=False):
        return None

    for method in (
        "get_conversation_head",
        "get_run_work_item",
        "claim_work_item",
        "submit_work_item",
        "commit_work_item_result",
    ):
        for kind in ("send", "decode"):
            monkeypatch.setattr(
                coordination, f"{kind}_{method}", supported, raising=False
            )
    monkeypatch.setattr(client.WorkItemClient, "claim", supported)
    monkeypatch.setattr(client.WorkItemClient, "get_run", supported)
    monkeypatch.setattr(harness_host, "_CHECKPOINT_PROVIDER_ACTIVE", True)
    registry = SimpleNamespace(get=lambda _: object())
    host = SimpleNamespace(services=SimpleNamespace(registry=registry))
    assert native_history_contract_available(host) is True
    monkeypatch.setattr(registry, "get", lambda _: None)
    assert native_history_contract_available(host) is False
    monkeypatch.setattr(registry, "get", lambda _: object())
    monkeypatch.setattr(client.WorkItemClient, "claim", lambda self: None)
    assert native_history_contract_available(host) is False


def test_repeated_http_activation_headers_refuse_before_dispatch(monkeypatch):
    app, _, authority = _app()
    calls = []
    monkeypatch.setattr(authority, "dispatch", lambda **kwargs: calls.append(kwargs))
    response = TestClient(app).post(
        "/a2a",
        json=BODY,
        headers=[
            *HEADERS.items(),
            (EXTENSION_HEADER, HISTORY_EXTENSION),
            (EXTENSION_HEADER, HISTORY_EXTENSION[:-1] + "2"),
        ],
    )
    assert response.status_code == 400
    assert EXTENSION_HEADER not in response.headers
    assert calls == []


@pytest.mark.parametrize(
    "operation", ["register_harness_host", "unregister_harness_host"]
)
def test_partial_lifecycle_cannot_leave_advertisement_active(monkeypatch, operation):
    from agent_utilities import host_ports

    from arbora.mcp_server import harness_host

    monkeypatch.setattr(harness_host, "_CHECKPOINT_PROVIDER_ACTIVE", True)

    def fail(_provider):
        raise RuntimeError("unregister failed")

    monkeypatch.setattr(host_ports, "register_native_toolset_provider", fail)
    with pytest.raises(RuntimeError, match="unregister failed"):
        getattr(harness_host, operation)()
    assert harness_host.checkpoint_provider_active() is False


@pytest.mark.asyncio
async def test_authority_rechecks_before_admission_and_claim(monkeypatch):
    authority = WorkItemA2AAuthority(lambda: pytest.fail("engine touched"))
    with pytest.raises(A2AAssemblyUnavailable):
        await authority.dispatch(
            message=None,
            idempotency_key="new",
            decision=None,
            require_completed_text_history=True,
        )
    with pytest.raises(A2AAssemblyUnavailable):
        await authority._run(
            None,
            item_id="i",
            session=None,
            agent_name="a",
            task="t",
            require_completed_text_history=True,
        )
