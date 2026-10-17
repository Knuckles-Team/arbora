"""Shared fixtures for graph-os's backing-tool tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture
def graphos_harness_host() -> Iterator[None]:
    """graph-os registered as the harness host, as the served process does."""
    from graph_os.mcp_server.harness_host import (
        register_harness_host,
        unregister_harness_host,
    )

    register_harness_host()
    try:
        yield
    finally:
        unregister_harness_host()
