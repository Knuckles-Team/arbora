"""The served alert intake and the channel gateway admission.

ARBORA-MESSAGING-R007 (served channel gateway) and ARBORA-MESSAGING-R008
(alert intake operation): handler behavior, the served HTTP route, and the
composition rule that lets the served network host own intake.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arbora.api.http.auth import HTTPAuthenticationError
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import messaging as alert_ops
from arbora.messaging.models import InboundEvent, SendResult
from tests._eg_fakes import service_session


class _Backend:
    id = "telegram"
    is_connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, channel_id: str, text: str, **_: Any) -> SendResult:
        self.sent.append((channel_id, text))
        return SendResult(
            success=True, message_id="7", platform="telegram", channel_id=channel_id
        )


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Backend]:
    from arbora.messaging.host_ports import bind_host_ports, reset_host_ports
    from arbora.messaging.service import MessagingService

    backend = _Backend()
    service = MessagingService.instance()
    monkeypatch.setitem(service._backends, "telegram", backend)
    monkeypatch.setenv("MESSAGING_DEFAULT_PLATFORM", "telegram")
    monkeypatch.setenv("MESSAGING_DEFAULT_CHANNEL", "42")
    bind_host_ports(
        action_gate=lambda request, engine: SimpleNamespace(
            allowed=True, decision="allow", reason="test"
        )
    )
    yield backend
    reset_host_ports()


_SESSION = service_session("tenant-a", "tenant-a")


def _context(**services: Any) -> SimpleNamespace:
    return SimpleNamespace(
        caller=SimpleNamespace(
            effective_scopes=frozenset({"kg:write"}),
            session=_SESSION,
        ),
        services=services,
        service_identity=False,
        owner=None,
        registry_digest="d",
        fleet_decision=None,
    )


def _op() -> Any:
    return alert_ops.operations()[0]


@pytest.mark.spec("ARBORA-MESSAGING-R008")
async def test_the_lgtm_text_shape_reaches_the_default_telegram_channel(
    telegram: _Backend,
) -> None:
    result = await alert_ops.handle_messaging_alert(
        _context(), {"text": "disk full on r820"}, _op()
    )
    assert result == {
        "delivered": True,
        "platform": "telegram",
        "channel_id": "42",
        "message_id": "7",
    }
    assert telegram.sent == [("42", "disk full on r820")]


@pytest.mark.spec("ARBORA-MESSAGING-R008")
async def test_an_alertmanager_body_is_rendered_one_line_per_alert(
    telegram: _Backend,
) -> None:
    body = {
        "status": "firing",
        "alerts": [
            {"status": "firing", "annotations": {"summary": "node down"}},
            {"status": "resolved", "labels": {"alertname": "HighLoad"}},
        ],
    }
    await alert_ops.handle_messaging_alert(_context(), body, _op())
    assert telegram.sent == [("42", "[firing] node down\n[resolved] HighLoad")]


@pytest.mark.spec("ARBORA-MESSAGING-R008")
async def test_refusals_are_typed_and_send_nothing(
    telegram: _Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(OperationRefused) as empty:
        await alert_ops.handle_messaging_alert(_context(), {"status": "firing"}, _op())
    assert empty.value.code == "INVALID_ARGUMENT"
    monkeypatch.setenv("MESSAGING_DEFAULT_CHANNEL", "")
    with pytest.raises(OperationRefused) as unset:
        await alert_ops.handle_messaging_alert(_context(), {"text": "x"}, _op())
    assert unset.value.code == "UNAVAILABLE"
    assert telegram.sent == []


@pytest.mark.spec("ARBORA-MESSAGING-R008")
async def test_a_refused_send_is_a_refusal_not_a_delivery(
    telegram: _Backend,
) -> None:
    from arbora.messaging.host_ports import bind_host_ports

    bind_host_ports(
        action_gate=lambda request, engine: SimpleNamespace(
            allowed=False, decision="deny", reason="no send scope"
        )
    )
    with pytest.raises(OperationRefused) as refused:
        await alert_ops.handle_messaging_alert(_context(), {"text": "x"}, _op())
    assert refused.value.code == "UNAVAILABLE"
    assert telegram.sent == []


class _FleetGateway:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def effect(self, op: Any, params: Any, caller: Any) -> Any:
        return SimpleNamespace(effect=op.effect, required_scopes=frozenset())

    async def call(self, caller, server, tool, arguments, **_: Any) -> Any:
        self.calls.append((server, tool, dict(arguments)))
        return self.result


@pytest.mark.spec("ARBORA-MESSAGING-R008")
async def test_mattermost_alerts_go_through_the_fleet_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESSAGING_DEFAULT_PLATFORM", "mattermost")
    monkeypatch.setenv("MESSAGING_DEFAULT_CHANNEL", "chan-1")
    gateway = _FleetGateway({"id": "post-9"})
    result = await alert_ops.handle_messaging_alert(
        _context(fleet_gateway=gateway), {"text": "backup failed"}, _op()
    )
    assert result["message_id"] == "post-9"
    ((server, tool, arguments),) = gateway.calls
    assert (server, tool) == ("mattermost-mcp", "mattermost_mcp_posts")
    assert arguments["action"] == "create_post"
    assert json.loads(arguments["params_json"]) == {
        "channel_id": "chan-1",
        "message": "backup failed",
    }

    refused = _FleetGateway({"error": "Operation failed"})
    with pytest.raises(OperationRefused) as failure:
        await alert_ops.handle_messaging_alert(
            _context(fleet_gateway=refused), {"text": "x"}, _op()
        )
    assert failure.value.code == "UPSTREAM_FAILED"
    with pytest.raises(OperationRefused) as unbound:
        await alert_ops.handle_messaging_alert(_context(), {"text": "x"}, _op())
    assert unbound.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-MESSAGING-R008")
def test_the_operation_is_registered_with_its_route_and_send_scope() -> None:
    from arbora.api.ops import get_registry

    op = next(op for op in get_registry() if op.id == "messaging.alert")
    assert op.http is not None and op.http.path == "/api/v1/messaging/alert"
    assert op.scopes == frozenset({"kg:write"})


@pytest.mark.spec("ARBORA-MESSAGING-R008")
def test_the_served_route_admits_a_bearer_and_refuses_anonymous(
    telegram: _Backend,
) -> None:
    from arbora.api.http.app import create_api_application

    op = _op()

    class Registry:
        digest = "digest"

        def __iter__(self):
            return iter((op,))

        def find(self, caller, *, surface, policy):
            return (op,)

    class Auth:
        async def authenticate(self, request):
            if request.headers.get("authorization") != "Bearer good":
                raise HTTPAuthenticationError("bearer required")
            return SimpleNamespace(authenticated=True, request_id="r1")

        def is_console_request(self, request, caller):
            return False

    async def invoke(op_id, params, caller, surface, **_: Any) -> Any:
        validated = op.params.model_validate(params).model_dump(
            by_alias=True, exclude_none=True
        )
        value = await alert_ops.handle_messaging_alert(_context(), validated, op)
        return SimpleNamespace(code="OK", value=value)

    async def visible(op: Any, caller: Any) -> bool:
        return True

    parent = FastAPI()
    parent.mount(
        "/api/v1",
        create_api_application(
            services=SimpleNamespace(registry=Registry()),
            visibility=visible,
            authenticator=Auth(),
            invoke=invoke,
        ),
    )
    with TestClient(parent) as client:
        anonymous = client.post("/api/v1/messaging/alert", json={"text": "x"})
        assert anonymous.status_code == 401
        admitted = client.post(
            "/api/v1/messaging/alert",
            json={"text": "lgtm says hi"},
            headers={"Authorization": "Bearer good"},
        )
        assert admitted.status_code == 200, admitted.text
        assert admitted.json()["result"]["delivered"] is True
    assert telegram.sent == [("42", "lgtm says hi")]


@pytest.mark.spec("ARBORA-MESSAGING-R007")
def test_the_served_network_host_owns_intake_only_on_an_explicit_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.mcp_server import composition

    monkeypatch.setattr(
        composition, "configured_platforms", lambda engine: ("telegram",)
    )
    monkeypatch.setenv("KG_DAEMON_ROLE", "client")
    monkeypatch.setenv("MESSAGING_INTAKE_PLATFORMS", "telegram")

    def plan(flag: Any, served: bool) -> bool:
        return composition.detect_composition(
            None, messaging_intake_enabled=flag, served_network=served
        ).messaging_intake_configured

    assert plan("true", True) is True
    assert plan(True, True) is True
    assert plan("false", True) is False
    assert plan(None, True) is False
    assert plan("true", False) is False
    monkeypatch.setenv("MESSAGING_INTAKE_PLATFORMS", "slack")
    assert plan("true", True) is False


@pytest.mark.spec("ARBORA-MESSAGING-R007")
def test_only_listed_senders_are_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    from arbora.messaging.planner_handler import sender_admitted

    event = InboundEvent(platform="telegram", channel_id="5", user_id="5", content="hi")
    monkeypatch.delenv("MESSAGING_ALLOWED_SENDERS", raising=False)
    assert sender_admitted(event) is False
    monkeypatch.setenv("MESSAGING_ALLOWED_SENDERS", "mattermost:5, telegram:5")
    assert sender_admitted(event) is True
    assert sender_admitted(event.model_copy(update={"user_id": "6"})) is False
    assert sender_admitted(event.model_copy(update={"user_id": ""})) is False


@pytest.mark.spec("ARBORA-MESSAGING-R007")
async def test_an_unlisted_sender_gets_no_turn_and_no_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.messaging.host_ports import bind_host_ports, reset_host_ports
    from arbora.messaging.planner_handler import create_planner_handler

    turns: list[str] = []

    async def turn(engine: Any, text: str, *, session: Any = "") -> str:
        turns.append(text)
        return "reply"

    bind_host_ports(turn_handler=turn)
    monkeypatch.setenv("MESSAGING_ALLOWED_SENDERS", "telegram:1")
    backend = _Backend()
    try:
        handler = await create_planner_handler(None)
        await handler(
            InboundEvent(
                platform="telegram", channel_id="9", user_id="9", content="/help"
            ),
            cast(Any, backend),
        )
    finally:
        reset_host_ports()
    assert turns == []
    assert backend.sent == []


@pytest.mark.spec("ARBORA-MESSAGING-R800.1")
def test_the_registry_discovers_the_adapters_the_wheel_publishes() -> None:
    import tomllib
    from pathlib import Path

    from arbora.messaging import registry

    project = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    published = project["project"]["entry-points"][registry._ENTRY_POINT_GROUP]
    assert {"telegram", "mattermost"} <= set(published)
    assert registry.MessagingRegistry().is_installed("telegram")
