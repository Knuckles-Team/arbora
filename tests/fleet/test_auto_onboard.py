# ruff: noqa: F811
"""Every onboarding trigger reaches the one onboard function (ARBORA-FLEET-R041.3)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.fleet import onboarding
from arbora.fleet import onboarding as onboarding_module
from arbora.fleet.onboarding import (
    FleetEndpoint,
    FleetOnboarding,
    FleetOnboardingService,
    OnboardingTrigger,
    onboard_fleet,
)
from tests.fleet.test_fleet_onboarding import (
    ALPHA,
    _Mux,
    _onboarding,
    _Registry,
    _run_in_session,
    eg,  # noqa: F401
)


@pytest.mark.spec("ARBORA-FLEET-R041.3")
def test_every_trigger_reaches_the_one_onboard_function(
    eg: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``onboard_fleet``, ``run_pass`` and the handshake pass share one ``onboard``."""

    calls: list[tuple[str, str]] = []

    async def recording_onboard(
        _self: FleetOnboarding,
        endpoint: FleetEndpoint,
        *,
        auth: Any,
        trigger: OnboardingTrigger | None = None,
    ) -> bool:
        calls.append((endpoint.name, trigger.reason if trigger else "default"))
        return False

    monkeypatch.setattr(FleetOnboarding, "onboard", recording_onboard)
    monkeypatch.setattr(
        onboarding, "configured_fleet_endpoints", lambda **_kwargs: (ALPHA,)
    )

    async def drive() -> None:
        await onboard_fleet(_onboarding(eg), (ALPHA,))
        await FleetOnboardingService(
            onboarding=_onboarding(eg), multiplexer=_Mux({})
        ).run_pass()
        for reason in ("handshake", "list_changed"):
            await _onboarding(eg).handshake_pass(
                ALPHA,
                auth=None,
                trigger=OnboardingTrigger(ALPHA.name, reason, f"digest-{reason}"),
                server_version="1",
            )

    _run_in_session(drive())

    assert calls == [
        (ALPHA.name, "default"),
        (ALPHA.name, "default"),
        (ALPHA.name, "handshake"),
        (ALPHA.name, "list_changed"),
    ]


# --- R041.1 to R041.6, R041.19, R041.20: the onboarding record --------------

import types  # noqa: E402

from arbora.fleet.onboarding import (  # noqa: E402
    PackSurfaceCounts,
    ProjectionFailed,
    _onboard_all,
)
from arbora.fleet.surfaces import pack_surface_report  # noqa: E402
from tests.fleet.test_connect_contract import (  # noqa: E402
    ENDPOINT,
    RecordingOnboarding,
    contract_pack,
    make_onboarding,
)


def _counted_pack(release: bytes = b"m1", prompts: int = 1):
    pack = contract_pack(manifest=release)
    entries = list(pack.archive.entries)
    kinds = [
        ("tool", "t1"),
        ("tool", "t2"),
        *[("prompt", f"p{i}") for i in range(prompts)],
    ]
    for kind, name in kinds:
        entries.append(types.SimpleNamespace(kind=kind, name=name, uri="", body=b""))
    pack.archive.entries = tuple(entries)
    return pack


@pytest.mark.spec("ARBORA-FLEET-R041.1")
def test_trigger_and_counts_round_trip() -> None:
    trigger = OnboardingTrigger("crm", "release_changed", "d1")
    assert (trigger.connector, trigger.reason, trigger.release_digest) == (
        "crm",
        "release_changed",
        "d1",
    )
    counts = PackSurfaceCounts.build(
        pack_surface_report(_counted_pack()),
        {
            "manifest_digest": "a" * 64,
            "ontology_digests": {"o": "b" * 64},
            "seed_digest": "",
        },
    )
    assert (counts.tools, counts.prompts, counts.contracts) == (2, 1, 2)
    assert counts.contract_state == "imported"
    with pytest.raises(AttributeError):
        counts.tools = 3  # type: ignore[misc]


@pytest.mark.spec("ARBORA-FLEET-R041.2")
@pytest.mark.asyncio
async def test_registration_and_release_start_one_pass_each(monkeypatch) -> None:
    packs = [_counted_pack(), _counted_pack(release=b"m2")]
    onboarding, imports = make_onboarding(packs, monkeypatch)
    first = await onboarding.onboard(
        ENDPOINT, auth=None, trigger=OnboardingTrigger("crm", "registered")
    )
    second = await onboarding.onboard(
        ENDPOINT, auth=None, trigger=OnboardingTrigger("crm", "release_changed", "d2")
    )
    assert first and second and len(imports) == 2
    assert onboarding.surfaces["crm"]["contract"]["changed"] == ["manifest"]

    renewed: list[str] = []

    class Unchanged(RecordingOnboarding):
        async def pack_unchanged(self, endpoint, pack):
            return True

        async def renew(self, endpoints, *, now_ms=None):
            renewed.extend(endpoints)
            return ()

    same, imports = make_onboarding([_counted_pack()], monkeypatch)
    quiet = Unchanged(
        **{
            f: getattr(same, f)
            for f in ("engine", "session", "declarations", "action_policies", "capture")
        }
    )
    assert await quiet.onboard(ENDPOINT, auth=None) is False
    assert renewed == ["crm"] and imports == []


