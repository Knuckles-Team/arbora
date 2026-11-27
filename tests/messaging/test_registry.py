"""ARBORA-HOST-R007.2.63: messaging backends and reach load from the ``arbora.messaging`` groups."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

from arbora.messaging import registry

_PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"


def _declared(group: str) -> dict[str, str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["entry-points"][group]


def _resolve(target: str) -> object:
    module, _, attribute = target.partition(":")
    return getattr(importlib.import_module(module), attribute)


@pytest.mark.spec("ARBORA-HOST-R007.2.63")
def test_backends_discovered_from_arbora_group() -> None:
    assert registry._ENTRY_POINT_GROUP == "arbora.messaging"
    backends = _declared("arbora.messaging")
    assert len(backends) >= 17
    for name, target in backends.items():
        assert isinstance(_resolve(target), type), name
    reach = _declared("arbora.messaging.reach")
    assert set(reach) == {"notify_sync", "service"}
    for name, target in reach.items():
        assert callable(_resolve(target)), name


@pytest.mark.spec("ARBORA-HOST-R007.2.63")
def test_no_entry_point_group_keeps_the_retired_name() -> None:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    groups = set(data["project"]["entry-points"])
    assert all(group.startswith("arbora.") for group in groups)
