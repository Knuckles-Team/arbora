"""ARBORA-HOST-R007.2.35: child policies load from the ``arbora.mcp_child_policies`` group."""

from __future__ import annotations

import importlib.metadata

import pytest

from arbora.fleet import child_runtime_policy as _multiplexer_child_runtime_policy


class _FakePolicyFactory:
    pass


@pytest.mark.spec("ARBORA-HOST-R007.2.35")
def test_policies_load_from_arbora_group(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, str]] = []

    class _EntryPoint:
        def load(self) -> type:
            return _FakePolicyFactory

    def entry_points(*, group: str, name: str) -> tuple[_EntryPoint, ...]:
        asked.append((group, name))
        return (_EntryPoint(),) if group == "arbora.mcp_child_policies" else ()

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    assert (
        _multiplexer_child_runtime_policy._RUNTIME_CHILD_POLICY_GROUP
        == "arbora.mcp_child_policies"
    )
    assert (
        _multiplexer_child_runtime_policy._load_runtime_child_policy_factory("fake")
        is _FakePolicyFactory
    )
    assert asked == [("arbora.mcp_child_policies", "fake")]


@pytest.mark.spec("ARBORA-HOST-R007.2.35")
def test_an_absent_policy_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: ())
    with pytest.raises(RuntimeError, match="unavailable"):
        _multiplexer_child_runtime_policy._load_runtime_child_policy_factory("missing")
