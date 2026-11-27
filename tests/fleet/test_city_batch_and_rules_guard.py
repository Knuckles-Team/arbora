"""The city layout reads its groups in one batch and defers to the engine's rules."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet import city_layout
from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.pin_lag import medulla as pin_lag


def _catalog(items: tuple[CatalogItem, ...], hidden: str = "") -> FleetCatalog:
    async def source() -> tuple[CatalogItem, ...]:
        return items

    async def visible(item: CatalogItem, _caller: Any) -> bool:
        return item.name != hidden

    return FleetCatalog([source], visible)


def _item(server: str, name: str, group: str | None) -> CatalogItem:
    return CatalogItem(
        f"fleet:tool:{server}/{name}", "tool", name, server=server, group=group
    )


@pytest.mark.spec("ARBORA-FLEET-R084")
async def test_server_groups_reads_every_named_server_in_one_pass() -> None:
    catalog = _catalog(
        (
            _item("alpha", "b", "late"),
            _item("alpha", "a", "ops"),
            _item("beta", "x", None),
            _item("other", "y", "skip"),
        ),
        hidden="a",
    )
    caller = SimpleNamespace(effective_scopes=frozenset({"mcp:discover"}))
    groups = await catalog.server_groups(caller, ["alpha", "beta", "gone"])
    # The lowest visible item id decides: alpha/a is hidden, so alpha/b answers.
    assert groups == {"alpha": "late", "beta": "", "gone": ""}


@pytest.mark.spec("ARBORA-FLEET-R084")
def test_the_local_layout_rules_answer_on_a_release_without_the_engine_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pin_lag, "world_view_layout_rules", lambda: None)
    placed = city_layout.derive_placements(
        [city_layout.CatalogEntry("b", "g"), city_layout.CatalogEntry("a", "")]
    )
    assert [(p.server_id, p.district, p.x, p.z) for p in placed] == [
        ("b", "g", 0, 0),
        ("a", "ungrouped", 200, 0),
    ]


@pytest.mark.spec("ARBORA-FLEET-R084")
def test_the_engine_layout_rules_own_the_placement_once_the_pin_carries_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = types.ModuleType("medulla.world_view")
    seen: list[Any] = []

    def derive(catalog: Any) -> Any:
        seen.append(list(catalog))
        return (SimpleNamespace(server_id="s", district="d", plot=3, x=11, z=22),)

    engine.CatalogEntry = lambda server_id, group: (server_id, group)  # type: ignore[attr-defined]
    engine.derive_placements = derive  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "medulla.world_view", engine)
    placed = city_layout.derive_placements([city_layout.CatalogEntry("s", "d")])
    assert seen == [[("s", "d")]]
    assert (placed[0].server_id, placed[0].plot, placed[0].x, placed[0].z) == (
        "s",
        3,
        11,
        22,
    )
    assert pin_lag.world_view_layout_rules() is engine


@pytest.mark.spec("ARBORA-FLEET-R084")
def test_the_guard_answers_none_when_the_pinned_release_lacks_the_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "medulla.world_view", None)
    assert pin_lag.world_view_layout_rules() is None
