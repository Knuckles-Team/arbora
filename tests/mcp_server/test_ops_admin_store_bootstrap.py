"""The served bootstrap binds the operational-admin store (ARBORA-OPS-R024.5).

``ops.config``/``ops.tenants`` read Arbora's own ``ARBORA_CONFIG_DIR``;
``ops.doctor``/``ops.backup``/``ops.restore`` reach the engine's
``Health``/``Backup``/``Restore`` methods, faked at ``send_by_id``.  The
console step-up, fresh-MFA and audit gate on backup/restore is proven through
the invocation pipeline in ``tests/api/test_ops_admin_ops.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops import ops as ops_admin
from arbora.api.registry import Effect, PrincipalRule
from arbora.mcp_server.ops_admin_store import REDACTED, ArboraOpsAdminStore
from arbora.mcp_server.served_invocation import _with_ops_admin_store

_OPS = {op.id: op for op in ops_admin.operations()}


class _Pool:
    async def connected_client(self, context: Any, graph: str) -> Any:
        return SimpleNamespace(graph=graph)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], Any]]:
    calls: list[tuple[str, dict[str, Any], Any]] = []

    async def fake(client: Any, method: str, params: Any, graph: Any, **kw: Any):
        calls.append((method, dict(params), kw.get("idempotency_key")))
        if method == "Health":
            return {"status": "ok", "uptime_s": 5, "version": "2.27.10"}
        if method == "Backup":
            return {"shards": 4, "graph_scopes": 2}
        if method == "Restore":
            return {"stage_ref": "stage-1", "restored_shards": params["target_shards"]}
        raise AssertionError(method)

    monkeypatch.setattr("epistemic_graph.generated.send_by_id", fake)
    return calls


@pytest.fixture
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"APP_PROFILE": "prod", "GRAPH_SERVICE_TOKEN": "openbao://kv/arbora#t"}
        )
    )
    (tmp_path / "tenants.json").write_text(
        json.dumps({"tenants": ["t1", "t2", "t3"], "admin_tenants": ["t1"]})
    )
    return tmp_path


def _served() -> dict[str, Any]:
    bound = _with_ops_admin_store({}, _Pool(), "tenant-graph", lambda t: t)
    assert isinstance(bound["ops_admin_store"], ArboraOpsAdminStore)
    return bound


def _ctx(services: Any, tenant: str = "t1") -> Any:
    caller = SimpleNamespace(tenant=tenant, principal="operator")
    return SimpleNamespace(caller=caller, services=services, idempotency_key="k1")


def test_unbound_without_composition_is_not_bound() -> None:
    assert _with_ops_admin_store(None, _Pool(), "g", lambda t: t) is None


@pytest.mark.spec("ARBORA-OPS-R024.5")
async def test_admin_ops_are_served_from_arbora_config_and_engine(
    sent: Any, config_root: Path
) -> None:
    for op_id in ("ops.backup", "ops.restore"):
        op = _OPS[op_id]
        assert op.effect is Effect.ADMIN
        assert op.principals is PrincipalRule.HUMAN_UNDELEGATED
        assert op.audit.value == "event"
    served = _served()
    doctor = await ops_admin.handle_ops_doctor(_ctx(served), {}, _OPS["ops.doctor"])
    assert doctor["value"]["status"] == "ok"
    assert doctor["value"]["checks"]["config_dir"]["present"] is True
    config = await ops_admin.handle_ops_config(_ctx(served), {}, _OPS["ops.config"])
    assert config["value"]["settings"] == {
        "APP_PROFILE": "prod",
        "GRAPH_SERVICE_TOKEN": REDACTED,
    }
    admin = await ops_admin.handle_ops_tenants(
        _ctx(served), {"limit": 2}, _OPS["ops.tenants"]
    )
    assert admin["value"] == {"tenants": ["t1", "t2"], "next_cursor": "t2"}
    other = await ops_admin.handle_ops_tenants(
        _ctx(served, "t2"), {"limit": 50}, _OPS["ops.tenants"]
    )
    assert other["value"] == {"tenants": ["t2"], "next_cursor": None}
    backup = await ops_admin.handle_ops_backup(
        _ctx(served), {"destination": "nightly", "label": "n"}, _OPS["ops.backup"]
    )
    assert backup["value"]["backup_id"] == "nightly"
    assert backup["value"]["engine_receipt"]["shards"] == 4
    restore = await ops_admin.handle_ops_restore(
        _ctx(served), {"backup_id": "nightly", "target_shards": 8}, _OPS["ops.restore"]
    )
    assert restore["value"]["engine_receipt"]["stage_ref"] == "stage-1"
    assert sent == [
        ("Health", {}, None),
        ("Backup", {"destination": "nightly", "label": "n"}, "k1"),
        ("Restore", {"source": "nightly", "target_shards": 8}, "k1"),
    ]


def test_backup_destination_is_a_bundle_name_not_a_path() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ops_admin.OpsBackupParams(destination="../etc/passwd")
    with pytest.raises(ValidationError):
        ops_admin.OpsRestoreParams(backup_id="nightly", target_shards=0)
