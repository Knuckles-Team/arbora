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
                kind="resource", name="n", uri="seed://identity", body=seed
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
            entries=(types.SimpleNamespace(uri="seed://identity", body=section),)
        )
    )
    with pytest.raises(ContractRefused):
        check_resource_sizes(pack, 4096)
    check_resource_sizes(pack, 8192)


def _seeded_pack() -> types.SimpleNamespace:
    entries = [
        types.SimpleNamespace(
            kind="resource", name="n", uri="connector://resources", body=b"m1"
        ),
        *(
            types.SimpleNamespace(
                kind="seed_part", name=key, uri=f"seed://{key}", body=b"<s> <p> <o> .\n"
            )
            for key in ("identity", "catalog", "references")
        ),
    ]
    return types.SimpleNamespace(
        connector="crm", archive=types.SimpleNamespace(entries=tuple(entries))
    )


def _install_pack_shapes(monkeypatch: pytest.MonkeyPatch, *, main: bool) -> None:
    """Structural doubles of ``PackEntryKind`` and ``PackImportReceipt``: pinned or ``main``."""
    import enum

    from medulla.generated import models
    from pydantic import BaseModel

    members = {"tool": "tool", "resource": "resource"}
    if main:
        members["seed_part"] = "seed_part"
    kind = enum.Enum("PackEntryKind", members, type=str)

    class Receipt(BaseModel):
        connector: str

    class MainReceipt(Receipt):
        seed: Any | None = None

    monkeypatch.setattr(models, "PackEntryKind", kind, raising=False)
    monkeypatch.setattr(
        models, "PackImportReceipt", MainReceipt if main else Receipt, raising=False
    )


@pytest.mark.spec("ARBORA-FLEET-R054")
@pytest.mark.asyncio
async def test_seed_parts_refuse_unavailable_on_the_pinned_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pack with seed parts is never imported with its seed dropped (pin-lag)."""
    from arbora.deployment.semantic_provisioning import import_attested_pack
    from arbora.pin_lag.medulla import PinLagUnavailable, require_seed_parts

    _install_pack_shapes(monkeypatch, main=False)
    with pytest.raises(PinLagUnavailable, match="PackEntryKind.seed_part"):
        require_seed_parts()
    with pytest.raises(PinLagUnavailable, match="seed_part"):
        await import_attested_pack(
            object(),
            object(),
            client=object(),
            commons=object(),
            pack=_seeded_pack(),
            action_policies=object(),
        )


@pytest.mark.spec("ARBORA-FLEET-R054")
@pytest.mark.asyncio
async def test_seed_parts_reach_pack_import_on_the_main_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole pack, seed parts included, goes to the engine; its seed digest is read back."""
    import contextlib

    import scion_sdk.sinks.medulla as sink_module

    import arbora.api.contracts as contracts
    from arbora.deployment.semantic_provisioning import (
        SemanticProvisioningError,
        import_attested_pack,
        seed_content_digest,
        seed_part_entries,
    )

    _install_pack_shapes(monkeypatch, main=True)
    sent: list[Any] = []
    refusal: list[str] = []

    class Sink:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def import_pack(self, pack: Any, **kwargs: Any) -> Any:
            sent.append((pack, kwargs))
            if refusal:
                return types.SimpleNamespace(
                    result="rejected",
                    violations=[
                        types.SimpleNamespace(
                            code=types.SimpleNamespace(value=refusal[0]),
                            uri="seed://catalog",
                            detail="triple",
                        )
                    ],
                )
            return types.SimpleNamespace(
                result="imported",
                receipt=types.SimpleNamespace(
                    seed=types.SimpleNamespace(content_digest="d" * 64)
                ),
            )

    async def attested(*args: Any, **kwargs: Any) -> str:
        return "binding"

    async def owner(*args: Any, **kwargs: Any) -> str:
        return "owner"

    monkeypatch.setattr(sink_module, "MedullaSink", Sink)
    monkeypatch.setattr(contracts, "pack_import_authority", lambda *a, **k: object())
    monkeypatch.setattr(semantic_provisioning, "attest_self_served_catalog", attested)
    monkeypatch.setattr(semantic_provisioning, "_owner_principal", owner)
    monkeypatch.setattr(
        semantic_provisioning, "session_context", lambda session: {"tenant": "t"}
    )
    session = types.SimpleNamespace(graph="g", with_graph=lambda graph: session)
    pack = _seeded_pack()
    assert [str(e.uri) for e in seed_part_entries(pack)] == [
        "seed://identity",
        "seed://catalog",
        "seed://references",
    ]

    def run() -> Any:
        return import_attested_pack(
            object(),
            session,
            client=types.SimpleNamespace(),
            commons=object(),
            pack=pack,
            action_policies=object(),
            bind_graph=lambda graph: contextlib.nullcontext(),
        )

    result = await run()
    assert sent[0][0] is pack and "seed" not in sent[0][1]
    assert seed_content_digest(result) == "d" * 64
    for code in (
        "SEED_TOO_LARGE",
        "SEED_BULK_DATA",
        "SEED_SHACL_VIOLATION",
        "SEED_DIGEST_MISMATCH",
    ):
        refusal[:] = [code]
        with pytest.raises(SemanticProvisioningError, match=code):
            await run()


@pytest.mark.spec("ARBORA-FLEET-R054")
@pytest.mark.asyncio
async def test_onboarding_records_the_engine_seed_digest(monkeypatch) -> None:
    """The contract surface carries the engine's ``seed.content_digest`` from the receipt."""
    result = types.SimpleNamespace(
        result="imported",
        receipt=types.SimpleNamespace(
            seed=types.SimpleNamespace(content_digest="e" * 64)
        ),
    )
    onboarding, _ = make_onboarding([_seeded_pack()], monkeypatch, result=result)
    await onboarding.onboard(ENDPOINT, auth=None)
    contract = onboarding.surfaces["crm"]["contract"]
    assert contract["seed"] == "present"
    assert contract["seed_content_digest"] == "e" * 64
