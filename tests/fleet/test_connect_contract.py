"""The connector contract record and its delta (ARBORA-FLEET-R036.1, R036.2)."""

from __future__ import annotations

import pytest

from arbora.fleet.connect_contract import (
    ConnectContract,
    build_connect_contract,
    contract_delta,
)


def _contract(**changes):
    fields = {
        "connector": "crm",
        "manifest": "manifest-v1",
        "ontologies": {"ontology://crm": "<ttl>", "ontology://core": "<core>"},
        "seed": "seed-v1",
        "observed_at_ms": 5,
    }
    fields.update(changes)
    return build_connect_contract(
        fields["connector"],
        fields["manifest"],
        fields["ontologies"],
        fields["seed"],
        observed_at_ms=fields["observed_at_ms"],
    )


@pytest.mark.spec("ARBORA-FLEET-R036.1")
def test_contract_record_round_trips() -> None:
    contract = _contract()
    assert ConnectContract.from_dict(contract.as_dict()) == contract
    assert set(contract.keys()) == {
        "manifest",
        "ontology:ontology://crm",
        "ontology:ontology://core",
        "seed",
    }
    assert _contract(seed=None).seed_digest == ""
    assert "seed" not in _contract(seed=None).keys()
    with pytest.raises(ValueError):
        _contract(connector="  ")
    with pytest.raises(ValueError):
        ConnectContract("crm", "NOT-HEX")


@pytest.mark.spec("ARBORA-FLEET-R036.2")
def test_only_changed_resources_import() -> None:
    first = _contract()
    assert contract_delta(None, first) == frozenset(first.keys())
    assert contract_delta(first, _contract(observed_at_ms=9)) == frozenset()
    changed = _contract(
        ontologies={"ontology://crm": "<ttl2>", "ontology://core": "<core>"}
    )
    assert contract_delta(first, changed) == {"ontology:ontology://crm"}
    assert contract_delta(first, _contract(seed=None)) == {"seed"}
    assert contract_delta(first, _contract(manifest="m2")) == {"manifest"}


@pytest.mark.spec("ARBORA-FLEET-R036.2")
def test_onboarding_records_the_contract_and_the_changed_keys() -> None:
    from types import SimpleNamespace

    from arbora.fleet.onboarding import FleetOnboarding

    def entry(uri: str, body: bytes) -> SimpleNamespace:
        return SimpleNamespace(uri=uri, body=body, kind="resource", name=uri)

    def pack(manifest: bytes) -> SimpleNamespace:
        entries = [
            entry("connector://resources", manifest),
            entry("ontology://crm", b"ttl"),
        ]
        return SimpleNamespace(archive=SimpleNamespace(entries=entries))

    host = SimpleNamespace(contracts={}, surfaces={"crm": {}})
    record = FleetOnboarding._record_contract
    record(host, "crm", pack(b"v1"))
    assert host.surfaces["crm"]["contract"]["changed"] == [
        "manifest",
        "ontology:ontology://crm",
    ]
    record(host, "crm", pack(b"v1"))
    assert host.surfaces["crm"]["contract"]["changed"] == []
    record(host, "crm", pack(b"v2"))
    assert host.surfaces["crm"]["contract"]["changed"] == ["manifest"]
