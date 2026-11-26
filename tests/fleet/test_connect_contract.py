"""The connector contract record and its delta (ARBORA-FLEET-R036.1, R036.2)."""

from __future__ import annotations

from typing import Any

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


# --- R036.3 to R036.5: the onboarding wiring ---------------------------------

import types  # noqa: E402

from arbora.deployment import semantic_provisioning  # noqa: E402
from arbora.fleet.connect_contract import ContractRefused  # noqa: E402
from arbora.fleet.declarations import Declarations  # noqa: E402
from arbora.fleet.onboarding import FleetEndpoint, FleetOnboarding  # noqa: E402


def contract_pack(seed: bytes | None = b"seed-v1", manifest: bytes = b"m1"):
    entries = [
        types.SimpleNamespace(
            kind="resource", name="n", uri="connector://resources", body=manifest
        ),
        types.SimpleNamespace(
            kind="resource", name="n", uri="ontology:crm", body=b"<ttl>"
        ),
    ]
    if seed is not None:
        entries.append(
            types.SimpleNamespace(
                kind="resource", name="n", uri="connector://seed", body=seed
            )
        )
    return types.SimpleNamespace(
        connector="crm", archive=types.SimpleNamespace(entries=tuple(entries))
    )


class RecordingOnboarding(FleetOnboarding):
    """The real onboarding pass with the engine-bound steps replaced by doubles."""

    async def pack_unchanged(self, endpoint, pack):
        return False

    def register_contracts(self, endpoint, pack):
        return ()

    async def approve_contracts(self, endpoint, pack, decisions):
        return None

    async def write_catalog(self, endpoint, pack):
        return None

    async def projection_state(self, connector):
        return ("applied", "")

    async def reproject(self, connector):
        return None


def make_onboarding(packs, monkeypatch, *, result=None, error=None):
    from arbora.access.action_policy import ActionPolicies

    imports: list[Any] = []

    async def fake_import(*args, **kwargs):
        imports.append(kwargs["pack"])
        if error is not None:
            raise error
        return result

    async def capture(endpoint, *, auth):
        return packs.pop(0)

    monkeypatch.setattr(semantic_provisioning, "import_attested_pack", fake_import)
    monkeypatch.setattr(
        "arbora.fleet.onboarding._remember_declaration", lambda *args, **kw: None
    )
    engine = types.SimpleNamespace(
        graph_compute=types.SimpleNamespace(
            for_graph=lambda name: types.SimpleNamespace(async_client=object())
        )
    )
    onboarding = RecordingOnboarding(
        engine=engine,
        session=types.SimpleNamespace(graph="g"),
        declarations=Declarations(),
        action_policies=ActionPolicies(),
        capture=capture,
    )
    return onboarding, imports


ENDPOINT = FleetEndpoint("crm", "http://crm/mcp")


@pytest.mark.spec("ARBORA-FLEET-R036.3")
@pytest.mark.asyncio
async def test_list_changed_reaches_onboard(monkeypatch) -> None:
    packs = [contract_pack(), contract_pack(seed=b"seed-v2")]
    onboarding, imports = make_onboarding(packs, monkeypatch)
    await onboarding.onboard(ENDPOINT, auth=None)
    first = onboarding.surfaces["crm"]["contract"]
    assert "seed" in first["changed"] and first["seed"] == "present"
    # a second onboard, as a list_changed notification drives it, updates the record
    await onboarding.onboard(ENDPOINT, auth=None)
    second = onboarding.surfaces["crm"]["contract"]
    assert second["changed"] == ["seed"]
    assert len(imports) == 2


@pytest.mark.spec("ARBORA-FLEET-R036.4")
@pytest.mark.asyncio
async def test_contract_refusals(monkeypatch) -> None:
    from arbora.deployment.semantic_provisioning import SemanticProvisioningError

    onboarding, imports = make_onboarding([contract_pack(seed=None)], monkeypatch)
    await onboarding.onboard(ENDPOINT, auth=None)
    assert onboarding.surfaces["crm"]["contract"]["seed"] == "seed_absent"

    monkeypatch.setenv("ARBORA_CONTRACT_RESOURCE_MAX_BYTES", "1024")
    from arbora.core.settings import load_config

    load_config(reload=True)
    big = contract_pack(seed=b"x" * 2048)
    onboarding, imports = make_onboarding([big], monkeypatch)
    with pytest.raises(ContractRefused) as refused:
        await onboarding.onboard(ENDPOINT, auth=None)
    assert refused.value.code == "CONTRACT_RESOURCE_TOO_LARGE"
    assert imports == []
    assert onboarding.surfaces["crm"]["contract"]["outcome"] == "contract_rejected"

    for code in ("ONTOLOGY_INVALID", "SHACL_VIOLATION", "SEED_SHACL_VIOLATION"):
        failure = SemanticProvisioningError(f"import was rejected: {code}:u:d")
        onboarding, _ = make_onboarding([contract_pack()], monkeypatch, error=failure)
        with pytest.raises(SemanticProvisioningError):
            await onboarding.onboard(ENDPOINT, auth=None)
        contract = onboarding.surfaces["crm"]["contract"]
        assert contract["outcome"] == "contract_rejected" and contract["reason"] == code


@pytest.mark.spec("ARBORA-FLEET-R036.5")
@pytest.mark.asyncio
async def test_status_reports_contract(monkeypatch) -> None:
    result = types.SimpleNamespace(
        result="committed", pack_digest="sha256:" + "a" * 64, binding_revision=7
    )
    onboarding, _ = make_onboarding([contract_pack()], monkeypatch, result=result)
    await onboarding.onboard(ENDPOINT, auth=None)
    contract = onboarding.surfaces["crm"]["contract"]
    assert contract["manifest_digest"] and contract["ontology_digests"]
    assert contract["import_receipt"] == {
        "result": "committed",
        "pack_digest": "sha256:" + "a" * 64,
        "binding_revision": 7,
    }


@pytest.mark.spec("ARBORA-FLEET-R036.4")
def test_section_length_bounds_the_resource() -> None:
    from arbora.fleet.connect_contract import check_resource_sizes

    section = types.SimpleNamespace(offset=0, length=5000, sha256="0" * 64)
    pack = types.SimpleNamespace(
        archive=types.SimpleNamespace(
            entries=(types.SimpleNamespace(uri="connector://seed", body=section),)
        )
    )
    with pytest.raises(ContractRefused):
        check_resource_sizes(pack, 4096)
    check_resource_sizes(pack, 8192)
