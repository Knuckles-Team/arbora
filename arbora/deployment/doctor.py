#!/usr/bin/python
"""``arbora-doctor`` — one holistic health sweep of a deployment.

Like ``brew doctor`` / ``flutter doctor``: runs a battery of independent checks
across every subsystem, each reporting ok / warn / fail / skip with a concrete
**remediation** — and, where one exists, the **skill or command that fixes it** so
the operator (or Claude) can act or auto-fix. The doctor is a thin *aggregator*: it
composes the diagnostics that already exist (config_doctor, shard topology probe,
backend health_check, the hook doctor, the MCP-config validator, secrets resolution)
rather than re-implementing them.

Each check is defensive — a missing optional dependency or an unreachable service
yields ``skip``/``warn``/``fail`` with guidance, never a crash. ``run_doctor`` returns
a structured report; ``fix=True`` runs the conservative, idempotent auto-remediations
(only checks marked ``auto_fixable``).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from arbora.core.settings import RuntimeConfig
from arbora.deployment.doctor_support import PROBE_FAULTS

from . import doctor_a2a as _doctor_a2a
from . import doctor_certification as _doctor_certification
from . import doctor_coverage as _doctor_coverage
from . import doctor_graph as _doctor_graph
from . import doctor_lakehouse as _doctor_lakehouse
from . import doctor_observability as _doctor_observability
from . import doctor_support as _doctor_support

_RANK = _doctor_support._RANK
_prescription = _doctor_support._prescription
_result = _doctor_support._result

from arbora.deployment.doctor_fleet import (
    _check_hooks,
    _check_mcp_fleet,
    _check_mcp_fleet_secrets,
    _check_prepush_hook,
    _check_venv_drift,
)
from arbora.deployment.doctor_identity import (
    _check_auth,
    _check_graph_identity,
    _check_identity_exposure,
    _check_outbound_auth,
    _check_secrets,
    _check_secrets_backend,
)
from arbora.deployment.doctor_runtime import (
    _check_engine,
    _check_engine_request_context,
    _check_runtime_integrations,
)
from arbora.deployment.doctor_security import (
    _check_eunomia,
    _check_execution_security,
    _check_google_workspace_oauth,
    _check_permission_governance,
    _check_source_egress,
    _check_transport_security,
)

logger = logging.getLogger(__name__)


def _validate_skill_certification_regular_inputs(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._validate_skill_certification_regular_inputs(
        *args, **kwargs
    )


def _validate_skill_certification_profile(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._validate_skill_certification_profile(*args, **kwargs)


def _validate_skill_certification_commands(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._validate_skill_certification_commands(*args, **kwargs)


def _validate_skill_certification_material(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._validate_skill_certification_material(*args, **kwargs)


def _check_skill_certification(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._check_skill_certification(*args, **kwargs)


def _cert_required_values(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._cert_required_values(*args, **kwargs)


def _cert_configuration_gate(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._cert_configuration_gate(*args, **kwargs)


def _cert_verify_release_manifest(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._cert_verify_release_manifest(*args, **kwargs)


def _cert_verify_artifacts_dir(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._cert_verify_artifacts_dir(*args, **kwargs)


def _cert_verify_bearer_token(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._cert_verify_bearer_token(*args, **kwargs)


def _cert_resolve_prometheus_trust(*args: Any, **kwargs: Any) -> Any:
    return _doctor_certification._cert_resolve_prometheus_trust(*args, **kwargs)


def _require_observability_imports(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._require_observability_imports(*args, **kwargs)


def _otel_endpoint(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._otel_endpoint(*args, **kwargs)


def _otel_transport_posture(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._otel_transport_posture(*args, **kwargs)


def _otel_tls_profile_configured(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._otel_tls_profile_configured(*args, **kwargs)


def _otel_data(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._otel_data(*args, **kwargs)


def _otel_prove_credentials(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._otel_prove_credentials(*args, **kwargs)


def _otel_tls_verify_enabled(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._otel_tls_verify_enabled(*args, **kwargs)


def _check_observability(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._check_observability(*args, **kwargs)


def _run_async_doctor_probe(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._run_async_doctor_probe(*args, **kwargs)


def _langfuse_rows(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_rows(*args, **kwargs)


def _langfuse_posture_metadata_only(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_posture_metadata_only(*args, **kwargs)


def _langfuse_trace_read_bounded(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_trace_read_bounded(*args, **kwargs)


async def _call_langfuse_child(*args: Any, **kwargs: Any) -> Any:
    return await _doctor_observability._call_langfuse_child(*args, **kwargs)


def _probe_langfuse_mcp_visibility(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._probe_langfuse_mcp_visibility(*args, **kwargs)


def _probe_langfuse_live(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._probe_langfuse_live(*args, **kwargs)


def _langfuse_inputs(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_inputs(*args, **kwargs)


def _langfuse_data(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_data(*args, **kwargs)


def _langfuse_configuration_gate(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_configuration_gate(*args, **kwargs)


def _langfuse_credential_gate(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_credential_gate(*args, **kwargs)


def _langfuse_persistence_gate(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_persistence_gate(*args, **kwargs)


def _langfuse_trust_gate(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_trust_gate(*args, **kwargs)


def _langfuse_live_result(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_live_result(*args, **kwargs)


def _langfuse_ready_result(*args: Any, **kwargs: Any) -> Any:
    return _doctor_observability._langfuse_ready_result(*args, **kwargs)


def _check_engine_domains() -> Any:
    return _doctor_graph._check_engine_domains()


def _check_a2a_persistence(
    live: bool = False, service: Any = None, read_task: Any = None
) -> Any:
    """Probe A2A task persistence; only a process that serves A2A can run it."""
    if not live or service is None:
        return _doctor_a2a._result(
            "a2a_persistence",
            "skip",
            "the A2A persistence probe runs live inside a serving process",
        )
    import asyncio

    return asyncio.run(_doctor_a2a.check_a2a_persistence(service, read_task))


def _check_graph_connections(live: bool = False) -> Any:
    return _doctor_graph._check_graph_connections(live=live)


def _check_langfuse(live: bool = False) -> Any:
    return _doctor_observability._check_langfuse(
        live=live, probe_live=_probe_langfuse_live
    )


def _check_skills(*args: Any, **kwargs: Any) -> Any:
    return _doctor_coverage._check_skills(*args, **kwargs)


def _unified_install_tally(*args: Any, **kwargs: Any) -> Any:
    return _doctor_coverage._unified_install_tally(*args, **kwargs)


def _count_generation(*args: Any, **kwargs: Any) -> Any:
    return _doctor_coverage._count_generation(*args, **kwargs)


def _count_provider_materialization(*args: Any, **kwargs: Any) -> Any:
    return _doctor_coverage._count_provider_materialization(*args, **kwargs)


def _nested_child_is_plain_dir(*args: Any, **kwargs: Any) -> Any:
    return _doctor_coverage._nested_child_is_plain_dir(*args, **kwargs)


def _classify_managed_marker(*args: Any, **kwargs: Any) -> Any:
    return _doctor_coverage._classify_managed_marker(*args, **kwargs)


def _classify_nested_child(*args: Any, **kwargs: Any) -> Any:
    return _doctor_coverage._classify_nested_child(*args, **kwargs)


def _scan_nested_children(*args: Any, **kwargs: Any) -> Any:
    return _doctor_coverage._scan_nested_children(*args, **kwargs)


def _unified_install_result(*args: Any, **kwargs: Any) -> Any:
    return _doctor_coverage._unified_install_result(*args, **kwargs)


def _endpoint_host_port(*args: Any, **kwargs: Any) -> Any:
    return _doctor_lakehouse._endpoint_host_port(*args, **kwargs)


def _probe_tcp(*args: Any, **kwargs: Any) -> Any:
    return _doctor_lakehouse._probe_tcp(*args, **kwargs)


def _probe_http(*args: Any, **kwargs: Any) -> Any:
    return _doctor_lakehouse._probe_http(*args, **kwargs)


def _check_kafka(live: bool = False) -> Any:
    return _doctor_lakehouse._check_kafka(live=live, probe_tcp=_probe_tcp)


def _check_fuseki(live: bool = False) -> Any:
    return _doctor_lakehouse._check_fuseki(live=live, probe_http=_probe_http)


def _check_seaweedfs_s3(live: bool = False) -> Any:
    return _doctor_lakehouse._check_seaweedfs_s3(live=live, probe_tcp=_probe_tcp)


def _lakekeeper_prescription(*args: Any, **kwargs: Any) -> Any:
    return _doctor_lakehouse._lakekeeper_prescription(*args, **kwargs)


def _lakekeeper_gotcha_findings(*args: Any, **kwargs: Any) -> Any:
    return _doctor_lakehouse._lakekeeper_gotcha_findings(*args, **kwargs)


def _check_lakekeeper(live: bool = False) -> Any:
    return _doctor_lakehouse._check_lakekeeper(live=live, probe_http=_probe_http)


def _check_lakekeeper_db(*args: Any, **kwargs: Any) -> Any:
    return _doctor_lakehouse._check_lakekeeper_db(*args, **kwargs)


def _check_trino(live: bool = False) -> Any:
    return _doctor_lakehouse._check_trino(live=live, probe_http=_probe_http)


def _check_spark_runner(live: bool = False) -> Any:
    return _doctor_lakehouse._check_spark_runner(live=live, probe_tcp=_probe_tcp)


# ── individual checks (each returns one _result; never raises) ──────────────
def _optional_extras_present() -> dict[str, bool]:
    """Which optional runtime extras are importable, keyed by human label."""
    return {
        label: importlib.util.find_spec(mod) is not None
        for mod, label in (
            ("psycopg", "postgres"),
            ("stardog", "stardog"),
        )
    }


def _python_env_detail(ver: str, optional: dict[str, bool]) -> str:
    """Render the python_env detail line from the resolved extras map."""
    import platform

    present = [k for k, v in optional.items() if v]
    missing = [k for k, v in optional.items() if not v]
    detail = (
        f"Python {platform.python_version()}, meristem {ver}; "
        f"optional extras present: {present or 'none'}"
    )
    if missing:
        detail += f"; absent (install if needed): {missing}"
    return detail


def _check_python_env() -> dict[str, Any]:
    import importlib.metadata
    import sys

    try:
        ver = importlib.metadata.version("meristem")
    except importlib.metadata.PackageNotFoundError:
        return _result(
            "python_env",
            "fail",
            "meristem not installed",
            remediation="pip install meristem",
        )
    optional = _optional_extras_present()
    py_ok = sys.version_info >= (3, 12)
    return _result(
        "python_env",
        "ok" if py_ok else "warn",
        _python_env_detail(ver, optional),
        remediation=None if py_ok else "arbora needs Python 3.12+",
        data=optional,
    )


def _check_mcp_sdk_floor() -> dict[str, Any]:
    """The installed `mcp`/`fastmcp` SDK must satisfy the `[mcp]` extra's declared floor.

    A runtime that resolved an older SDK line (`mcp` v1 while the extra declares
    `fastmcp>=4.0.0b1` / SDK v2) previously only failed as a swallowed `ImportError`
    deep inside `child_resilience.py` at serve time (D-ISR-2). This surfaces that
    mismatch here instead through Arbora's native protocol compatibility gate.
    """
    try:
        from arbora.fleet.protocol_compat import check_mcp_sdk_floor
    except ImportError as exc:
        return _result(
            "mcp_sdk_floor",
            "skip",
            f"protocol_compat unavailable ({type(exc).__name__})",
        )
    outcome = check_mcp_sdk_floor()
    if outcome["ok"] is None:
        return _result("mcp_sdk_floor", "skip", outcome["detail"])
    if outcome["ok"]:
        return _result("mcp_sdk_floor", "ok", outcome["detail"])
    return _result(
        "mcp_sdk_floor",
        "fail",
        outcome["detail"],
        remediation=(
            "reinstall/rebuild the runtime image so `mcp`/`fastmcp` resolve the "
            "versions declared by Arbora in pyproject.toml (re-lock and redeploy)"
        ),
    )


def _check_config() -> dict[str, Any]:
    try:
        from arbora.deployment.config_generator import config_doctor

        rep = config_doctor()
    except PROBE_FAULTS as exc:
        return _result(
            "config", "error", f"config_doctor failed ({type(exc).__name__})"
        )
    healthy = rep.get("healthy")
    profile = rep.get("profile", "?")
    if healthy:
        return _result(
            "config", "ok", f"config healthy for profile {profile!r}", data=rep
        )
    # Tiny's durability findings are advisory.
    status = "warn" if profile == "tiny" else "fail"
    return _result(
        "config",
        status,
        f"config needs attention (profile {profile!r}) — see checks",
        remediation="`setup-config doctor` for detail; `setup-config generate --profile <p>` to (re)seed",
        skill="arbora-deployment",
        data=rep,
    )


def _check_evolution_staging() -> dict[str, Any]:
    """Validate the evolution artifact boundary without exposing its path."""

    import os
    from pathlib import Path

    try:
        from arbora.core.settings import ArboraConfig as AgentConfig

        configured = AgentConfig().evolution_staging_root
    except PROBE_FAULTS as exc:
        return _result(
            "evolution_staging",
            "error",
            f"evolution staging configuration unavailable ({type(exc).__name__})",
            data={"configured": False, "usable": False},
        )
    if not configured:
        return _result(
            "evolution_staging",
            "skip",
            "reviewable evolution artifact staging is not configured",
            remediation=(
                "Set EVOLUTION_STAGING_ROOT in AgentConfig before enabling artifact "
                "drafting or materialization."
            ),
            data={"configured": False, "usable": False},
        )
    try:
        raw = Path(configured).expanduser()
        if raw.is_symlink():
            raise PermissionError("symbolic-link root")
        root = raw.resolve(strict=True)
        if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise PermissionError("unusable root")
        if os.name != "nt" and root.stat().st_mode & 0o077:
            return _result(
                "evolution_staging",
                "fail",
                "evolution staging is accessible outside the current account",
                remediation="Restrict the configured staging root to mode 0700.",
                data={"configured": True, "usable": False},
            )
    except PROBE_FAULTS as exc:
        return _result(
            "evolution_staging",
            "fail",
            f"evolution staging is unusable ({type(exc).__name__})",
            remediation=(
                "Create a private non-symlink directory, set mode 0700, and point "
                "EVOLUTION_STAGING_ROOT to it."
            ),
            data={"configured": True, "usable": False},
        )
    return _result(
        "evolution_staging",
        "ok",
        "evolution staging is configured, private, and usable",
        data={"configured": True, "usable": True},
    )


# Registry: name -> callable. Order is the report order.
CHECKS: Mapping[str, Callable[..., dict[str, Any]]] = MappingProxyType(
    {
        "python_env": _check_python_env,
        "mcp_sdk_floor": _check_mcp_sdk_floor,
        "config": _check_config,
        "evolution_staging": _check_evolution_staging,
        "execution_security": _check_execution_security,
        "permission_governance": _check_permission_governance,
        "transport_security": _check_transport_security,
        "google_workspace_oauth": _check_google_workspace_oauth,
        "source_egress": _check_source_egress,
        "eunomia": _check_eunomia,
        "runtime_integrations": _check_runtime_integrations,
        "engine_request_context": _check_engine_request_context,
        "engine": _check_engine,
        "engine_domains": _check_engine_domains,
        "secrets": _check_secrets,
        "secrets_backend": _check_secrets_backend,
        "auth": _check_auth,
        "identity_exposure": _check_identity_exposure,
        "outbound_auth": _check_outbound_auth,
        "skill_certification": _check_skill_certification,
        "graph_identity": _check_graph_identity,
        "mcp_fleet_secrets": _check_mcp_fleet_secrets,
        "mcp_fleet": _check_mcp_fleet,
        "hooks": _check_hooks,
        "prepush_hook": _check_prepush_hook,
        "observability": _check_observability,
        "graph_connections": _check_graph_connections,
        "a2a_persistence": _check_a2a_persistence,
        "langfuse": _check_langfuse,
        "skills": _check_skills,
        "venv_drift": _check_venv_drift,
        "kafka": _check_kafka,
        "fuseki": _check_fuseki,
        "seaweedfs_s3": _check_seaweedfs_s3,
        "lakekeeper": _check_lakekeeper,
        "lakekeeper_db": _check_lakekeeper_db,
        "trino": _check_trino,
        "spark_runner": _check_spark_runner,
    }
)

# Checks that accept a `live=` kwarg and prove real reachability when the doctor
# is run with `--live` -- shared between `run_doctor`'s dispatch and
# `interactive_apply`'s post-apply re-run so the two never drift apart.
_CONFIG_CHECK_NAMES = frozenset({"auth", "identity_exposure"})
_LIVE_CHECK_NAMES = frozenset(
    {
        "mcp_fleet",
        "graph_connections",
        "a2a_persistence",
        "langfuse",
        "kafka",
        "fuseki",
        "seaweedfs_s3",
        "lakekeeper",
        "lakekeeper_db",
        "trino",
        "spark_runner",
    }
)


def _auto_fix(name: str) -> dict[str, Any]:
    """Run a conservative, idempotent remediation for an auto-fixable check."""
    if name == "hooks":
        try:
            from arbora.ecosystem.hook_installer import HookInstaller

            inst = HookInstaller()
            inst.install()
            return {
                "fixed": name,
                "result": "re-installed hooks",
                "errors": inst.errors,
            }
        except PROBE_FAULTS as exc:
            return {
                "fixed": name,
                "error": f"auto-fix failed ({type(exc).__name__})",
            }
    return {"fixed": name, "result": "no auto-fix available"}


def _selection_is_invalid(only: Any) -> bool:
    """Reject anything but a non-empty, duplicate-free list of registered names."""
    return (
        not isinstance(only, list)
        or not only
        or len(only) > len(CHECKS)
        or any(not isinstance(name, str) or name not in CHECKS for name in only)
        or len(set(only)) != len(only)
    )


def _unhealthy_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """A report that ran no check: every result is an error, so it fails closed."""
    return {
        "status": "unhealthy",
        "counts": {"error": len(results)},
        "checks": results,
        "fixes": [],
        "summary": _summarize("unhealthy", results),
    }


def _load_runtime_config(names: list[str]) -> list[dict[str, Any]] | None:
    """Load the deployment AgentConfig; on failure, one error result per check.

    Every runtime entry point consumes the same XDG AgentConfig document. A
    doctor launched directly from its console script must do that too; without
    this load, checks that instantiate ``AgentConfig`` would silently inspect
    package defaults instead of the deployment Arbora actually uses. Returning
    results (rather than proceeding) keeps the sweep fail-closed: no check may
    report ok against defaults the deployment does not use.
    """
    try:
        from arbora.core.settings import load_config

        load_config()
    except PROBE_FAULTS as exc:  # source details may be sensitive
        return [
            _result(
                name,
                "error",
                f"configuration load failed ({type(exc).__name__})",
                remediation=(
                    "Repair the private AgentConfig source, then rerun the doctor; "
                    "configuration values are intentionally not reported."
                ),
                data={"redacted": True},
            )
            for name in names
        ]
    return None


def _call_check(
    name: str, fn: Callable[..., dict[str, Any]], config: RuntimeConfig, live: bool
) -> dict[str, Any]:
    """Call one registered check with the arguments its kind takes."""
    if name in _CONFIG_CHECK_NAMES:
        return fn(config)
    return fn(live=live) if name in _LIVE_CHECK_NAMES else fn()


def _run_checks(
    names: list[str], config: RuntimeConfig, *, live: bool
) -> list[dict[str, Any]]:
    """Run each selected check; a check that raises becomes an error result."""
    results: list[dict[str, Any]] = []
    for name in names:
        fn = CHECKS.get(name)
        if fn is None:
            continue
        try:
            res = _call_check(name, fn, config, live)
        except PROBE_FAULTS as exc:  # a check must never crash the doctor
            res = _result(name, "error", f"check raised ({type(exc).__name__})")
        results.append(res)
    return results


def _apply_auto_fixes(
    results: list[dict[str, Any]], config: RuntimeConfig
) -> list[dict[str, Any]]:
    """Auto-remediate the ``auto_fixable`` not-ok checks and re-run each in place."""
    fixes: list[dict[str, Any]] = []
    for res in results:
        if res["status"] in ("warn", "fail") and res.get("auto_fixable"):
            fixes.append(_auto_fix(res["name"]))
            try:
                res.update(
                    _call_check(res["name"], CHECKS[res["name"]], config, False)
                )  # re-run after fix
            except PROBE_FAULTS:
                pass  # nosec B110 - auto-fix report remains failed
    return fixes


def _doctor_report(
    results: list[dict[str, Any]], fixes: list[dict[str, Any]]
) -> dict[str, Any]:
    """Aggregate the per-check results into the overall verdict (worst wins)."""
    worst = max((_RANK[r["status"]] for r in results), default=0)
    overall = {0: "healthy", 1: "warnings", 2: "unhealthy"}[worst]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "status": overall,
        "counts": counts,
        "checks": results,
        "fixes": fixes,
        "summary": _summarize(overall, results),
    }


def run_doctor(
    only: list[str] | None = None,
    *,
    config: RuntimeConfig,
    fix: bool = False,
    live: bool = False,
) -> dict[str, Any]:
    """Run the health sweep and return a structured report.

    Args:
        only: restrict to these check names (default: all).
        config: the host's runtime settings, read by the checks that need them.
        fix: run conservative auto-remediations for ``auto_fixable`` checks, then
            re-run those checks.
        live: prove network and engine capabilities for live-aware checks.
    """
    if only is None:
        names = list(CHECKS)
    elif _selection_is_invalid(only):
        return _unhealthy_report(
            [
                _result(
                    "selection",
                    "error",
                    "doctor check selection is invalid",
                    remediation="select one or more registered doctor checks",
                    data={"redacted": True},
                )
            ]
        )
    else:
        names = only
    load_failures = _load_runtime_config(names)
    if load_failures is not None:
        return _unhealthy_report(load_failures)
    results = _run_checks(names, config, live=live)
    fixes = _apply_auto_fixes(results, config) if fix else []
    return _doctor_report(results, fixes)


def _summarize(overall: str, results: list[dict[str, Any]]) -> str:
    bad = [r["name"] for r in results if r["status"] in ("warn", "fail", "error")]
    if overall == "healthy":
        return "All checks passed."
    return f"{overall}: attend to {bad}. Each failing check lists a remediation/skill."


def _format_prescription(check: dict[str, Any]) -> str:
    """Render one not-ok check's plan for operator review before any confirmation prompt."""
    prescription = check.get("prescription") or {}
    lines = [
        f"  service: {check['name']} ({check['status']})",
        f"  detail : {check['detail']}",
    ]
    manifest_path = prescription.get("manifest_path")
    if manifest_path:
        lines.append(f"  manifest: {manifest_path}")
    config_keys = prescription.get("config_keys") or {}
    if config_keys:
        lines.append("  config keys:")
        for key, value in config_keys.items():
            lines.append(f"    {key} = {value}")
    gotcha = prescription.get("gotcha")
    if gotcha:
        lines.append(f"  known gotcha: {gotcha}")
    scaling = prescription.get("scaling") or {}
    if scaling:
        if scaling.get("supported"):
            lines.append("  scaling: supported live via a fleet tool call")
        else:
            lines.append(f"  scaling: not supported -- {scaling.get('reason', '')}")
    return "\n".join(lines)


