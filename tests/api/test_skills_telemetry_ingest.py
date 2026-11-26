"""Skill hook events as OpenTelemetry logs (ARBORA-OPS-R953)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import skills_telemetry
from tests.api._knowledge_support import NO_OP, caller_context, record_engine


def _record(
    ref: str = "skill:a@1",
    outcome: str = "ok",
    extra: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def attr(key: str, value: dict[str, Any]) -> dict[str, Any]:
        return {"key": key, "value": value}

    return {
        "timeUnixNano": "5000000000",
        "attributes": [
            attr("skill.ref", {"stringValue": ref}),
            attr("skill.version", {"stringValue": "1"}),
            attr("skill.chosen_by", {"stringValue": "user"}),
            attr("session.run_id", {"stringValue": "run-1"}),
            attr("skill.outcome", {"stringValue": outcome}),
            attr("skill.tokens_in", {"intValue": "7"}),
            *(extra or []),
        ],
    }


def _body(*records: dict[str, Any]) -> dict[str, Any]:
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]}


@pytest.mark.spec("ARBORA-OPS-R953")
async def test_otlp_logs_map_to_invocations_and_unknown_attribute_refuses_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = record_engine(monkeypatch, skills_telemetry, {"ingested": 2})
    context = caller_context()
    error = _record(
        outcome="error",
        extra=[{"key": "skill.error_code", "value": {"stringValue": "BOOM"}}],
    )
    await skills_telemetry.handle_skill_telemetry_ingest(
        context, _body(_record(), error), NO_OP
    )
    method, op, idempotent = sent[0]
    assert (method, op["op"], idempotent) == ("SkillTelemetry", "ingest", True)
    first, second = op["events"]
    assert first["chosen_by"] == "user" and first["outcome"] == "ok"
    assert first["ended_ms"] == 5000 and first["tokens_in"] == 7
    assert second["outcome"] == {"error": {"code": "BOOM"}}
    # A human caller sends no pseudonym and no principal.
    assert all("principal_pseudonym" not in e for e in op["events"])
    assert first["tenant_id"] == "tenant:one"
    assert first["invocation_id"] != second["invocation_id"]
    bad = _record(extra=[{"key": "user.prompt", "value": {"stringValue": "x"}}])
    with pytest.raises(OperationRefused) as refused:
        await skills_telemetry.handle_skill_telemetry_ingest(
            context, _body(_record(), bad), NO_OP
        )
    assert refused.value.details == {"reason": "TELEMETRY_FIELD_UNKNOWN"}
    with pytest.raises(OperationRefused):
        await skills_telemetry.handle_skill_telemetry_ingest(
            context, _body({**_record(), "body": {"stringValue": "prompt"}}), NO_OP
        )
    assert len(sent) == 1


@pytest.mark.spec("ARBORA-OPS-R953")
def test_logs_route_drives_the_same_op_with_the_same_closed_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from arbora.api.http.app import create_api_application
    from arbora.api.http.auth import HTTPAuthenticationError

    sent = record_engine(monkeypatch, skills_telemetry, {"ingested": 1})
    op = skills_telemetry.operations()[0]
    assert op.http is not None and op.http.path == "/api/v1/logs"
    keys: list[Any] = []

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

    async def invoke(op_id, params, caller, surface, **kwargs: Any) -> Any:
        assert op_id == "skills.telemetry.ingest"
        keys.append(kwargs["idempotency_key"])
        try:
            value = await skills_telemetry.handle_skill_telemetry_ingest(
                caller_context(), params, op
            )
        except OperationRefused as refusal:
            return SimpleNamespace(code=refusal.code, details=refusal.details)
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
    good = {"Authorization": "Bearer good"}
    with TestClient(parent) as client:
        assert client.post("/api/v1/logs", json=_body(_record())).status_code == 401
        first = client.post("/api/v1/logs", json=_body(_record()), headers=good)
        again = client.post("/api/v1/logs", json=_body(_record()), headers=good)
        assert first.status_code == again.status_code == 200, first.text
        bad = _record(extra=[{"key": "user.prompt", "value": {"stringValue": "x"}}])
        refused = client.post("/api/v1/logs", json=_body(bad), headers=good)
        assert refused.status_code == 400
    # Same body, same key; the exporter sent none.
    assert keys[0] == keys[1] and keys[0] != keys[2]
    assert len(sent) == 2
    assert sent[0][1]["events"][0]["skill_ref"] == "skill:a@1"
