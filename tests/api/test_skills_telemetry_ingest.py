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
