"""Directory isolation for the overstory WebUi settings singleton."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_webui_settings() -> None:
    """Give every test a clean overstory ``WebUiSettings`` singleton.

    The overstory factory installs its remote-listener rate defaults
    directly into the process-wide settings singleton (for a non-loopback
    listener, ``config.gateway_rate_limit`` is assigned, not read). That
    assignment survives the test and would let a later test observe a
    policy an earlier one installed. ``reset()`` is the documented test
    hook that drops such process overrides before and after each test.
    """
    try:
        from overstory.host import config
    except ModuleNotFoundError:  # local venvs without the overstory extra
        yield
        return

    config.reset()
    yield
    config.reset()
