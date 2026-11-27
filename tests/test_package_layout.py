"""Wiring tests for every RF-ADR-009 §2 Arbora runtime package."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

#: The retired harness package name, spelled so no source line contains it.
RETIRED = "agent_" + "utilities"

SUBMODULES = [
    "arbora.mcp_server",
    "arbora.fleet",
    "arbora.gateway",
    "arbora.control_plane",
    "arbora.webui_host",
    "arbora.deployment",
]


@pytest.mark.parametrize("module_name", SUBMODULES)
def test_submodule_imports_and_documents_its_ownership(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.__doc__, f"{module_name} must document its RF-ADR-009 ownership"
    assert "RF-ADR-009" in module.__doc__


def test_serving_packages_do_not_import_legacy_au_multiplexer() -> None:
    package_root = Path(__file__).parents[1] / "arbora"
    offenders = [
        path.relative_to(package_root).as_posix()
        for area in ("fleet", "mcp_server")
        for path in (package_root / area).rglob("*.py")
        if f"{RETIRED}.mcp.multiplexer" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_mcp_network_listener_uses_sdk_hardened_serving_contract() -> None:
    server = (
        Path(__file__).parents[1] / "arbora" / "mcp_server" / "server.py"
    ).read_text(encoding="utf-8")

    assert "from scion_sdk.mcp.network import build_network_serving_config" in server
    assert "network_serving.fastmcp_run_kwargs()" in server
    assert "mcp_network_run_kwargs" not in server


def test_runtime_consumes_only_public_au_session_and_catalog_ports() -> None:
    package_root = Path(__file__).parents[1] / "arbora"
    session_port = package_root / "api" / "contracts" / "session.py"
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package_root.rglob("*.py")
        if path != session_port
    )

    assert f"{RETIRED}.knowledge_graph.core.session" not in production
    assert f"{RETIRED}.control_plane.catalogs" not in production
    assert f"{RETIRED}.api.agent_control_plane" not in production


def test_deployment_doctor_uses_native_arbora_hosts() -> None:
    doctor = (
        Path(__file__).parents[1] / "arbora" / "deployment" / "doctor.py"
    ).read_text(encoding="utf-8")

    assert f"{RETIRED}.mcp.multiplexer" not in doctor
    assert f"{RETIRED}.mcp.kg_server" not in doctor


def test_bootstrap_has_no_retired_legacy_query_runtime() -> None:
    bootstrap = (
        Path(__file__).parents[1] / "arbora" / "mcp_server" / "bootstrap.py"
    ).read_text(encoding="utf-8")

    retired_symbols = {
        "get_tabular_query_service",
        "_get_extraction_manager",
        "get_connection_registry",
        "_resolve_target_engines",
        "_resolve_read_engines",
        "resolve_explicit_graph",
        "bound_to_graph",
        "fanout_execute",
        "_ontology_system",
        "_bind_ontology_package_sync",
        "_bind_mcp_probe_port",
        "_runtime_authority_binders",
        "_sync_ontologies_at_boot",
        "_ingest_self_tool_surface_at_boot",
        "_arbora_self_tool_surface",
        "_mcp_capability_declaration",
        "_ingest_mcp_config_capabilities",
        "_ingest_native_tool_capabilities",
        "_enqueue_fleet_tool_schema_hydration",
        "_hydrate_code_and_configured_connectors",
        "_run_enabled_boot_hydration",
        "derive_capability_synonyms",
        "run_breadth_ingest",
        "sweep_all_sources",
        "_ensure_bundled_skills_ready",
        "_ingest_skill_capabilities",
        "_ingest_prompts_at_boot",
        "_run_boot_hydration_plan",
        "_record_hydration_manifest",
        "get_existing_disabled_batch",
    }
    assert all(symbol not in bootstrap for symbol in retired_symbols)

    runtime = (
        Path(__file__).parents[1] / "arbora" / "mcp_server" / "runtime.py"
    ).read_text(encoding="utf-8")
    assert "get_existing_disabled_batch" not in runtime


@pytest.mark.spec("ARBORA-HOST-R001.1")
def test_arbora_imports_no_au_serving_packages() -> None:
    """ARBORA-HOST-R001.1: AU 3.0.0 is harness only; Arbora owns MCP/REST/API."""
    import re

    package_root = Path(__file__).parents[1]
    pattern = re.compile(r"meristem[./](mcp|gateway|server|api)\b")
    offenders = [
        path.relative_to(package_root).as_posix()
        for path in (package_root / "arbora").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


@pytest.mark.spec("ARBORA-HOST-R001.5")
def test_arbora_declares_and_runs_fastmcp_4() -> None:
    from importlib.metadata import requires, version

    declared = [r for r in requires("arbora") or [] if r.startswith("fastmcp")]
    assert declared and ">=4" in declared[0]
    assert version("fastmcp").split(".")[0] == "4"


_RETIRED_HARNESS_PACKAGE = "agent_" + "utilities"


def _literal_import_module(node: Any) -> list[str]:
    """The literal argument of an ``importlib.import_module`` call."""
    import ast

    literal = node.args[0] if node.args else None
    named = getattr(node.func, "attr", "") == "import_module"
    if named and isinstance(literal, ast.Constant) and isinstance(literal.value, str):
        return [literal.value]
    return []


def _from_import(node: Any) -> list[str]:
    return [node.module] if node.level == 0 and node.module else []


def _node_imports(node: Any) -> list[str]:
    """The modules one ``ast`` node imports."""
    handlers = {
        "Import": lambda n: [alias.name for alias in n.names],
        "ImportFrom": _from_import,
        "Call": _literal_import_module,
    }
    handler = handlers.get(type(node).__name__)
    return handler(node) if handler else []


def _imports_of(path: Path) -> set[str]:
    """Every module a file imports, from ``import`` statements and literal ``import_module`` calls."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {module for node in ast.walk(tree) for module in _node_imports(node)}


