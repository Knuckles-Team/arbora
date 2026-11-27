"""Doctor checks for the MCP fleet, the git hooks and the environment drift (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from arbora.core.environment import read_reference
from arbora.deployment.doctor_support import PROBE_FAULTS, _result

logger = logging.getLogger(__name__)


def _check_mcp_fleet(live: bool = False) -> dict[str, Any]:
    try:
        import json
        from pathlib import Path

        from arbora.core.settings import platform_config_file

        path = platform_config_file("mcp_config.json")
        if not path or not Path(path).exists():
            return _result(
                "mcp_fleet",
                "skip",
                "no mcp_config.json found in workspace",
                remediation=(
                    "Create an optional fleet catalog in the AgentConfig XDG root, "
                    "or set MCP_CONFIG to an explicit external fleet catalog"
                ),
            )
        import importlib.util as _u

        spec = _u.find_spec("scripts.validate_mcp_config")
        if spec is None:
            return _result("mcp_fleet", "skip", "validate_mcp_config not importable")
        cfg = json.loads(Path(path).read_text())
        mod = importlib.import_module("scripts.validate_mcp_config")
        rep = mod.validate(
            cfg, mod.caddy_hosts() if hasattr(mod, "caddy_hosts") else set(), live=live
        )
    except PROBE_FAULTS as exc:
        return _result(
            "mcp_fleet",
            "skip",
            f"fleet check skipped ({type(exc).__name__})",
        )
    safe_report = {
        "total": int(rep.get("total", 0)),
        "valid_count": len(rep.get("ok", [])),
        "invalid_count": len(rep.get("invalid", {})),
        "unreachable_count": len(rep.get("unreachable", {})),
        "missing_route_count": len(rep.get("missing_from_config", [])),
        "passed": bool(rep.get("passed", False)),
        "redacted": True,
    }
    if rep.get("passed"):
        return _result(
            "mcp_fleet",
            "ok",
            f"{safe_report['valid_count']} MCP server(s) valid",
            data=safe_report,
        )
    bad = {**rep.get("invalid", {}), **rep.get("unreachable", {})}
    return _result(
        "mcp_fleet",
        "warn",
        f"{len(bad)} MCP server(s) need attention",
        remediation="`python scripts/validate_mcp_config.py --live` for detail",
        data=safe_report,
    )


def _fleet_alias_kind(alias: str, reference: Any) -> str:
    """Classify one fleet alias as ``direct``, ``mapped``, or ``unresolved``.

    Any failure -- a missing projection, an unavailable reference, or control
    characters in either -- is ``unresolved``: the alias never counts as ready.
    """
    from scion_sdk.credentials.runtime_references import (
        resolve_runtime_secret_reference,
    )

    try:
        direct = read_reference(alias, None)
        if direct not in (None, ""):
            if any(character in str(direct) for character in "\x00\r\n"):
                raise ValueError("invalid direct runtime material")
            return "direct"
        resolved = resolve_runtime_secret_reference(reference)
        if resolved in (None, "") or any(
            character in str(resolved) for character in "\x00\r\n"
        ):
            raise ValueError("unavailable runtime reference")
        return "mapped"
    except PROBE_FAULTS:  # aliases and references stay redacted
        return "unresolved"


def _check_mcp_fleet_secrets() -> dict[str, Any]:
    """Validate neutral fleet alias resolution without disclosing alias metadata."""

    data = {
        "configured_alias_count": 0,
        "direct_alias_count": 0,
        "mapped_alias_count": 0,
        "unresolved_alias_count": 0,
        "redacted": True,
    }
    try:
        from arbora.core.settings import ArboraConfig as AgentConfig

        mappings = AgentConfig().mcp_fleet_secret_refs
        if not isinstance(mappings, dict) or len(mappings) > 512:
            raise ValueError("invalid fleet secret alias mapping")
        data["configured_alias_count"] = len(mappings)
        for alias, reference in mappings.items():
            data[f"{_fleet_alias_kind(alias, reference)}_alias_count"] += 1
    except PROBE_FAULTS as exc:  # doctor remains a redacted boundary
        return _result(
            "mcp_fleet_secrets",
            "fail",
            f"MCP fleet secret alias configuration is invalid ({type(exc).__name__})",
            remediation=(
                "Configure MCP_FLEET_SECRET_REFS as neutral aliases mapped only "
                "to env://, vault://, or secret:// runtime references."
            ),
            data=data,
        )
    if data["unresolved_alias_count"]:
        return _result(
            "mcp_fleet_secrets",
            "fail",
            "one or more MCP fleet secret aliases cannot be resolved",
            remediation=(
                "Project the direct runtime alias or repair its configured "
                "runtime secret reference."
            ),
            data=data,
        )
    return _result(
        "mcp_fleet_secrets",
        "ok",
        "MCP fleet secret alias resolution is ready",
        data=data,
    )


def _check_hooks() -> dict[str, Any]:
    try:
        from arbora.ecosystem.hook_installer import HookInstaller

        rep = HookInstaller().doctor()
    except PROBE_FAULTS as exc:
        return _result(
            "hooks", "skip", f"hook doctor unavailable ({type(exc).__name__})"
        )
    installed = [k for k, v in rep.items() if v.get("status") == "healthy"]
    stale = [k for k, v in rep.items() if v.get("status") == "stale"]
    if stale:
        return _result(
            "hooks",
            "warn",
            f"{len(stale)} stale hook(s): {stale}",
            remediation="re-install hooks (`graph_configure action=install_hooks`)",
            auto_fixable=True,
            data=rep,
        )
    return _result("hooks", "ok", f"{len(installed)} agent hook(s) healthy", data=rep)


def _resolve_prepush_shim_config(shim_text: str) -> str | None:
    """Extract the ``--config=<path>`` pre-commit's ``hook-impl`` resolves."""
    import re

    match = re.search(r"--config=(\S+)", shim_text)
    return match.group(1) if match else None


