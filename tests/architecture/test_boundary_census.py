"""Tests for the ARBORA-HOST-R001 import-boundary census."""

from __future__ import annotations

import textwrap
from pathlib import Path

from arbora.architecture.boundary_census import BoundaryOwner, ImportBoundaryCensus


def test_census_loads_seeds_from_the_published_registry() -> None:
    census = ImportBoundaryCensus.from_seeds_file()
    owner = census.owner_for("scion_sdk.mcp")
    assert owner is not None
    assert owner.owner_repository == "scion-sdk"


def test_census_accepts_an_import_through_the_public_surface(tmp_path: Path) -> None:
    census = ImportBoundaryCensus.from_seeds_file()
    source = tmp_path / "allowed.py"
    source.write_text(
        textwrap.dedent(
            """
            from scion_sdk.mcp import build_server
            """
        ),
        encoding="utf-8",
    )
    assert census.census(source) == []


def test_census_refuses_an_import_outside_the_public_surface(tmp_path: Path) -> None:
    census = ImportBoundaryCensus.from_seeds_file()
    source = tmp_path / "leaking.py"
    source.write_text(
        textwrap.dedent(
            """
            from scion_sdk.credentials.internal import _raw_secret_store
            """
        ),
        encoding="utf-8",
    )
    violations = census.census(source)
    assert len(violations) == 1
    assert violations[0].owner_repository == "scion-sdk"
    assert violations[0].imported == "scion_sdk.credentials.internal"


def test_owner_treats_arbora_imports_as_self_owned() -> None:
    census = ImportBoundaryCensus.from_seeds_file()
    owner = census.owner_for("arbora.fleet")
    assert owner is not None
    assert owner.owner_repository == "arbora"


def test_covers_rejects_a_sibling_prefix_that_is_not_a_dotted_child() -> None:
    owner = BoundaryOwner(
        owner_repository="scion-sdk",
        owned_module_paths=("scion_sdk/mcp",),
        public_import_surface=("scion_sdk.mcp",),
    )
    assert owner.covers("scion_sdk.mcp") is True
    assert owner.covers("scion_sdk.mcp_legacy") is False