def _rerun_check_proof(name: str, config: RuntimeConfig) -> dict[str, Any] | None:
    """Re-run one check to prove an applied remediation, live where supported.

    ``None`` only when the name is not a registered check; a re-run that raises
    becomes an ``error`` result, never silent success.
    """
    rerun = CHECKS.get(name)
    if rerun is None:
        return None
    try:
        return _call_check(name, rerun, config, True)
    except PROBE_FAULTS as exc:
        return _result(name, "error", f"re-run failed ({type(exc).__name__})")


def _interactive_outcome(
    config: RuntimeConfig,
    check: dict[str, Any],
    confirm: Callable[[str], bool],
    executor: Callable[[dict[str, Any]], dict[str, Any]] | None,
    output: Callable[[str], None],
) -> dict[str, Any]:
    """Show one check's prescription, ask, and only then possibly apply it.

    Nothing is applied unless ``confirm`` returned ``True`` AND an ``executor``
    was supplied; every other path records a reason and applies nothing.
    """
    output(_format_prescription(check))
    approved = bool(confirm(f"Apply the remediation for {check['name']!r}? [y/N]: "))
    outcome: dict[str, Any] = {
        "name": check["name"],
        "confirmed": approved,
        "applied": False,
    }
    if not approved:
        outcome["reason"] = "not confirmed"
        return outcome
    if executor is None:
        outcome["reason"] = (
            "PLAN-ONLY: no executor configured -- hand this prescription to an "
            "operator or a reviewed `graph_orchestrate action=execute_agent` run"
        )
        return outcome
    try:
        exec_result = executor(check)
    except PROBE_FAULTS as exc:  # interactive_apply is a defensive boundary
        outcome["reason"] = f"executor failed ({type(exc).__name__})"
        return outcome
    outcome["applied"] = bool(exec_result.get("applied"))
    outcome["executor_result"] = exec_result
    if outcome["applied"]:
        proof = _rerun_check_proof(check["name"], config)
        if proof is not None:
            outcome["proof"] = proof
    return outcome