def audit_prepush_shim(shim_text: str | None, *, repo_root: Any) -> dict[str, Any]:
    """ARBORA-DEPLOY-R003: a relocated config path or missing hook is a failure.

    Never a silent pass-through: a missing shim, a shim that does not invoke
    pre-commit, a shim with no ``--config`` reference, or a reference whose
    file does not exist under ``repo_root`` all report ``fail`` with the exact
    reason, never ``ok``/``skip``.
    """
    if not shim_text:
        return _result("prepush_hook", "fail", "no pre-push hook is installed", data={})
    if "pre_commit" not in shim_text and "pre-commit" not in shim_text:
        return _result(
            "prepush_hook",
            "fail",
            "installed pre-push hook does not invoke pre-commit",
            data={"installed": True},
        )
    config = _resolve_prepush_shim_config(shim_text)
    if config is None:
        return _result(
            "prepush_hook",
            "fail",
            "pre-push hook has no --config reference",
            data={"installed": True, "config_path": None},
        )
    resolved = repo_root / config
    if not resolved.is_file():
        return _result(
            "prepush_hook",
            "fail",
            f"pre-push hook's configuration path does not resolve: {config}",
            remediation="re-run `uvx pre-commit install --hook-type pre-push`",
            data={"installed": True, "config_path": config, "config_exists": False},
        )
    return _result(
        "prepush_hook",
        "ok",
        "pre-push hook resolves to its pre-commit configuration",
        data={"installed": True, "config_path": config, "config_exists": True},
    )


def _check_prepush_hook() -> dict[str, Any]:
    import shutil
    import subprocess  # nosec B404 -- fixed-argv git prerequisite probes
    from pathlib import Path

    git = shutil.which("git")
    if git is None:
        return _result("prepush_hook", "skip", "git unavailable (not on PATH)")
    try:
        hook_path = subprocess.run(  # nosec B603 -- resolved git executable, fixed argv
            [git, "rev-parse", "--git-path", "hooks/pre-push"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        repo_root = subprocess.run(  # nosec B603 -- resolved git executable, fixed argv
            [git, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return _result(
            "prepush_hook", "skip", f"git unavailable ({type(exc).__name__})"
        )
    shim = Path(hook_path)
    shim_text = shim.read_text() if shim.is_file() else None
    return audit_prepush_shim(shim_text, repo_root=Path(repo_root))


def _check_venv_drift() -> dict[str, Any]:
    """Is the shared uv-workspace venv still what its lock says it should be?

    CONCEPT:MERISTEM-OS.host.venv-drift-detector

    This check exists because the shared development environment silently sat
    ten days behind its own lock: one import failed, an entire test module
    stopped collecting, and thirteen real defects were invisible until somebody
    happened to look.  Nothing was checking, so nothing was known.  Running it
    here means every ``arbora-doctor`` answers the question.

    Deployments have no uv workspace above them (the runtime image installs
    wheels), so an absent workspace is a ``skip``, not a failure.
    """

    try:
        from arbora.deployment.venv_plan import Workspace, WorkspaceNotFoundError
        from arbora.deployment.venv_sync import detect_drift
    except ImportError as exc:
        return _result("venv_drift", "error", f"venv_sync is not importable: {exc}")

    try:
        workspace = Workspace.discover()
    except WorkspaceNotFoundError:
        return _result(
            "venv_drift",
            "skip",
            "no uv workspace above this checkout (normal for a deployed runtime)",
        )
    except PROBE_FAULTS as exc:  # a probe must never crash the doctor
        return _result("venv_drift", "error", f"workspace discovery failed: {exc}")

    try:
        report = detect_drift(workspace)
    except PROBE_FAULTS as exc:  # a probe must never crash the doctor
        return _result("venv_drift", "error", f"drift detection failed: {exc}")

    status = {"ok": "ok", "warn": "warn", "fail": "fail"}[report.status]
    return _result(
        "venv_drift",
        status,
        report.summary,
        remediation=(
            None
            if status == "ok"
            else (
                "`arbora-venv status` for detail, then "
                "`arbora-venv sync` (safe form, guardrailed) or "
                "`arbora-venv relock` when a manifest moved."
            )
        ),
        data=report.as_dict(),
    )