def _retired_imports(path: Path) -> list[str]:
    return sorted(
        module
        for module in _imports_of(path)
        if module.split(".", 1)[0] == _RETIRED_HARNESS_PACKAGE
    )


@pytest.mark.spec("ARBORA-HOST-R007.2.1")
def test_a2a_authority_imports_no_retired_harness_package() -> None:
    root = Path(__file__).parents[1] / "arbora"
    assert _retired_imports(root / "a2a" / "authority.py") == []


@pytest.mark.spec("ARBORA-HOST-R007.2.2")
def test_a2a_routing_imports_no_retired_harness_package() -> None:
    root = Path(__file__).parents[1] / "arbora"
    assert _retired_imports(root / "a2a" / "routing.py") == []


@pytest.mark.spec("ARBORA-HOST-R007.2.55")
def test_mcp_server_entry_imports_no_retired_harness_package() -> None:
    root = Path(__file__).parents[1] / "arbora"
    assert _retired_imports(root / "mcp_server" / "server.py") == []


@pytest.mark.spec("ARBORA-HOST-R007.2.68")
def test_webui_co_service_imports_no_retired_harness_package() -> None:
    root = Path(__file__).parents[1] / "arbora"
    assert _retired_imports(root / "webui_host" / "webui_co_service.py") == []


