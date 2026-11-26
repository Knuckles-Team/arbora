"""Versioned definitions and the admission rules (ARBORA-FLEET-R002.3, R002.4, R002.6)."""

from __future__ import annotations

import pytest

from arbora.fleet.definitions import (
    REFUSALS,
    CompatibilityDecision,
    DefinitionGeneration,
    DefinitionRefused,
    admit_definition,
    parse_definition,
)

H1, H2 = "1" * 64, "2" * 64


def _def(version: str = "1.0.0", schema: str = H1, **extra):
    data = {
        "kind": "tool",
        "producer_id": "crm",
        "version": version,
        "input_schema": {"type": "object", "properties": {"a": {"type": "object"}}},
        "schema_digest": schema,
        "body_digest": H1,
    }
    data.update(extra)
    return parse_definition(data)


@pytest.mark.spec("ARBORA-FLEET-R002.3")
def test_nested_definition_round_trip_and_refusals() -> None:
    definition = _def(output_schema={"type": "object"})
    again = parse_definition(definition.model_dump(mode="json"))
    assert again == definition and definition.semver == (1, 0, 0)
    missing = _def().output_digest
    assert len(missing) == 64 and missing != _def(output_schema={}).output_digest
    with pytest.raises(DefinitionRefused) as caught:
        parse_definition({**definition.model_dump(), "version": ""})
    assert caught.value.code == "DEFINITION_VERSION_REQUIRED"
    with pytest.raises(DefinitionRefused) as caught:
        parse_definition({**definition.model_dump(), "unknown_required": 1})
    assert caught.value.code == "SCHEMA_INVALID"
    with pytest.raises(DefinitionRefused) as caught:
        _def(version="1.0")
    assert caught.value.code == "SCHEMA_INVALID"


def _decision(verdict="compatible", **kw):
    return CompatibilityDecision("d-1", verdict, **kw)


@pytest.mark.spec("ARBORA-FLEET-R002.4")
def test_admit_definition_rules() -> None:
    prior = _def("1.0.0")
    with pytest.raises(DefinitionRefused) as caught:
        admit_definition(prior, _def("1.0.0", schema=H2), None)
    assert caught.value.code == "DEFINITION_VERSION_CONFLICT"
    assert admit_definition(prior, _def("1.0.0"), None).decision_id is None
    receipt = admit_definition(prior, _def("1.1.0", schema=H2), _decision(), tenant="t")
    assert (receipt.old_version, receipt.new_version, receipt.decision_id) == (
        "1.0.0",
        "1.1.0",
        "d-1",
    )
    with pytest.raises(DefinitionRefused) as caught:
        admit_definition(prior, _def("1.0.1", schema=H2), _decision("breaking"))
    assert caught.value.code == "SCHEMA_INCOMPATIBLE"
    with pytest.raises(DefinitionRefused) as caught:
        admit_definition(prior, _def("2.0.0", schema=H2), _decision("breaking"))
    assert caught.value.code == "SCHEMA_INCOMPATIBLE"
    migrated = _decision("breaking", migration_id="m-1", readmitted=True)
    assert (
        admit_definition(prior, _def("2.0.0", schema=H2), migrated).new_version
        == "2.0.0"
    )
    assert admit_definition(None, _def(), None).old_version is None


@pytest.mark.spec("ARBORA-FLEET-R002.6")
def test_each_refusal_code_leaves_generation_unchanged() -> None:
    first, _ = DefinitionGeneration().publish({"tool:crm": _def("1.0.0")}, {})
    attempts = {
        "DEFINITION_VERSION_CONFLICT": ({"tool:crm": _def("0.9.0")}, {}),
        "PRODUCER_UNAVAILABLE": ({"tool:crm": _def("1.2.0", schema=H2)}, {}),
        "SCHEMA_INCOMPATIBLE": (
            {"tool:crm": _def("1.2.0", schema=H2)},
            {"tool:crm": _decision("breaking")},
        ),
    }
    for code, (candidates, decisions) in attempts.items():
        with pytest.raises(DefinitionRefused) as caught:
            first.publish(candidates, decisions)
        assert caught.value.code == code
        assert first.number == 1 and first.definitions["tool:crm"].version == "1.0.0"
    with pytest.raises(DefinitionRefused):
        first.publish(
            {"tool:crm": _def("1.1.0", schema=H2), "tool:z": _def("0.0.1")},
            {"tool:crm": _decision("breaking")},
        )
    assert "tool:z" not in first.definitions
    second, receipts = first.publish(
        {"tool:crm": _def("1.1.0", schema=H2)}, {"tool:crm": _decision()}
    )
    assert second.number == 2 and receipts[0].generation == 2
    assert set(REFUSALS) >= set(attempts) | {
        "DEFINITION_VERSION_REQUIRED",
        "SCHEMA_INVALID",
    }
