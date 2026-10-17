"""Arbora-owned provider discovery (GRAPHOS-HOST-R001.10).

Ported from the agent-utilities ``core.providers`` contract tests: discovery is
distribution-owned and never imports provider code, rejects ambiguous or unsafe
ownership, and reads only the ``agent_connector_sdk.*_providers`` groups.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from arbora.core import providers
from arbora.core.provider_materialization import (
    build_asset_manifest,
    inactive_marker,
    write_managed_provider_marker,
)

pytestmark = pytest.mark.spec("GRAPHOS-HOST-R001.10")

REGISTRATION = "a" * 64
_ARBORA = Path(__file__).resolve().parents[2] / "arbora"


class _Distribution:
    def __init__(self, root: Path, name: str = "provider-dist", version: str = "1"):
        self.root = root
        self.metadata = {"Name": name}
        self.version = version
        self.files = tuple(
            PurePosixPath(path.relative_to(root).as_posix())
            for path in sorted(root.rglob("*"))
            if path.is_file()
        )

    def locate_file(self, path):
        return self.root / str(path)


class _EditableDistribution(_Distribution):
    def __init__(self, root: Path):
        super().__init__(root)
        self.files = (PurePosixPath("__editable__.provider_dist.pth"),)

    def read_text(self, filename: str) -> str | None:
        if filename != "direct_url.json":
            return None
        return json.dumps({"dir_info": {"editable": True}, "url": self.root.as_uri()})


def _entry(root: Path, *, name: str = "provider-a", target: str = "pkg.prompts"):
    return SimpleNamespace(
        name=name,
        value=target,
        group=providers.PROMPT_PROVIDER_GROUP,
        dist=_Distribution(root),
    )


def _editable_entry(root: Path):
    return SimpleNamespace(
        name="provider-a",
        value="pkg.prompts",
        group=providers.PROMPT_PROVIDER_GROUP,
        dist=_EditableDistribution(root),
    )


def _serve_entries(monkeypatch, *entries) -> list[str]:
    requested: list[str] = []

    def _entry_points(**kwargs):
        requested.append(kwargs["group"])
        return list(entries)

    monkeypatch.setattr(providers, "entry_points", _entry_points)
    return requested


def _owned_prompts(root: Path, *parts: str) -> Path:
    owned = root.joinpath(*(parts or ("pkg", "prompts")))
    owned.mkdir(parents=True)
    (owned / "prompt.json").write_text("{}", encoding="utf-8")
    return owned


def _forbid_imports(monkeypatch) -> None:
    monkeypatch.setattr(
        "importlib.import_module",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not import")
        ),
    )


def test_groups_are_the_connector_sdk_namespace_only() -> None:
    groups = (
        providers.SKILL_PROVIDER_GROUP,
        providers.PROMPT_PROVIDER_GROUP,
        providers.ONTOLOGY_PROVIDER_GROUP,
        providers.SOURCE_CONNECTOR_PROVIDER_GROUP,
    )
    assert all(group.startswith("agent_connector_sdk.") for group in groups)
    for retired in (
        "agent_utilities.skill_providers",
        "agent_utilities.prompt_providers",
        "agent_utilities.ontology_providers",
        "agent_utilities.source_connector_providers",
    ):
        with pytest.raises(ValueError):
            providers.provider_registrations(retired)
        with pytest.raises(ValueError):
            providers.current_provider_assets(retired)


def test_discovery_queries_only_the_requested_sdk_group(
    tmp_path: Path, monkeypatch
) -> None:
    _owned_prompts(tmp_path)
    requested = _serve_entries(monkeypatch, _entry(tmp_path))

    providers.provider_registrations(providers.PROMPT_PROVIDER_GROUP)

    assert requested == ["agent_connector_sdk.prompt_providers"]


def test_arbora_never_imports_the_agent_utilities_provider_module() -> None:
    offenders = []
    for path in sorted(_ARBORA.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(name == "agent_utilities.core.providers" for name in names):
                offenders.append(path.relative_to(_ARBORA).as_posix())
    assert offenders == []


def test_editable_registration_uses_pep610_owned_target_without_import(
    tmp_path: Path, monkeypatch
) -> None:
    owned = _owned_prompts(tmp_path)
    _serve_entries(monkeypatch, _editable_entry(tmp_path))
    _forbid_imports(monkeypatch)

    registration = providers.provider_registrations(providers.PROMPT_PROVIDER_GROUP)[0]
    assets = providers.current_provider_assets(providers.PROMPT_PROVIDER_GROUP)

    assert registration.source_root == owned
    assert registration.owned_paths == frozenset({"prompt.json"})
    assert [item.source_root for item in assets] == [owned]


def test_editable_registration_honors_safe_setuptools_package_root(
    tmp_path: Path, monkeypatch
) -> None:
    owned = _owned_prompts(tmp_path, "agent", "pkg", "prompts")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["agent"]\n', encoding="utf-8"
    )
    _serve_entries(monkeypatch, _editable_entry(tmp_path))

    registration = providers.provider_registrations(providers.PROMPT_PROVIDER_GROUP)[0]

    assert registration.source_root == owned


def test_editable_registration_rejects_unsafe_package_root_and_links(
    tmp_path: Path, monkeypatch
) -> None:
    unsafe = tmp_path / "unsafe"
    _owned_prompts(unsafe)
    (unsafe / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["../outside"]\n', encoding="utf-8"
    )
    _serve_entries(monkeypatch, _editable_entry(unsafe))
    assert (
        providers.provider_registrations(providers.PROMPT_PROVIDER_GROUP)[0].source_root
        is None
    )

    linked = tmp_path / "linked"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "prompt.json").write_text("{}", encoding="utf-8")
    (linked / "pkg").mkdir(parents=True)
    (linked / "pkg" / "prompts").symlink_to(outside, target_is_directory=True)
    _serve_entries(monkeypatch, _editable_entry(linked))
    assert providers.current_provider_assets(providers.PROMPT_PROVIDER_GROUP) == ()


def test_registration_is_distribution_owned_and_never_imports_provider(
    tmp_path: Path, monkeypatch
) -> None:
    owned = _owned_prompts(tmp_path)
    _serve_entries(monkeypatch, _entry(tmp_path))
    _forbid_imports(monkeypatch)

    registrations = providers.provider_registrations(providers.PROMPT_PROVIDER_GROUP)

    assert len(registrations) == 1
    assert registrations[0].group == "agent_connector_sdk.prompt_providers"
    assert registrations[0].source_root == owned
    assert len(registrations[0].digest) == 64


def test_source_connector_group_resolves_bounded_data_dirs(
    tmp_path: Path, monkeypatch
) -> None:
    owned = tmp_path / "pkg" / "connectors"
    owned.mkdir(parents=True)
    (owned / "mcp_source_presets.json").write_text("{}", encoding="utf-8")
    _serve_entries(monkeypatch, _entry(tmp_path, target="pkg.connectors"))

    assert providers.iter_provider_dirs(providers.SOURCE_CONNECTOR_PROVIDER_GROUP) == [
        ("provider-a", owned)
    ]


def test_unowned_or_unrecorded_assets_are_not_served(
    tmp_path: Path, monkeypatch
) -> None:
    unowned = tmp_path / "unowned"
    _owned_prompts(unowned, "different", "prompts")
    _serve_entries(monkeypatch, _entry(unowned, target="pkg.prompts"))
    assert providers.current_provider_assets(providers.PROMPT_PROVIDER_GROUP) == ()

    injected = tmp_path / "injected"
    owned = _owned_prompts(injected)
    entry = _entry(injected)
    (owned / "injected.json").write_text("{}", encoding="utf-8")
    _serve_entries(monkeypatch, entry)
    registration = providers.provider_registrations(providers.PROMPT_PROVIDER_GROUP)[0]
    assert registration.owned_paths == frozenset({"prompt.json"})
    assert providers.current_provider_assets(providers.PROMPT_PROVIDER_GROUP) == ()


def test_duplicate_and_casefold_provider_owners_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    _owned_prompts(tmp_path)
    _serve_entries(
        monkeypatch,
        _entry(tmp_path, name="Provider-A"),
        _entry(tmp_path, name="provider-a"),
    )

    with pytest.raises(providers.ProviderRegistrationConflict):
        providers.provider_registrations(providers.PROMPT_PROVIDER_GROUP)


def test_prompt_dirs_fall_back_to_the_current_source(
    tmp_path: Path, monkeypatch
) -> None:
    source = _owned_prompts(tmp_path / "src")
    manifest = build_asset_manifest(source, leg="prompts")
    registration = providers.ProviderRegistration(
        name="provider-a",
        group=providers.PROMPT_PROVIDER_GROUP,
        target="pkg.prompts",
        owner_name="provider",
        owner_version="1",
        digest=REGISTRATION,
        source_root=source,
        owned_paths=frozenset({"prompt.json"}),
    )
    assets = providers.ProviderAssets(registration, source, manifest)
    root = tmp_path / "xdg-prompts"
    root.mkdir()
    monkeypatch.setattr(providers, "current_provider_assets", lambda _group: (assets,))
    monkeypatch.setattr("agent_utilities.core.paths.unified_prompts_dir", lambda: root)

    assert providers.resolve_prompt_provider_dirs() == [("provider-a", source)]


def test_skill_dirs_come_only_from_registered_providers_and_flat_local_skills(
    tmp_path: Path, monkeypatch
) -> None:
    provider_root = tmp_path / "provider"
    (provider_root / "alpha").mkdir(parents=True)
    (provider_root / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha-skill\n---\nbody", encoding="utf-8"
    )
    (provider_root / "skill_graphs" / "topic").mkdir(parents=True)
    (provider_root / "skill_graphs" / "topic" / "SKILL.md").write_text(
        "---\nname: alpha-skill\n---\nreference page", encoding="utf-8"
    )
    registration = providers.ProviderRegistration(
        name="provider-a",
        group=providers.SKILL_PROVIDER_GROUP,
        target="pkg.skills",
        owner_name="provider",
        owner_version="1",
        digest=REGISTRATION,
        source_root=provider_root,
        owned_paths=frozenset(),
    )
    assets = providers.ProviderAssets(
        registration, provider_root, build_asset_manifest(provider_root, leg="skills")
    )
    xdg = tmp_path / "xdg"
    (xdg / "local-one").mkdir(parents=True)
    (xdg / "local-one" / "SKILL.md").write_text(
        "---\nname: local-one\n---\nbody", encoding="utf-8"
    )
    retired = xdg / "retired-provider"
    retired.mkdir()
    (retired / "SKILL.md").write_text("---\nname: retired\n---\n", encoding="utf-8")
    write_managed_provider_marker(
        retired,
        inactive_marker(
            provider="retired-provider", leg="skills", registration=REGISTRATION
        ),
    )
    groups: list[str] = []

    def _assets(group):
        groups.append(group)
        return (assets,)

    monkeypatch.setattr(providers, "current_provider_assets", _assets)
    monkeypatch.setattr("agent_utilities.core.paths.skills_dir", lambda: xdg)

    roots = providers.resolve_skill_provider_dirs()

    assert groups == ["agent_connector_sdk.skill_providers"]
    assert roots == [
        ("provider-a", provider_root / "alpha"),
        ("xdg-local", xdg / "local-one"),
    ]


def test_duplicate_flat_skill_identity_resolves_deterministically(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    for directory, identity in (("one", "duplicate-local"), ("two", "DUPLICATE-LOCAL")):
        root = tmp_path / directory
        root.mkdir()
        (root / "SKILL.md").write_text(
            f"---\nname: {identity}\n---\nbody", encoding="utf-8"
        )
    monkeypatch.setattr("agent_utilities.core.paths.skills_dir", lambda: tmp_path)
    monkeypatch.setattr(providers, "current_provider_assets", lambda _group: ())

    with caplog.at_level("ERROR", logger="arbora.core.providers"):
        roots = providers.resolve_skill_provider_dirs()

    assert roots == [("xdg-local", tmp_path / "one")]
    assert "duplicate-local" in caplog.text.lower()


def test_registry_digest_is_opaque_and_stable(tmp_path: Path, monkeypatch) -> None:
    _owned_prompts(tmp_path)
    _serve_entries(monkeypatch, _entry(tmp_path))

    first = providers.provider_registry_digest(providers.PROMPT_PROVIDER_GROUP)
    second = providers.provider_registry_digest(providers.PROMPT_PROVIDER_GROUP)

    assert first == second and len(first) == 64
    assert str(tmp_path) not in first
