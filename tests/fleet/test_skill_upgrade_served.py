"""Skill upgrades are admitted on engine contract compatibility (ARBORA-FLEET-R034.13).

The engine is a structural double answering ``Decide`` for ``skill_upgrade`` in
the shape of the engine ``main`` ``SkillUpgradeReceipt``; arbora sends the
previous and new consumer contracts and applies the answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.fleet.admission import AdmissionGate
from arbora.fleet.engine_admission import EngineAdmission
from tests.fleet._certified_support import World, h, no_pin_lag
from tests.fleet.test_admission_wiring import GOOD  # noqa: F401


class UpgradeEngine:
    """Admits equal-major changes; a breaking change needs the declared migration."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def send(self, method: str, params: dict[str, Any], tenant: str) -> Any:
        assert method == "Decide"
        self.requests.append(params)
        body = next(p for p in params["params"] if p["name"] == "skill_upgrade")[
            "value"
        ]["value"]
        removed = {f["name"] for f in body["previous"]["fields"]} - {
            f["name"] for f in body["new"]["fields"]
        }
        if removed and not body.get("declared_migration_id"):
            # The engine answers a refusal as an abstained record, not an error.
            return {
                "records": [
                    {
                        "record_id": "dec-0",
                        "outcome": {
                            "outcome": "abstained",
                            "reasons": [
                                {
                                    "reason": "family",
                                    "family": "skill_upgrade",
                                    "code": "SKILL_UPGRADE_BREAKING_WITHOUT_MIGRATION",
                                    "detail": "breaking",
                                }
                            ],
                        },
                        "answer": None,
                    }
                ]
            }
        return {
            "record_id": "dec-1",
            "answer": {
                "type": "skill_upgrade",
                "value": {
                    "declared_migration_id": body.get("declared_migration_id"),
                    "schema_version": 1,
                    "previous_consumer_contract_digest": h("p"),
                    "new_consumer_contract_digest": h("n"),
                },
            },
        }


def _definition(version: str, schema: str) -> dict[str, Any]:
    return {
        "kind": "skill",
        "producer_id": "crm/triage",
        "version": version,
        "input_schema": {"type": "object"},
        "schema_digest": h(schema),
        "body_digest": h(f"body:{version}"),
    }


def _upgrade(
    prior: str, new: str, *, removed: bool = False, migration: str | None = None
) -> dict[str, Any]:
    fields = [{"name": "a", "required": True, "type_name": "string"}]
    wider = fields + [{"name": "b", "required": False, "type_name": "string"}]
    upgrade: dict[str, Any] = {
        "prior": _definition(prior, "s0"),
        "candidate": _definition(new, "s1"),
        "previous_fields": wider,
        "new_fields": fields if removed else wider,
    }
    if migration:
        upgrade["migration_id"] = migration
    return {"skill": {"id": "triage", "text": "ok", "upgrade": upgrade}}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> tuple[World, UpgradeEngine]:
    no_pin_lag(monkeypatch)
    built = World()
    engine = UpgradeEngine()
    gate = AdmissionGate(port=EngineAdmission(engine.send, lambda _t: {}))
    fields = {
        f: getattr(built.services, f) for f in built.services.__dataclass_fields__
    }
    fields["admission"] = gate
    built.services = type(built.services)(**fields)
    return built, engine


def _assert_one_declared_option(sent: dict[str, Any]) -> None:
    """The engine family declares one option, ``admit_upgrade`` (engine main)."""
    assert [o["option_id"] for o in sent["candidates"]["options"]] == ["admit_upgrade"]


@pytest.mark.spec("ARBORA-FLEET-R034.13")
def test_compatible_breaking_and_migrated_upgrades(world) -> None:
    served, engine = world
    assert served.call(_upgrade("1.0.0", "1.1.0"), key="a").code == "OK"
    sent = engine.requests[0]
    assert sent["question"]["kind"] == "skill_upgrade"
    _assert_one_declared_option(sent)
    assert served.audit.events[-1]["admission.decision_ids"] == "dec-1"

    breaking = served.call(_upgrade("1.0.0", "2.0.0", removed=True), key="b")
    assert breaking.code == "SKILL_UPGRADE_BREAKING_WITHOUT_MIGRATION"
    assert len(served.runtime.calls) == 1  # no effect for the refusal

    migrated = served.call(
        _upgrade("1.0.0", "2.0.0", removed=True, migration="mig-7"), key="c"
    )
    assert migrated.code == "OK"
    assert served.audit.events[-1]["admission.migration_id"] == "mig-7"

    equal = served.call(_upgrade("1.0.0", "1.0.0"), key="d")
    assert equal.code == "DEFINITION_VERSION_CONFLICT"  # same version, changed digest


@pytest.mark.spec("ARBORA-FLEET-R034.13")
def test_pinned_engine_without_the_shape_refuses_naming_it(monkeypatch) -> None:
    from arbora.pin_lag import medulla as pin_lag
    from arbora.pin_lag.unavailable import PinLagUnavailable

    def missing(names):
        raise PinLagUnavailable("SkillUpgradeRequest")

    monkeypatch.setattr(pin_lag, "require_models", missing)
    built = World()
    engine = UpgradeEngine()
    fields = {
        f: getattr(built.services, f) for f in built.services.__dataclass_fields__
    }
    fields["admission"] = AdmissionGate(
        port=EngineAdmission(engine.send, lambda _t: {})
    )
    built.services = type(built.services)(**fields)
    refused = built.call(_upgrade("1.0.0", "1.1.0"))
    assert refused.code == "PRODUCER_UNAVAILABLE"
    assert refused.details["missing"] == "SkillUpgradeRequest"
    assert engine.requests == [] and built.runtime.calls == []
