"""Arbora's typed expectation of the ``overstory`` package layout.

RF-ADR-009 host-composition-boundary R013 ("Front ends import only public
Arbora/agent-runtime surfaces") requires a package-layout check on the
web UI package. This module is Arbora's side of that check: a typed
expectation of which top-level ``overstory`` modules Arbora imports
from (``browser_control``, ``identity`` and ``webui_host`` wiring), and a
validator that refuses an installed-package fixture missing any of them.

The validator is exercised against an *installed-package fixture* here --
a stand-in for ``importlib.metadata``/module-inventory data -- never
against the real ``overstory`` install, which belongs to that package's
own test suite.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The published distribution name Arbora depends on.
OVERSTORY_DISTRIBUTION = "overstory"

#: The top-level ``overstory`` modules Arbora imports from, per the
#: current import sites in ``arbora/browser_control``,
#: ``arbora/identity`` and ``arbora/webui_host``.
EXPECTED_TOP_LEVEL_MODULES: tuple[str, ...] = (
    "overstory.api_extensions",
    "overstory.browser_control",
    "overstory.oidc_session",
    "overstory.orchestrator_model",
    "overstory.server",
)


class OverstoryLayoutError(ValueError):
    """Raised when an installed-package fixture does not satisfy Arbora's layout expectation."""


@dataclass(frozen=True)
class InstalledPackageFixture:
    """A fixture standing in for an installed package's module inventory.

    This is deliberately not the real ``overstory`` install: it lets
    Arbora's layout expectation be exercised, including its refusal
    path, without depending on that package being installed.
    """

    distribution: str
    top_level_modules: tuple[str, ...]


def validate_overstory_layout(fixture: InstalledPackageFixture) -> None:
    """Refuse a fixture for the wrong distribution or missing an expected module."""

    if fixture.distribution != OVERSTORY_DISTRIBUTION:
        raise OverstoryLayoutError(
            f"fixture is for distribution {fixture.distribution!r}, "
            f"not {OVERSTORY_DISTRIBUTION!r}"
        )

    missing = [
        module
        for module in EXPECTED_TOP_LEVEL_MODULES
        if module not in fixture.top_level_modules
    ]
    if missing:
        raise OverstoryLayoutError(
            f"installed overstory package fixture is missing modules "
            f"Arbora imports: {missing}"
        )
