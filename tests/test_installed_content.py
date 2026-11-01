"""Arbora serves all installed content through the connector SDK.

Spec: ARBORA-FLEET-R044 (and ARBORA-HOST-R001.10 discovery). A fixture package
declares itself content-only under ``scion_sdk.connector``; nothing
here reads a provider entry-point group.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from arbora import content
from arbora.content import (
    EG_CONNECTOR,
    content_providers,
    eg_core_ontology_resources,
    register_installed_content,
)
from arbora.core import providers
from arbora.fleet.local_skill_catalog import build_local_skill_catalog

PROMPTS = Path(content.__file__).parent / "prompts"


def _install(root: Path, package: str, fleet_name: str, *, skills=(), ontology=False):
    """Create an importable package with a dist-info declaring it content-only."""
    pkg = root / package
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for name in skills:
        skill = pkg / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} fixture\n---\n# {name}\n"
        )
    prompts = pkg / "prompts"
    prompts.mkdir()
    (prompts / "triage.json").write_text(
        json.dumps(
            {
                "description": "fixture prompt",
                "identity": {"role": "Fixture triage"},
                "metadata": {"topic": "fixture-topic"},
                "serves": "Task:FixtureTriage",
                "instructions": {"core_directive": "Use `ask`."},
            }
        )
    )
    if ontology:
        (pkg / "ontology" / "shapes").mkdir(parents=True)
        (pkg / "ontology" / "fix.ttl").write_text("@prefix : <urn:f#> .\n:A a :B .\n")
        (pkg / "ontology" / "shapes" / "fix.shapes.ttl").write_text("# shapes\n")
    dist = root / f"{package}-1.2.3.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {package}\nVersion: 1.2.3\n"
    )
    (dist / "entry_points.txt").write_text(
        f"[scion_sdk.connector]\n{fleet_name} = {package}\n"
    )


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "site"
    root.mkdir()
    _install(
        root,
        "fixture_skills",
        "fixture-skills",
        skills=("alpha", "beta"),
        ontology=True,
    )
    _install(root, "fixture_more", "fixture-more", skills=("gamma",))
    monkeypatch.syspath_prepend(str(root))
    yield root
    for module in [m for m in sys.modules if m.startswith("fixture_")]:
        del sys.modules[module]


def _fixture_names() -> set[str]:
    return {
        c.name for c in providers.content_connectors() if c.name.startswith("fixture-")
    }


@pytest.mark.spec("ARBORA-HOST-R001.10")
def test_discovery_finds_content_only_package_and_arbora(installed: Path) -> None:
    names = {c.name for c in providers.content_connectors()}
    assert {"fixture-skills", "fixture-more", "arbora"} <= names
    skills = {root.name for _owner, root in providers.resolve_skill_provider_dirs()}
    assert {"alpha", "beta", "gamma", "arbora-genesis"} <= skills


@pytest.mark.spec("ARBORA-HOST-R001.10")
def test_duplicate_skill_identity_keeps_first_and_reports(
    installed: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _install(installed, "fixture_dup", "fixture-zdup", skills=("alpha",))
    with caplog.at_level("ERROR"):
        owners = dict(
            (root.name, owner)
            for owner, root in providers.resolve_skill_provider_dirs()
        )
    assert owners["alpha"] == "fixture-skills"
    assert "duplicate skill 'alpha'" in caplog.text


@pytest.mark.spec("ARBORA-FLEET-R025")
def test_catalog_includes_package_skills(installed: Path) -> None:
    entries, problems = build_local_skill_catalog()
    assert problems == []
    by_name = {e["name"]: e for e in entries}
    assert by_name["alpha"]["uri"] == "skill://alpha/SKILL.md"
    assert by_name["gamma"]["description"] == "gamma fixture"


@pytest.mark.spec("ARBORA-FLEET-R044.1")
@pytest.mark.spec("ARBORA-FLEET-R044.2")
@pytest.mark.asyncio
async def test_skills_prompts_and_ontology_are_served_natively(installed: Path) -> None:
    mcp = FastMCP("t")
    keep = tuple(p for p in content_providers() if p().connector.startswith("fixture-"))
    registered = register_installed_content(mcp, keep)
    assert registered.connectors["fixture-skills"].skills == 2
    async with Client(mcp) as client:
        resources = {str(r.uri) for r in await client.list_resources()}
        assert "skill://alpha/SKILL.md" in resources
        assert "skill://gamma/SKILL.md" in resources
        assert "ontology://fixture-skills/fix.ttl" in resources
        assert "shapes://fixture-skills/fix.shapes.ttl" in resources
        prompts = {p.name: p for p in await client.list_prompts()}
    meta = prompts["triage"].meta["team.knuckles/prompt"]
    assert meta["role"] == "Fixture triage"
    assert meta["topic"] == "fixture-topic"
    assert meta["systemPromptFor"] == "Task:FixtureTriage"
    assert prompts["triage"].title == "Fixture triage"


@pytest.mark.spec("ARBORA-FLEET-R044.5")
def test_semantic_providers_cover_every_content_package(installed: Path) -> None:
    from arbora.semantic_content import required_content_connectors

    required = set(required_content_connectors())
    assert {"arbora", "fixture-skills", "fixture-more"} <= required


@pytest.mark.spec("ARBORA-FLEET-R044.3")
def test_core_ontology_is_served_from_engine_package_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "fixture_engine"
    (pkg / "ontology" / "shapes").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "ontology" / "core-foundation-v1.ttl").write_text("# core\n")
    (pkg / "ontology" / "temporal-v1.shapes.ttl").write_text("# shapes\n")
    (pkg / "ontology" / "shapes" / "extra.ttl").write_text("# more\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    uris = {str(r.uri) for r in eg_core_ontology_resources("fixture_engine")}
    assert uris == {
        f"ontology://{EG_CONNECTOR}/core-foundation-v1.ttl",
        f"shapes://{EG_CONNECTOR}/temporal-v1.shapes.ttl",
        f"shapes://{EG_CONNECTOR}/extra.ttl",
    }


@pytest.mark.spec("ARBORA-FLEET-R044.3")
def test_engine_without_ontology_data_serves_none_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "fixture_bare").mkdir()
    (tmp_path / "fixture_bare" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    with caplog.at_level("WARNING"):
        assert eg_core_ontology_resources("fixture_bare") == []
    assert "no core ontology is served" in caplog.text


EXPECTED_GROUPS = {
    "cross-connector-proof": 3,
    "competency-question": 10,
    "cmdb-trm": 10,
    "delegation": 5,
    "system-prompt": 1,
}


@pytest.mark.spec("ARBORA-FLEET-R044.4")
def test_arbora_prompts_are_data_with_title_tags_and_meta() -> None:
    counts: dict[str, int] = {}
    for path in sorted(PROMPTS.glob("*.json")):
        doc = json.loads(path.read_text())
        topic = doc["metadata"]["topic"]
        counts[topic] = counts.get(topic, 0) + 1
        assert doc["identity"]["role"], path.name
        assert doc["identity"]["goal"], path.name
        assert {"prompt", topic} <= set(doc["tags"]), path.name
        directive = doc["instructions"]["core_directive"]
        assert any(
            tool in directive
            for tool in ("`ask`", "`find`", "`act`", "`agents.plan_run`")
        ), path.name
    assert counts == EXPECTED_GROUPS


@pytest.mark.spec("ARBORA-FLEET-R044.2")
@pytest.mark.asyncio
async def test_arbora_serves_its_own_prompts_with_meta() -> None:
    mcp = FastMCP("t")
    register_installed_content(
        mcp, tuple(p for p in content_providers() if p().connector == "arbora")
    )
    async with Client(mcp) as client:
        prompts = {p.name: p for p in await client.list_prompts()}
    assert len(prompts) == sum(EXPECTED_GROUPS.values())
    triage = prompts["incident-triage-system"].meta["team.knuckles/prompt"]
    assert triage["systemPromptFor"] == "Task:IncidentTriage"
    assert (
        prompts["cq-b"].meta["team.knuckles/prompt"]["topic"] == "cross-connector-proof"
    )
    assert prompts["cq-b"].title