def interactive_apply(
    report: dict[str, Any],
    *,
    config: RuntimeConfig,
    confirm: Callable[[str], bool] | None = None,
    executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    output: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Walk the operator through applying each not-ok check's prescription.

    SAFETY-CRITICAL DIRECTION: nothing is ever applied without an explicit
    ``True`` from ``confirm``. The default ``confirm`` refuses everything --
    calling this with no arguments over a report full of failing checks is
    always a no-op. This is "default to dry-run" made structural, not just
    documented.

    ``executor`` is the only thing that can make this function mutate a live
    deployment. When it is ``None`` (the default) a confirmed step still never
    calls a fleet tool: it records that the plan is PLAN-ONLY and must be
    handed to an operator or a reviewed ``graph_orchestrate
    action=execute_agent`` run. Arbora has no generic manifest-apply backend;
    deployment effects require an explicitly injected operator executor.
    When an ``executor`` genuinely applies a
    step (``exec_result["applied"]`` is truthy), the underlying check is
    re-run — live, when it supports ``live=`` — to prove the result rather
    than trusting the executor's say-so.
    """

    def _refuse(_prompt: str) -> bool:
        return False

    confirm = confirm or _refuse
    outcomes: list[dict[str, Any]] = []
    for check in report.get("checks", []):
        if check["status"] not in ("warn", "fail"):
            continue
        if not check.get("prescription"):
            continue
        outcomes.append(_interactive_outcome(config, check, confirm, executor, output))
    return outcomes


def _doctor_arg_parser() -> Any:
    """The ``arbora-doctor`` console argument parser."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="arbora-doctor",
        description="Holistic health sweep of a meristem deployment.",
    )
    parser.add_argument("--only", nargs="*", choices=list(CHECKS), default=None)
    parser.add_argument(
        "--fix", action="store_true", help="Run safe auto-remediations."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Prove live graph connections, MCP, Langfuse, and native "
            "ProgramOptimize capabilities."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text."
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Run the host DEPENDENCY preflight (runtimes/tools) instead of the deployment sweep.",
    )
    parser.add_argument(
        "--profile",
        default="tiny",
        help="Deployment profile for --preflight (tiny | single-node-prod | enterprise).",
    )
    parser.add_argument(
        "--component",
        dest="components",
        action="append",
        default=None,
        help="UI component to preflight (repeatable): overstory | geniusbot | agent-terminal-ui.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "After the sweep, walk through each not-ok check's prescription "
            "(manifest + config keys + gotcha) and ask before doing anything. "
            "Never applies without explicit confirmation; a non-interactive "
            "terminal always declines."
        ),
    )
    return parser


