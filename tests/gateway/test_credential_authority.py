"""The credential authority builds a binding only from the exact current grant (ARBORA-OPS-R893)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

from arbora.gateway.credential_authority import CredentialAuthority, CredentialDenied
from arbora.pin_lag.unavailable import PinLagUnavailable
from tests.gateway._sdk_binding_double import PROFILE, decision, install

_ASK: dict[str, Any] = {
    "tenant": "tenant:one",
    "requesting_principal": "person:caller",
    "client": "client:one",
    "connector": "mail-agent",
    "operation": "send_mail",
    "resource": "mailbox:1",
    "protocol": "mcp",
    "profile": PROFILE,
    "scopes": ("read",),
}


class _Grants:
    """The engine's ``CheckResourceAccess``: one decision, recorded per call."""

    def __init__(self, answer: Mapping[str, Any] | None = None, fail: bool = False):
        self.answer, self.fail = answer, fail
        self.asked: list[tuple[str, ...]] = []

    async def decision(self, *key: str) -> Mapping[str, Any]:
        self.asked.append(key)
        if self.fail or self.answer is None:
            raise ConnectionError("engine down")
        return self.answer


class _Receipts:
    def __init__(self) -> None:
        self.rows: list[Mapping[str, Any]] = []

    async def record(self, receipt: Mapping[str, Any]) -> None:
        self.rows.append(receipt)


def _authority(
    records: _Grants, receipts: _Receipts, now: int = 1_000
) -> CredentialAuthority:
    return CredentialAuthority(records, receipts, lambda: now)


@pytest.mark.spec("ARBORA-OPS-R893")
async def test_exact_grant_only_and_refusals_write_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = install(monkeypatch)
    receipts = _Receipts()
    ok_grants = _Grants(decision())
    ok = _authority(ok_grants, receipts)
    async with ok.admitted(**_ASK) as binding:
        # The SDK port answers ok for the admitted binding and for no other.
        assert ok.verify(binding) == "ok"
        assert module.current_credential_binding() == binding
        assert binding.requesting_principal == "person:caller"
        other = binding.model_copy(update={"resource": "mailbox:2"})
        assert ok.verify(other) == "binding_mismatch"
    assert ok.verify(binding) == "binding_mismatch"
    assert ok_grants.asked == [
        ("tenant:one", "client:one", "mailbox:1", "mcp", "send_mail")
    ]
    cases = (
        (decision(allow=False, reason_code="revoked"), {}, "revoked"),
        (decision(allow=False, reason_code="grant_expired"), {}, "grant_expired"),
        (decision(expires_at_ms=1_000), {}, "grant_expired"),
        (decision(), {"scopes": ("admin",)}, "scope_denied"),
        (decision(allow=False, reason_code="no_grant"), {}, "binding_mismatch"),
    )
    for answer, ask, reason in cases:
        receipts.rows.clear()
        authority = _authority(_Grants(answer), receipts)
        with pytest.raises(CredentialDenied) as refused:
            async with authority.admitted(**{**_ASK, **ask}):
                raise AssertionError("not admitted")
        assert refused.value.reason == reason
        [row] = receipts.rows
        assert row["outcome"] == reason
        # The engine requires a ``sha256:<64 hex>`` digest on every receipt (R753).
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", row["binding_digest"])
        assert "vault_ref" not in row and "token" not in str(row).lower()
    receipts.rows.clear()
    down = _authority(_Grants(fail=True), receipts)
    with pytest.raises(CredentialDenied) as refused:
        async with down.admitted(**_ASK):
            raise AssertionError("not admitted")
    assert refused.value.reason == "authority_unavailable"
    assert [r["outcome"] for r in receipts.rows] == ["authority_unavailable"]


@pytest.mark.spec("ARBORA-OPS-R893")
async def test_an_admitted_call_writes_one_ok_receipt_with_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = install(monkeypatch)
    receipts = _Receipts()
    authority = _authority(_Grants(decision()), receipts)
    async with authority.admitted(**_ASK) as binding:
        pass
    [row] = receipts.rows
    assert row["outcome"] == "ok"
    assert row["binding_digest"] == "sha256:" + module.binding_digest(binding)


@pytest.mark.spec("ARBORA-OPS-R893")
async def test_a_pin_without_the_sdk_binding_refuses_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "scion_sdk.credentials.binding", None)
    authority = _authority(_Grants(decision()), _Receipts())
    with pytest.raises(PinLagUnavailable) as refused:
        async with authority.admitted(**_ASK):
            raise AssertionError("not admitted")
    assert (
        refused.value.missing == "scion_sdk.credentials.binding.CredentialAccessBinding"
    )