def test_the_retired_import_check_finds_a_planted_import(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(
        f"import {RETIRED}.api\nimport importlib\n"
        f"importlib.import_module('{RETIRED}.graph')\n",
        encoding="utf-8",
    )
    assert _retired_imports(planted) == [f"{RETIRED}.api", f"{RETIRED}.graph"]


# The pin-lag guard modules are the one place a pinned meristem release's earlier
# module locations may be read; each is removed after the meristem re-pin.
_FENCE_EXEMPT = ("arbora/pin_lag/", "tests/pin_lag/")
_MERISTEM_ROOT_NAMES = frozenset(
    {
        "__version__",
        "PUBLIC_HOST_MODULES",
        "create_graph_agent",
        "execute_graph",
        "initialize_graph_from_workspace",
        "environment_declarations",
    }
)


def _from_meristem_root(node: Any) -> list[str]:
    """``from meristem import x`` names the module ``meristem.x`` unless ``x`` is a root export."""
    if node.level != 0 or node.module != "meristem":
        return []
    return [
        f"meristem.{a.name}" for a in node.names if a.name not in _MERISTEM_ROOT_NAMES
    ]


def _meristem_modules(path: Path) -> set[str]:
    """Every ``meristem`` submodule a file imports; the bare root package is allowed."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        found.update(_node_imports(node))
        if isinstance(node, ast.ImportFrom):
            found.update(_from_meristem_root(node))
    return {module for module in found if module.startswith("meristem.")}


def _is_public(module: str, public: tuple[str, ...]) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in public)


def _fence_violations(root: Path, public: tuple[str, ...]) -> list[str]:
    """Retired-harness imports and non-public ``meristem`` imports under ``root``."""
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        violations += [f"{rel}: {m}" for m in _retired_imports(path)]
        violations += [
            f"{rel}: {m}"
            for m in sorted(_meristem_modules(path))
            if not _is_public(m, public)
        ]
    return violations


def _fenced_tree_violations(public: tuple[str, ...]) -> list[str]:
    repo = Path(__file__).parents[1]
    found = [
        f"{area}/{line}"
        for area in ("arbora", "tests")
        for line in _fence_violations(repo / area, public)
    ]
    return [line for line in found if not line.startswith(_FENCE_EXEMPT)]


@pytest.mark.spec("ARBORA-HOST-R007.2.69")
@pytest.mark.spec("ARBORA-HOST-R007.1")
def test_arbora_imports_only_meristem_public_modules() -> None:
    """Every import under ``arbora/`` and ``tests/`` is a declared meristem public module."""
    from arbora.pin_lag.meristem import fence_modules

    public, _installed = fence_modules()
    assert "meristem.host_ports" in public
    assert _fenced_tree_violations(public) == []


@pytest.mark.spec("ARBORA-HOST-R007.2.69")
@pytest.mark.spec("ARBORA-HOST-R007.1")
def test_arbora_imports_only_declared_meristem_modules() -> None:
    from arbora.pin_lag.meristem import MAIN_PUBLIC_HOST_MODULES

    assert _fenced_tree_violations(MAIN_PUBLIC_HOST_MODULES) == []


@pytest.mark.spec("ARBORA-HOST-R007.2.69")
def test_fence_detects_planted_violation(tmp_path: Path) -> None:
    from arbora.pin_lag.meristem import MAIN_PUBLIC_HOST_MODULES

    (tmp_path / "planted.py").write_text(
        "from meristem.harness.edit_engine import apply_edits\n"
        "from meristem import host_ports\n"
        "from meristem.graph.state import GraphState\n"
        f"import {RETIRED}.graph\n"
        "import importlib\n"
        "importlib.import_module('meristem.core.paths')\n",
        encoding="utf-8",
    )
    assert _fence_violations(tmp_path, MAIN_PUBLIC_HOST_MODULES) == [
        f"planted.py: {RETIRED}.graph",
        "planted.py: meristem.core.paths",
        "planted.py: meristem.harness.edit_engine",
    ]


@pytest.mark.spec("ARBORA-HOST-R007.2.69")
def test_the_pinned_release_without_the_declaration_refuses_unavailable() -> None:
    import meristem

    from arbora.pin_lag.meristem import (
        MAIN_PUBLIC_HOST_MODULES,
        PinLagUnavailable,
        fence_modules,
        public_host_modules,
    )

    public, installed = fence_modules()
    if hasattr(meristem, "PUBLIC_HOST_MODULES"):
        assert installed and public == tuple(meristem.PUBLIC_HOST_MODULES)
        assert public == MAIN_PUBLIC_HOST_MODULES
        return
    assert not installed and public == MAIN_PUBLIC_HOST_MODULES
    with pytest.raises(PinLagUnavailable):
        public_host_modules()


@pytest.mark.spec("ARBORA-HOST-R007.2.70")
def test_no_retired_harness_import_remains() -> None:
    repo = Path(__file__).parents[1]
    offenders = [
        f"{area}/{path.relative_to(repo / area).as_posix()}"
        for area in ("arbora", "tests")
        for path in sorted((repo / area).rglob("*.py"))
        if _retired_imports(path)
    ]
    # The planted-import test above writes its fixture at runtime, not under tests/.
    assert [o for o in offenders if o != "tests/test_package_layout.py"] == []
