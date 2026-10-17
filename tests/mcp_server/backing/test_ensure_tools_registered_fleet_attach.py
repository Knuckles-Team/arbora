"""AU-CONTROL-R031: ``ensure_tools_registered()``'s headless build attaches
AU's own fleet loader, or records a precise, surfaced refusal when it cannot.

Before this fix, any process whose *only* build path was
``ensure_tools_registered()`` (a gateway-only process, e.g. standalone
agent-webui) got a ``mcp`` with no fleet multiplexer ever attached, so every
``find``/``act`` fleet call raised a generic "embedded/headless build"
error forever — even though AU already owns a complete, working fleet-attach
implementation (:func:`graph_os.fleet.multiplexer.attach_fleet_loader`),
used with these exact arguments by this module's own full composed server.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import ANY, patch

import pytest

from graph_os.fleet.catalog_reader import DeferredFleetCatalogReader
from graph_os.mcp_server import runtime
from graph_os.mcp_server import server as graphos_server
from graph_os.mcp_server.backing.tools.intent_tools import _fleet_mux


def test_attach_headless_fleet_loader_calls_attach_fleet_loader_with_composed_args() -> (
    None
):
    """Same arguments the served process passes in ``server._attach_fleet_runtime``."""
    mcp = SimpleNamespace()
    sentinel_embed = object()
    with (
        patch.object(graphos_server, "_fleet_embed_fn", return_value=sentinel_embed),
        patch("graph_os.fleet.multiplexer.attach_fleet_loader") as mock_attach,
    ):
        runtime._attach_headless_fleet_loader(mcp)

    mock_attach.assert_called_once_with(
        mcp,
        catalog_reader=ANY,
        embed_fn=sentinel_embed,
        authority_scope=runtime.verified_tool_session_scope,
    )
    assert isinstance(
        mock_attach.call_args.kwargs["catalog_reader"], DeferredFleetCatalogReader
    )
    assert not hasattr(mcp, "_fleet_mux_unavailable_reason")


def test_attach_headless_fleet_loader_records_the_exact_failure_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No silent fallback: a failed attach leaves ``mcp`` unattached and
    records why, rather than a degraded or empty multiplexer.

    The exact cause (the exception's own message) reaches the server-side
    log -- an ``agent_utilities.*`` logger, sanitized by the process-wide
    log-privacy boundary before it leaves the process -- but the value
    stored on ``mcp`` is read verbatim by a served caller
    (``intent_tools._fleet_mux``), so it carries only the exception's CLASS
    name, never its message
    (``test_exception_surface_static_gate.py::test_served_packages_do_not_expose_raw_exception_text``).
    """
    mcp = SimpleNamespace()
    with (
        caplog.at_level(logging.WARNING, logger="graph_os.mcp_server.runtime"),
        patch(
            "graph_os.fleet.multiplexer.attach_fleet_loader",
            side_effect=RuntimeError("no mcp_config.json found"),
        ),
    ):
        runtime._attach_headless_fleet_loader(mcp)

    assert getattr(mcp, "_fleet_mux", None) is None
    reason = mcp._fleet_mux_unavailable_reason
    assert "RuntimeError" in reason
    assert "no mcp_config.json found" not in reason
    assert "no mcp_config.json found" in caplog.text
    assert "RuntimeError" in caplog.text


def _assert_headless_build_attaches() -> None:
    built_mcp = SimpleNamespace()
    with (
        patch.object(
            runtime,
            "_build_server",
            return_value=(SimpleNamespace(), built_mcp, []),
        ) as mock_build,
        patch.object(runtime, "_attach_headless_fleet_loader") as mock_attach,
    ):
        runtime.ensure_tools_registered()

    mock_build.assert_called_once_with(bootstrap=False)
    mock_attach.assert_called_once_with(built_mcp)


def test_ensure_tools_registered_attaches_fleet_loader_to_the_built_mcp(
    monkeypatch,
) -> None:
    """``ensure_tools_registered()`` now attaches a fleet loader to the exact
    ``mcp`` its headless ``_build_server(bootstrap=False)`` call returns."""
    monkeypatch.setattr(runtime, "REGISTERED_TOOLS", {})
    _assert_headless_build_attaches()


def test_ensure_tools_registered_skips_rebuilding_when_fully_registered(
    monkeypatch,
) -> None:
    """The idempotency guard short-circuits once a full build's names are present."""
    monkeypatch.setattr(runtime, "REGISTERED_TOOLS", {"find": object()})
    monkeypatch.setattr(runtime, "_FULL_TOOL_NAMES", frozenset({"find"}))
    with (
        patch.object(runtime, "_build_server") as mock_build,
        patch.object(runtime, "_attach_headless_fleet_loader") as mock_attach,
    ):
        runtime.ensure_tools_registered()

    mock_build.assert_not_called()
    mock_attach.assert_not_called()


def test_ensure_tools_registered_rebuilds_a_partial_table(monkeypatch) -> None:
    """A surface registrar's few entries are not a complete tool table.

    A2A or browser control can register a tool before any full build; that
    used to make ``ensure_tools_registered`` a no-op and leave the REST
    gateway dispatching over a partial table (and made the suite order
    dependent).
    """
    monkeypatch.setattr(runtime, "REGISTERED_TOOLS", {"graph_a2a": object()})
    monkeypatch.setattr(runtime, "_FULL_TOOL_NAMES", frozenset())
    _assert_headless_build_attaches()


def test_fleet_mux_returns_the_attached_multiplexer() -> None:
    sentinel = object()
    mcp = SimpleNamespace(_fleet_mux=sentinel)
    assert _fleet_mux(mcp) is sentinel


def test_fleet_mux_raises_the_recorded_reason_when_unattached() -> None:
    mcp = SimpleNamespace(
        _fleet_mux=None,
        _fleet_mux_unavailable_reason=(
            "fleet loader attach failed in this headless build (RuntimeError: boom)"
        ),
    )
    with pytest.raises(ValueError, match="fleet loader attach failed"):
        _fleet_mux(mcp)


def test_fleet_mux_raises_the_generic_message_when_no_reason_recorded() -> None:
    mcp = SimpleNamespace()
    with pytest.raises(ValueError, match="embedded/headless build"):
        _fleet_mux(mcp)