@pytest.mark.spec("ARBORA-FLEET-R041.4")
@pytest.mark.asyncio
async def test_orphan_refusal_marks_contract_rejected_and_admits_no_tools(
    monkeypatch,
) -> None:
    from arbora.semantic_provisioning import SemanticProvisioningError

    failure = SemanticProvisioningError("rejected: ONTOLOGY_INVALID:u:ONTOLOGY_ORPHAN")
    onboarding, _ = make_onboarding([_counted_pack()], monkeypatch, error=failure)
    with pytest.raises(SemanticProvisioningError):
        await onboarding.onboard(ENDPOINT, auth=None)
    record = onboarding.surfaces["crm"]
    assert record["onboarding_state"] == "contract_rejected"
    assert record["cause"] == "ONTOLOGY_INVALID"
    assert record["counts"]["contract_state"] == "contract_rejected"
    assert "surface_gap" in record
    # another connector in the same pass is unaffected
    report = types.SimpleNamespace(failed={}, onboarded=[], renewed=[], surfaces={})
    other = FleetEndpoint("other", "http://o/mcp")
    onboarding2, _ = make_onboarding([_counted_pack(), _counted_pack()], monkeypatch)
    monkeypatch.setattr(onboarding_module, "_child_auth", lambda *_: None)
    await _onboard_all(onboarding2, [ENDPOINT, other], report)
    assert report.onboarded == ["crm", "other"] and report.failed == {}


@pytest.mark.spec("ARBORA-FLEET-R041.5")
@pytest.mark.asyncio
async def test_status_reports_counts_digests_receipt_and_reason(monkeypatch) -> None:
    result = types.SimpleNamespace(
        result="committed", pack_digest="sha256:" + "b" * 64, binding_revision=3
    )
    onboarding, _ = make_onboarding([_counted_pack()], monkeypatch, result=result)
    await onboarding.onboard(
        ENDPOINT, auth=None, trigger=OnboardingTrigger("crm", "list_changed", "d9")
    )
    record = onboarding.surfaces["crm"]
    assert record["counts"]["tools"] == 2 and record["counts"]["prompts"] == 1
    assert record["trigger"] == {"reason": "list_changed", "release_digest": "d9"}
    assert record["contract"]["manifest_digest"]
    assert record["contract"]["import_receipt"]["binding_revision"] == 3
    assert record["onboarding_state"] == "onboarded"


class ProjectionOnboarding(RecordingOnboarding):
    states: list[tuple[str, str]]
    reprojects: list[str]

    async def projection_state(self, connector):
        return self.states.pop(0)

    async def reproject(self, connector):
        self.reprojects.append(connector)


@pytest.mark.spec("ARBORA-FLEET-R041.19")
@pytest.mark.asyncio
async def test_commit_then_projection_failure_names_code_and_graph_and_reprojects_once(
    monkeypatch,
) -> None:
    base, _ = make_onboarding([_counted_pack()], monkeypatch)
    fields = {
        f: getattr(base, f)
        for f in ("engine", "session", "declarations", "action_policies", "capture")
    }
    onboarding = ProjectionOnboarding(**fields)
    ProjectionOnboarding.states = [
        ("failed", "PROJECTION_BUDGET"),
        ("failed", "PROJECTION_BUDGET"),
    ]
    ProjectionOnboarding.reprojects = []
    with pytest.raises(ProjectionFailed) as failed:
        await onboarding.onboard(ENDPOINT, auth=None)
    assert failed.value.code == "PROJECTION_BUDGET"
    record = onboarding.surfaces["crm"]
    assert record["onboarding_state"] == "imported_projection_failed"
    assert record["projection"] == {
        "connector": "crm",
        "code": "PROJECTION_BUDGET",
        "graph_name": "g",
    }
    assert ProjectionOnboarding.reprojects == ["crm"]
    # a retryable failure that the one reproject cures is a normal import
    ProjectionOnboarding.states = [("failed", "X"), ("applied", "")]
    ProjectionOnboarding.reprojects = []
    base, _ = make_onboarding([_counted_pack()], monkeypatch)
    healed = ProjectionOnboarding(**{f: getattr(base, f) for f in fields})
    await healed.onboard(ENDPOINT, auth=None)
    assert ProjectionOnboarding.reprojects == ["crm"]


@pytest.mark.spec("ARBORA-FLEET-R041.20")
@pytest.mark.asyncio
async def test_pass_longer_than_authority_lifetime_renews_between_imports(
    monkeypatch,
) -> None:
    class Authority:
        def __init__(self) -> None:
            self.renewals = 0
            self.near = False

        def near_expiry(self) -> bool:
            return self.near

        async def renew(self) -> None:
            self.renewals += 1
            self.near = False

    authority = Authority()
    base, imports = make_onboarding([_counted_pack(), _counted_pack()], monkeypatch)
    fields = {
        f: getattr(base, f)
        for f in ("engine", "session", "declarations", "action_policies", "capture")
    }
    onboarding = RecordingOnboarding(**fields, authority=authority)
    report = types.SimpleNamespace(failed={}, onboarded=[], renewed=[], surfaces={})
    monkeypatch.setattr(onboarding_module, "_child_auth", lambda *_: None)
    other = FleetEndpoint("other", "http://o/mcp")

    original = onboarding_module.FleetOnboarding.onboard

    async def aging(self, endpoint, **kw):
        result = await original(self, endpoint, **kw)
        authority.near = True  # the lifetime runs out during this import
        return result

    monkeypatch.setattr(RecordingOnboarding, "onboard", aging)
    await _onboard_all(onboarding, [ENDPOINT, other], report)
    assert authority.renewals == 1 and report.failed == {}
    assert len(imports) == 2
