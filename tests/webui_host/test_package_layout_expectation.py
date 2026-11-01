"""Tests for the R013 overstory package layout expectation."""

from __future__ import annotations

import pytest

from arbora.webui_host.package_layout_expectation import (
    OVERSTORY_DISTRIBUTION,
    EXPECTED_TOP_LEVEL_MODULES,
    OverstoryLayoutError,
    InstalledPackageFixture,
    validate_overstory_layout,
)


def test_fixture_with_every_expected_module_passes() -> None:
    fixture = InstalledPackageFixture(
        distribution=OVERSTORY_DISTRIBUTION,
        top_level_modules=EXPECTED_TOP_LEVEL_MODULES,
    )
    validate_overstory_layout(fixture)


def test_fixture_missing_a_module_is_refused() -> None:
    fixture = InstalledPackageFixture(
        distribution=OVERSTORY_DISTRIBUTION,
        top_level_modules=tuple(
            module
            for module in EXPECTED_TOP_LEVEL_MODULES
            if module != "overstory.oidc_session"
        ),
    )
    with pytest.raises(OverstoryLayoutError):
        validate_overstory_layout(fixture)


def test_fixture_for_the_wrong_distribution_is_refused() -> None:
    fixture = InstalledPackageFixture(
        distribution="agent-web-ui",
        top_level_modules=EXPECTED_TOP_LEVEL_MODULES,
    )
    with pytest.raises(OverstoryLayoutError):
        validate_overstory_layout(fixture)
