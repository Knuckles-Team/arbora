"""Hook install and uninstall are registry operations (ARBORA-OPS-R879)."""

from __future__ import annotations

import pytest

from arbora.api.ops import ops
from arbora.api.registry import Effect
from tests.api._ops_support import service_context


@pytest.mark.spec("ARBORA-OPS-R879")
async def test_hooks_install_uninstall_over_http_and_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class _Installer:
        def install(self, agents=None):
            calls.append(("install", agents))
            return {"claude": "installed"}

        def uninstall(self, agents=None):
            calls.append(("uninstall", agents))
            return {"claude": "removed"}

    import arbora.ecosystem.hook_installer as installer

    monkeypatch.setattr(installer, "HookInstaller", _Installer)
    by_id = {o.id: o for o in ops.operations()}
    for op_id in ("ops.hooks.install", "ops.hooks.uninstall"):
        assert by_id[op_id].effect is Effect.ADMIN
        assert by_id[op_id].scopes == {"ops:admin"}
    context = service_context(tenant="t")
    up = await ops.hooks_install(
        context, {"agents": ["claude"]}, by_id["ops.hooks.install"]
    )
    down = await ops.hooks_uninstall(context, {}, by_id["ops.hooks.uninstall"])
    assert up["value"]["results"] == {"claude": "installed"}
    assert down["value"]["results"] == {"claude": "removed"}
    assert calls == [("install", ["claude"]), ("uninstall", None)]
