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
