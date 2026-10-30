"""ARBORA-HOST-R004: a single gateway implementation under arbora/gateway/.

A package-layout test that fails closed if a second, duplicate gateway
package is ever added outside ``arbora/gateway/``, and proves the retired
``arbora-daemon`` console script and ``gateway-widgets`` package extra are
absent from the published manifest.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ARBORA_ROOT = Path(__file__).resolve().parents[2] / "arbora"
GATEWAY_ROOT = ARBORA_ROOT / "gateway"


def _top_level_packages_named_like_gateway() -> set[Path]:
    return {
        path
        for path in ARBORA_ROOT.iterdir()
        if path.is_dir() and "gateway" in path.name.lower()
    }


def test_arbora_gateway_package_exists() -> None:
    assert GATEWAY_ROOT.is_dir()
    assert (GATEWAY_ROOT / "__init__.py").is_file()


def test_no_duplicate_top_level_gateway_package_exists() -> None:
    found = _top_level_packages_named_like_gateway()
    assert found == {GATEWAY_ROOT}, (
        f"expected arbora/gateway/ to be the sole gateway package, found: {found}"
    )


@pytest.mark.spec("ARBORA-HOST-R004")
def test_single_gateway_and_retired_script_and_extra_absent() -> None:
    assert _top_level_packages_named_like_gateway() == {GATEWAY_ROOT}
    project = tomllib.loads(
        (ARBORA_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert "arbora-daemon" not in project["scripts"]
    assert "gateway-widgets" not in project.get("optional-dependencies", {})
    import arbora.gateway.daemon as daemon

    assert not hasattr(daemon, "main")


@pytest.mark.spec("ARBORA-DEPLOY-R006")
def test_base_dependencies_pin_connector_sdk_floor_without_widgets_extra() -> None:
    from packaging.requirements import Requirement

    project = tomllib.loads(
        (ARBORA_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert "gateway-widgets" not in project.get("optional-dependencies", {})
    sdk = [
        Requirement(raw)
        for raw in project["dependencies"]
        if Requirement(raw).name == "scion-sdk"
    ]
    assert len(sdk) == 1 and sdk[0].url is None and len(sdk[0].specifier) > 0


@pytest.mark.spec("ARBORA-DEPLOY-R007.3")
def test_arbora_daemon_entry_point_retired() -> None:
    project = tomllib.loads(
        (ARBORA_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert "arbora-daemon" not in project["scripts"]
    import arbora.gateway.daemon as daemon

    assert not hasattr(daemon, "main")
