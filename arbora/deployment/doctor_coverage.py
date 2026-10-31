"""Ingestion, connector, workspace, and unified-install checks."""

from __future__ import annotations

import stat
from types import SimpleNamespace
from typing import Any

from arbora.api.errors import not_served

from .doctor_support import _result


def _check_skills() -> dict[str, Any]:
    """Report whether the agent-utilities skill toolkit is installed in the XDG dir.

    CONCEPT:MERISTEM-OS.deployment.agent-factory-autoload — the agent factory loads flat
    operator-owned skills plus valid managed subtrees for current providers under
    ``core.paths.skills_dir()``. The thirteen AU workflow skills unlock the platform. If
    they are absent, point at the one command that installs them. Local discovery
    paths never leave this probe.
    """
    exc = not_served("skills", "arbora/skills", owner="arbora")
    return _result(
        "skills",
        "fail",
        f"current skill resolution failed ({type(exc).__name__})",
        remediation="reconcile provider registrations and run `agent-utilities install`",
        skill="arbora-deployment",
        data={"ready": False, "redacted": True},
    )


def _unified_install_tally() -> SimpleNamespace:
    """Zeroed counters for one unified-install sweep."""
    return SimpleNamespace(
        missing=0,
        unresolved=0,
        materialized=0,
        stale_managed=0,
        unmanaged_nested=0,
        invalid_managed=0,
    )


def _count_generation(
    path: Any,
    provider: str,
    leg: str,
    registration: Any,
    source_manifest: Any,
    tally: SimpleNamespace,
) -> None:
    """Materialized when a managed generation resolves for this provider, else missing."""
    from arbora.core.provider_materialization import (
        resolve_managed_generation,
    )

    resolved = resolve_managed_generation(
        path,
        provider=provider,
        leg=leg,
        registration=registration,
        source_manifest=source_manifest,
    )
    if resolved is not None:
        tally.materialized += 1
    else:
        tally.missing += 1


def _count_provider_materialization(
    registration: Any, root: Any, leg: str, tally: SimpleNamespace
) -> None:
    """Count one registered provider as materialized, missing, or unresolved.

    A source that cannot be read is ``unresolved`` -- never silently skipped and
    never counted as materialized.
    """
    from arbora.core.provider_materialization import (
        ProviderAssetError,
        build_asset_manifest,
    )

    if registration.source_root is None:
        tally.unresolved += 1
        return
    try:
        manifest = build_asset_manifest(
            registration.source_root,
            leg=leg,
            allowed_relative_paths=registration.owned_paths,
        )
    except (OSError, ProviderAssetError, ValueError):
        tally.unresolved += 1
        return
    _count_generation(
        root / registration.name,
        registration.name,
        leg,
        registration.digest,
        manifest,
        tally,
    )


def _nested_child_is_plain_dir(child: Any, tally: SimpleNamespace) -> bool:
    """A leg-root child must be a real directory; anything else is invalid_managed."""
    try:
        child_info = child.lstat()
    except OSError:
        tally.invalid_managed += 1
        return False
    is_junction = getattr(child, "is_junction", lambda: False)()
    if child.is_symlink() or is_junction or not stat.S_ISDIR(child_info.st_mode):
        tally.invalid_managed += 1
        return False
    return True


def _classify_managed_marker(
    child: Any, leg: str, names: set[str], has_marker: bool, tally: SimpleNamespace
) -> None:
    """Classify a nested directory from its provider-ownership marker."""
    from arbora.core.provider_materialization import (
        read_managed_provider_marker,
    )

    marker = read_managed_provider_marker(child, provider=child.name, leg=leg)
    if marker is None:
        if has_marker:
            tally.invalid_managed += 1
        else:
            tally.unmanaged_nested += 1
    elif child.name not in names:
        tally.stale_managed += 1


def _classify_nested_child(
    child: Any, leg: str, names: set[str], tally: SimpleNamespace
) -> None:
    """Classify one directory under a leg root; an unmarked skill folder is fine."""
    from arbora.core.provider_materialization import marker_path_exists

    if not _nested_child_is_plain_dir(child, tally):
        return
    has_marker = marker_path_exists(child)
    if leg == "skills" and not has_marker and (child / "SKILL.md").is_file():
        return
    _classify_managed_marker(child, leg, names, has_marker, tally)


def _scan_nested_children(
    root: Any, leg: str, names: set[str], tally: SimpleNamespace
) -> None:
    """Classify every non-dotfile child under one materialized leg root."""
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.name.startswith("."):
            continue
        _classify_nested_child(child, leg, names, tally)


def _unified_install_result(
    legs: dict[str, tuple[Any, Any]],
    expected_counts: dict[str, int],
    tally: SimpleNamespace,
) -> dict[str, Any]:
    """The unified-install verdict; anything unreconciled is never reported ok."""
    data = {
        # Readiness is reportable; machine-specific XDG locations are not.  A
        # doctor result can itself be exported as telemetry, so never place a
        # host filesystem reference in its structured payload.
        "roots_ready": {leg: root.is_dir() for leg, (_g, root) in legs.items()},
        "expected_counts": expected_counts,
        "missing": tally.missing,
        "unresolved": tally.unresolved,
        "materialized": tally.materialized,
        "managed_ready": not any(
            (
                tally.missing,
                tally.unresolved,
                tally.stale_managed,
                tally.unmanaged_nested,
                tally.invalid_managed,
            )
        ),
        "stale_managed": tally.stale_managed,
        "unmanaged_nested": tally.unmanaged_nested,
        "invalid_managed": tally.invalid_managed,
        "redacted": True,
    }
    if tally.unresolved:
        return _result(
            "unified_install",
            "fail",
            f"current provider sources cannot be validated ({tally.unresolved} issue(s))",
            remediation="repair provider distributions before materialization",
            skill="arbora-deployment",
            data=data,
        )
    issues = (
        tally.missing
        + tally.stale_managed
        + tally.unmanaged_nested
        + tally.invalid_managed
    )
    if issues:
        return _result(
            "unified_install",
            "warn",
            f"unified provider materialization needs reconciliation ({issues} issue(s))",
            remediation=(
                "`agent-utilities install` (materializes current providers, marks "
                "ownership, and prunes removed managed providers)"
            ),
            skill="arbora-deployment",
            data=data,
        )
    return _result(
        "unified_install",
        "ok",
        f"unified XDG tree complete — {tally.materialized} provider contribution(s) materialized",
        data=data,
    )
