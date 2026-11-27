"""Federation sources served over HTTP and MCP against a loopback engine (ARBORA-OPS-R019).

The engine holds each source record with its owner and grantees and answers a source
the caller may not see exactly as an absent one. What is real here: the served app and
MCP surface, authentication, the invoke pipeline with its replay owner and audit, the
federation handlers and the composed engine-backed port. What is recorded: the engine.
"""

# ruff: noqa: F811

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from tests.api._loopback_engine import PRINCIPAL, Loopback, loopback  # noqa: F401
from tests.api._served_support import McpAsk, person
from tests.test_served_operation_api import served  # noqa: F401

STRANGER = "person:two"


class Catalog:
    """The engine's foreign-source catalog: owner, grantees and a credential."""

    def __init__(self) -> None:
        self.sources: dict[str, dict[str, Any]] = {
            "crm": {
                "name": "crm",
                "owner": PRINCIPAL,
                "grantees": [],
                "config": {"host": "db", "password": "hunter2"},
            }
        }

    def _visible(self, name: str, principal: str) -> dict[str, Any]:
        record = self.sources.get(name)
        if record is None or principal not in {record["owner"], *record["grantees"]}:
            raise EngineRefusal("SOURCE_NOT_FOUND")
        return record

    def get(self, params: dict[str, Any], principal: str) -> dict[str, Any]:
        return self._visible(params["name"], principal)

    def list(self, params: dict[str, Any], principal: str) -> dict[str, Any]:
        return {
            "sources": [
                r
                for r in self.sources.values()
                if principal in {r["owner"], *r["grantees"]}
            ]
        }

    def probe(self, params: dict[str, Any], principal: str) -> dict[str, Any]:
        record = self._visible(params["name"], principal)
        return {**record, "ok": True, "token": "t0ps3cret"}

    def share(self, params: dict[str, Any], principal: str) -> dict[str, Any]:
        record = self._visible(params["name"], principal)
        if record["owner"] != principal:
            raise EngineRefusal("SOURCE_NOT_FOUND")
        record["grantees"] = sorted({*record["grantees"], *params["grantees"]})
        return record

    def unshare(self, params: dict[str, Any], principal: str) -> dict[str, Any]:
        record = self._visible(params["name"], principal)
        if record["owner"] != principal:
            raise EngineRefusal("SOURCE_NOT_FOUND")
        record["grantees"] = [
            g for g in record["grantees"] if g not in params["grantees"]
        ]
        return record


def _serve(loop: Loopback, catalog: Catalog) -> None:
    from arbora.pin_lag import medulla

    loop.steps.journal_effects = True
    loop.serve_unreleased(
        medulla,
        {
            "GetForeignSource": catalog.get,
            "ListForeignSources": catalog.list,
            "ProbeForeignSource": catalog.probe,
            "ShareForeignSource": catalog.share,
            "UnshareForeignSource": catalog.unshare,
        },
    )


def _mcp(loop: Loopback, monkeypatch: pytest.MonkeyPatch, who: str) -> McpAsk:
    return McpAsk(
        loop.invocation,
        monkeypatch,
        person(who, extra_scopes=("federation:read", "federation:admin")),
    )


@pytest.mark.spec("ARBORA-OPS-R019.1")
def test_register_list_get_probe_redact_credentials(loopback: Loopback) -> None:
    catalog = Catalog()
    _serve(loopback, catalog)
    loopback.serve({}, "federation:read", "federation:admin")
    listed = Loopback.value(loopback.call("federation.sources.list", {}, "l-1"))
    assert [s["source_id"] for s in listed["sources"]] == ["crm"]
    got = Loopback.value(
        loopback.call("federation.sources.get", {"source_id": "crm"}, "g-1")
    )
    assert (
        got["config"]["password"].lower() == "[redacted]"
        and got["config"]["host"] == "db"
    )
    probed = loopback.call("federation.sources.probe", {"source_id": "crm"}, "p-1")
    assert Loopback.value(probed)["token"].lower() == "[redacted]"
    assert "hunter2" not in probed.text and "t0ps3cret" not in probed.text
    assert {m for m, *_ in loopback.engine_calls} >= {
        "ListForeignSources",
        "GetForeignSource",
        "ProbeForeignSource",
    }


@pytest.mark.spec("ARBORA-OPS-R019.2")
def test_only_owner_shares_and_revocation_blocks_probe(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = Catalog()
    _serve(loopback, catalog)
    loopback.serve({}, "federation:read", "federation:admin")
    stranger = _mcp(loopback, monkeypatch, "two")
    seen = stranger("find", "federation.sources.list", {})
    assert "crm" not in str(seen)
    # The stranger cannot share, and a refusal reads as an absent source.
    refused = stranger(
        "act",
        "federation.sources.share",
        {"source_id": "crm", "grantee": STRANGER},
        idempotency_key="s-0",
        execute=True,
    )
    assert "crm" not in str(refused).replace("source_id", "")
    assert catalog.sources["crm"]["grantees"] == []
    shared = loopback.call(
        "federation.sources.share", {"source_id": "crm", "grantee": STRANGER}, "s-1"
    )
    assert shared.status_code == 200, shared.text
    assert catalog.sources["crm"]["grantees"] == [STRANGER]
    seen = stranger("find", "federation.sources.list", {})
    assert "crm" in str(seen)
    revoked = loopback.call(
        "federation.sources.unshare", {"source_id": "crm", "grantee": STRANGER}, "u-1"
    )
    assert revoked.status_code == 200, revoked.text
    probe = stranger("ask", "federation.sources.probe", {"source_id": "crm"})
    assert "t0ps3cret" not in str(probe) and "SOURCE_NOT_FOUND" in str(probe)


@pytest.mark.spec("ARBORA-OPS-R019")
def test_share_probe_and_revoke_over_both_transports(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = Catalog()
    _serve(loopback, catalog)
    loopback.serve({}, "federation:read", "federation:admin")
    owner = _mcp(loopback, monkeypatch, "one")
    over_mcp = owner("ask", "federation.sources.get", {"source_id": "crm"})
    assert "[redacted]" in str(over_mcp).lower() and "hunter2" not in str(over_mcp)
    over_http = loopback.call("federation.sources.get", {"source_id": "crm"}, "g-2")
    assert over_http.status_code == 200 and "hunter2" not in over_http.text
    # The pin-lag guard names the missing engine method when the pin lacks it.
    from arbora.pin_lag.medulla import PinLagUnavailable, require_methods

    with pytest.raises(PinLagUnavailable):
        require_methods("NoSuchForeignSourceMethod")