def _run_preflight_cli(args: Any) -> int:
    """``--preflight``: the host DEPENDENCY sweep instead of the deployment one."""
    import json

    from .preflight import run_preflight

    report = run_preflight(args.profile, args.components)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report, title="meristem preflight")
    return 0 if report["status"] != "blocked" else 1


def _run_interactive_cli(report: dict[str, Any], config: RuntimeConfig) -> None:
    """``--interactive``: walk the prescriptions; a non-tty always declines."""
    import sys

    def _confirm(prompt: str) -> bool:
        if not sys.stdin.isatty():
            print(f"{prompt} (non-interactive terminal — declining)")
            return False
        return input(prompt).strip().lower() in ("y", "yes")

    outcomes = interactive_apply(report, config=config, confirm=_confirm)
    if not outcomes:
        return
    print("\ninteractive apply summary:")
    for outcome in outcomes:
        line = (
            f"  {outcome['name']}: confirmed={outcome['confirmed']} "
            f"applied={outcome['applied']}"
        )
        if outcome.get("reason"):
            line += f" — {outcome['reason']}"
        print(line)


def main(argv: list[str] | None = None) -> int:
    """``arbora-doctor`` console entry."""
    import json

    args = _doctor_arg_parser().parse_args(argv)
    if args.preflight:
        return _run_preflight_cli(args)

    config = RuntimeConfig()
    report = run_doctor(args.only, config=config, fix=args.fix, live=args.live)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)
    if args.interactive:
        _run_interactive_cli(report, config)
    return 0 if report["status"] != "unhealthy" else 1


def _print_human(report: dict[str, Any], title: str = "arbora-doctor") -> None:
    glyph = {"ok": "✓", "warn": "!", "fail": "✗", "error": "✗", "skip": "·"}
    print(f"{title} — {report['status'].upper()}\n")
    for r in report["checks"]:
        line = f"  {glyph.get(r['status'], '?')} {r['name']:<14} {r['detail']}"
        print(line)
        if r["status"] in ("warn", "fail", "error"):
            if r.get("remediation"):
                print(f"      → fix: {r['remediation']}")
            if r.get("skill"):
                print(f"      → skill: {r['skill']}")
    print(f"\n{report['summary']}")


if __name__ == "__main__":
    import sys

    sys.exit(main())
