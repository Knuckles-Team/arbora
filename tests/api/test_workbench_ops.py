"""Remote workbench compatibility and admission (ARBORA-HOST-R032)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import workbench
from arbora.api.ops.registry_factory import get_registry

MANIFEST = {
    "editor_protocol": "acp-1",
    "editor_version": "1.4.0",
    "extensions": [
        {
            "extension_id": "overstory.context",
            "version_range": ">=1.0,<2.0",
            "capabilities": ["context"],
        }
    ],
    "keybinding_profiles": ["default"],
    "workspace_id": "ws-1",
    "session_id": "sess-1",
    "focus_rules": ["editor-owns-focus"],
    "reserved_shortcuts": ["ctrl+k"],
}


class Port:
    def __init__(self) -> None:
        self.allowed = {("tenant-a", "ws-1", "sess-1")}

    async def authorizes(self, *, tenant: str, workspace_id: str, session_id: str):
        return (tenant, workspace_id, session_id) in self.allowed

    async def manifest(self, *, tenant: str, workspace_id: str, session_id: str):
        return dict(MANIFEST)


def _context(port: Port | None = None, *, tenant: str = "tenant-a") -> Any:
    services = {} if port is None else {"workbench": port}
    return SimpleNamespace(
        caller=SimpleNamespace(session=object(), tenant=tenant), services=services
    )


def _op(op_id: str) -> Any:
    return get_registry()[op_id]


SCOPE = {"workspace_id": "ws-1", "session_id": "sess-1"}


@pytest.mark.spec("ARBORA-HOST-R032")
@pytest.mark.asyncio
async def test_compatibility_admission_and_cross_workspace_refusal() -> None:
    port = Port()
    result = await workbench.handle_workbench_compatibility(
        _context(port), SCOPE, _op("workbench.compatibility.get")
    )
    value = result["value"]
    assert value["extensions"][0]["extension_id"] == "overstory.context"
    assert value["reserved_shortcuts"] == ["ctrl+k"]
    assert value["schema_version"] == 1
    # No credential-shaped field is ever exposed.
    assert not any(
        word in json.dumps(value).lower() for word in ("token", "secret", "password")
    )

    admit = _op("workbench.extensions.admit")
    ok = await workbench.handle_workbench_admit(
        _context(port),
        {
            **SCOPE,
            "extension_id": "overstory.context",
            "version": "1.2.0",
            "editor_protocol": "acp-1",
        },
        admit,
    )
    assert ok["value"]["outcome"] == "admitted"

    for overrides, code in (
        ({"editor_protocol": "acp-9"}, workbench.WORKBENCH_PROTOCOL_INCOMPATIBLE),
        ({"version": "2.0.0"}, workbench.WORKBENCH_EXTENSION_INCOMPATIBLE),
        ({"extension_id": "unknown.ext"}, workbench.WORKBENCH_EXTENSION_INCOMPATIBLE),
        ({"version": "not-a-version"}, workbench.WORKBENCH_EXTENSION_INCOMPATIBLE),
    ):
        params = {
            **SCOPE,
            "extension_id": "overstory.context",
            "version": "1.2.0",
            "editor_protocol": "acp-1",
            **overrides,
        }
        with pytest.raises(OperationRefused) as refused:
            await workbench.handle_workbench_admit(_context(port), params, admit)
        assert refused.value.code == code

    # A foreign workspace answers WORKBENCH_WORKSPACE_FORBIDDEN, never the manifest.
    for other in (
        {"workspace_id": "ws-other", "session_id": "sess-1"},
        {"workspace_id": "ws-1", "session_id": "sess-other"},
    ):
        with pytest.raises(OperationRefused) as refused:
            await workbench.handle_workbench_compatibility(
                _context(port), other, _op("workbench.compatibility.get")
            )
        assert refused.value.code == workbench.WORKBENCH_WORKSPACE_FORBIDDEN
    with pytest.raises(OperationRefused) as refused:
        await workbench.handle_workbench_compatibility(
            _context(port, tenant="tenant-b"), SCOPE, _op("workbench.compatibility.get")
        )
    assert refused.value.code == workbench.WORKBENCH_WORKSPACE_FORBIDDEN


@pytest.mark.spec("ARBORA-HOST-R032")
@pytest.mark.asyncio
async def test_an_uncomposed_workbench_is_unavailable_not_defaulted() -> None:
    with pytest.raises(OperationRefused) as refused:
        await workbench.handle_workbench_compatibility(
            _context(None), SCOPE, _op("workbench.compatibility.get")
        )
    assert refused.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-HOST-R032")
def test_both_operations_are_registered_read_only_with_closed_models() -> None:
    registry = get_registry()
    for op_id in ("workbench.compatibility.get", "workbench.extensions.admit"):
        assert registry[op_id].effect.value == "read"
    with pytest.raises(ValueError):
        workbench.WorkbenchCompatibility.model_validate({**MANIFEST, "extra": 1})
    with pytest.raises(ValueError):
        workbench.ExtensionAdmission(
            extension_id="a",
            version="1",
            outcome="maybe",  # type: ignore[arg-type]
        )
