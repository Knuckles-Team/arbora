"""GRAPHOS-HOST-R004: a single gateway implementation under arbora/gateway/.

A package-layout test that fails closed if a second, duplicate gateway
package is ever added outside ``arbora/gateway/``. The retired
``arbora-daemon`` console script and ``gateway-widgets`` package extra are
tracked separately (status.json records them as still-present, staged
removals) and are out of scope for this structural layout check.
"""

from __future__ import annotations

from pathlib import Path

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
