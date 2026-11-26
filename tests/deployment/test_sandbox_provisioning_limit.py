"""A repeat ``up`` on a fresh store never meets the provisioning limit (R019.3)."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from arbora.deployment import sandbox
from arbora.mcp_server import composition


@pytest.mark.spec("ARBORA-DEPLOY-R019.3")
def test_repeat_up_on_a_fresh_store_is_not_rate_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores: list[str] = []

    def engine(config: object) -> SimpleNamespace:
        stores.append(str(config.persist_dir))  # type: ignore[attr-defined]
        assert not any(Path(config.persist_dir).iterdir())  # type: ignore[attr-defined]
        return SimpleNamespace(address=f"127.0.0.1:{config.port}")  # type: ignore[attr-defined]

    monkeypatch.setattr(composition, "ensure_local_engine", engine)
    monkeypatch.setattr(composition, "stand_down_engine", lambda endpoint: None)
    monkeypatch.setattr(sandbox, "resolve_engine", lambda *a, **k: "pinned")
    args = argparse.Namespace(
        root=str(tmp_path),
        principal="operator",
        second_tenant_principal=None,
        port=19999,
    )

    # Three ups inside one hour: each gets its own empty store, so the
    # per-target count starts at zero and no refusal can arise.
    for _ in range(3):
        sandbox.up(args, hold=False)
        assert sandbox.down(args) is True

    assert len(set(stores)) == 3
    assert not any(Path(s).exists() for s in stores)


@pytest.mark.spec("ARBORA-DEPLOY-R019.3")
def test_a_spent_budget_maps_to_provisioning_rate_limited_with_the_wait() -> None:
    from arbora.access.action_policy import (
        DENIAL_RATE_LIMITED,
        ActionRequest,
    )
    from arbora.api.contracts.provisioning import (
        ProvisioningRateLimited,
        _require_receipt,
    )
    from arbora.deployment.production_ops import refusal_detail

    decision = SimpleNamespace(
        denial=DENIAL_RATE_LIMITED,
        rate_max=3,
        rate_window_s=3600.0,
        retry_after_s=1200.0,
        allowed=False,
        receipt=None,
        reason="rate limit exceeded",
    )
    policy = SimpleNamespace(decide=lambda request: decision)
    request = ActionRequest(
        kind="connector_pack_import", target="arbora", params={}, source="arbora"
    )
    with pytest.raises(ProvisioningRateLimited) as refused:
        _require_receipt(policy, request)  # type: ignore[arg-type]

    detail = refusal_detail(refused.value)
    assert detail["error_code"] == "PROVISIONING_RATE_LIMITED"
    assert detail["limit"] == 3
    assert detail["window_seconds"] == 3600
    assert detail["retry_after_seconds"] == 1201


@pytest.mark.spec("ARBORA-DEPLOY-R019.3")
def test_the_wait_is_the_time_until_the_oldest_counted_run_leaves_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.access import action_policy
    from arbora.access.action_policy import ActionPolicy, ActionRequest

    monkeypatch.setattr(action_policy, "_now", lambda: 10_000.0)
    request = ActionRequest(kind="connector_pack_import", target="arbora")
    allowed = next(iter(action_policy._ALLOWING))
    recent = [
        {"target": "arbora", "decision": allowed, "ts": 10_000.0 - age}
        for age in (3000.0, 2000.0, 100.0)
    ] + [{"target": "other", "decision": allowed, "ts": 9_999.0}]
    limit = {"max": 3, "window_s": 3600}

    wait = ActionPolicy._rate_retry_after(None, request, limit, recent)  # type: ignore[arg-type]

    assert wait == pytest.approx(600.0)
