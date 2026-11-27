"""Render the Arbora production topology from measured, retained authorities.

The renderer is intentionally dependency-free and side-effect free with
respect to Kubernetes. JSON is the authority input; the output is ordinary
Kubernetes YAML. Missing measurements, mutable images, unverified engine
discovery, local session state, or unbounded scaling fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse


class InputError(ValueError):
    """A fail-closed topology input error."""


_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_DNS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_HOST_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")
_METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{2,127}$")
_TARGET_RE = re.compile(r"^[0-9]+(?:m)?$")
_ENGINE_CONTRACT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXPECTED_METRICS = MappingProxyType(
    {
        "gateway": "arbora_gateway_in_flight_requests",
        "dispatch": "arbora_dispatch_queue_depth",
        "ingest": "arbora_kg_ingest_queue_depth",
    }
)


def _require(condition: object, message: str) -> None:
    if not condition:
        raise InputError(message)


def _closed(value: Any, field: str, allowed: set[str]) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{field} must be an object")
    unknown = sorted(set(value) - allowed)
    _require(not unknown, f"{field} has unknown fields: {', '.join(unknown)}")
    return value


def _string(value: Any, field: str) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()),
        f"{field} must be a non-empty string",
    )
    return value


def _positive(value: Any, field: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value > 0,
        f"{field} must be a positive integer",
    )
    return value


def _dns(value: Any, field: str) -> str:
    value = _string(value, field)
    _require(_DNS_RE.fullmatch(value) is not None, f"{field} must be a DNS label")
    return value


def _safe_ref(value: Any, field: str) -> str:
    value = _string(value, field)
    _require(
        not any(
            secret in value.lower()
            for secret in ("password", "token", "secret", "bearer")
        ),
        f"{field} must not contain secret material",
    )
    return value


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read JSON input {path}: {exc}") from exc
    _require(isinstance(value, dict), "top-level input must be an object")
    return value


def _canonical_engine_contract_digest(contract: dict[str, Any]) -> str:
    payload = {key: value for key, value in contract.items() if key != "digest"}
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _validate_engine_contract(value: Any) -> dict[str, Any]:
    contract = _closed(
        value,
        "engine_identity_contract",
        {
            "$schema",
            "apiVersion",
            "kind",
            "authority",
            "contract_version",
            "namespace",
            "statefulset_name",
            "peer_service_name",
            "client_service_name",
            "peer_port",
            "client_port",
            "metrics_port",
            "tls_server_name",
            "tls_sans",
            "discovery_identity",
            "transport",
            "compatibility_aliases",
            "digest",
        },
    )
    _require(
        contract.get("apiVersion") == "arbora.io/engine-identity/v1",
        "engine identity apiVersion is not supported",
    )
    _string(contract.get("$schema"), "engine_identity_contract.$schema")
    _require(
        contract.get("kind") == "EngineIdentityContract",
        "engine identity kind is not supported",
    )
    authority = _string(contract.get("authority"), "engine_identity_contract.authority")
    _require(
        contract.get("contract_version") == "engine-identity.v1",
        "engine identity contract version is not supported",
    )
    digest = _string(contract.get("digest"), "engine_identity_contract.digest")
    _require(
        _ENGINE_CONTRACT_DIGEST_RE.fullmatch(digest) is not None,
        "engine identity digest must be sha256",
    )
    _require(
        digest == _canonical_engine_contract_digest(contract),
        "engine identity digest does not match canonical content",
    )
    namespace = _dns(contract.get("namespace"), "engine_identity_contract.namespace")
    statefulset = _dns(
        contract.get("statefulset_name"), "engine_identity_contract.statefulset_name"
    )
    peer = _dns(
        contract.get("peer_service_name"), "engine_identity_contract.peer_service_name"
    )
    client = _dns(
        contract.get("client_service_name"),
        "engine_identity_contract.client_service_name",
    )
    peer_port = _positive(
        contract.get("peer_port"), "engine_identity_contract.peer_port"
    )
    client_port = _positive(
        contract.get("client_port"), "engine_identity_contract.client_port"
    )
    metrics_port = _positive(
        contract.get("metrics_port"), "engine_identity_contract.metrics_port"
    )
    _require(
        {
            "namespace": namespace,
            "statefulset_name": statefulset,
            "peer_service_name": peer,
            "client_service_name": client,
            "peer_port": peer_port,
            "client_port": client_port,
            "metrics_port": metrics_port,
        }
        == {
            "namespace": "arbora-cell",
            "statefulset_name": "epistemic-graph-raft",
            "peer_service_name": "epistemic-graph-raft",
            "client_service_name": "epistemic-graph-coordinator",
            "peer_port": 9100,
            "client_port": 9101,
            "metrics_port": 9102,
        },
        "engine identity contract does not contain the canonical production values",
    )
    _require(
        len({peer_port, client_port, metrics_port}) == 3,
        "engine identity ports must be distinct",
    )
    _require(
        all(port <= 65535 for port in (peer_port, client_port, metrics_port)),
        "engine identity ports must be <= 65535",
    )
    tls_name = _string(
        contract.get("tls_server_name"), "engine_identity_contract.tls_server_name"
    )
    _require(
        tls_name == f"{peer}.{namespace}.svc.cluster.local",
        "engine identity TLS name must identify the canonical peer Service",
    )
    sans = contract.get("tls_sans")
    _require(
        isinstance(sans, list) and sans and all(isinstance(item, str) for item in sans),
        "engine identity tls_sans must be a non-empty string list",
    )
    assert isinstance(sans, list)
    _require(
        tls_name in sans and len(sans) == len(set(sans)),
        "engine identity tls_sans must contain the canonical name without duplicates",
    )
    _require(
        contract.get("discovery_identity") == tls_name,
        "engine identity discovery identity must equal TLS server name",
    )
    _require(
        contract.get("transport") == "length-prefixed-msgpack-tls",
        "engine identity transport is not supported",
    )
    aliases = contract.get("compatibility_aliases")
    _require(
        isinstance(aliases, list) and aliases,
        "engine identity compatibility aliases must be explicit",
    )
    assert isinstance(aliases, list)
    for index, alias in enumerate(aliases):
        alias = _closed(
            alias,
            f"engine_identity_contract.compatibility_aliases[{index}]",
            {
                "peer_service_name",
                "client_service_name",
                "namespace",
                "status",
                "live_authority",
            },
        )
        _require(
            alias.get("status") == "migration-only"
            and alias.get("live_authority") is False,
            "engine identity aliases cannot be live authority",
        )
        _require(
            (
                alias.get("peer_service_name"),
                alias.get("client_service_name"),
                alias.get("namespace"),
            )
            != (peer, client, namespace),
            "engine identity alias must differ from canonical identity",
        )
    return {
        "ref": authority,
        "digest": digest,
        "namespace": namespace,
        "statefulset_name": statefulset,
        "peer_service_name": peer,
        "client_service_name": client,
        "peer_port": peer_port,
        "client_port": client_port,
        "metrics_port": metrics_port,
        "tls_server_name": tls_name,
        "discovery_identity": tls_name,
    }


def _validate_engine_binding(value: Any, contract: dict[str, Any]) -> None:
    binding = _closed(value, "engine_identity_contract_binding", {"ref", "digest"})
    _require(
        binding.get("ref") == contract["ref"],
        "engine identity binding ref does not match the canonical contract",
    )
    _require(
        binding.get("digest") == contract["digest"],
        "engine identity binding digest does not match the canonical contract",
    )


def _validate_metric(metric: Any, workload: str) -> dict[str, Any]:
    metric = _closed(
        metric,
        f"workloads.{workload}.metric",
        {"mode", "name", "target", "adapter_ref", "reason"},
    )
    mode = _string(metric.get("mode"), f"workloads.{workload}.metric.mode")
    if mode == "fixed":
        _require(
            workload == "mining",
            "only mining may use fixed scaling until a metric is published",
        )
        return {
            "mode": mode,
            "reason": _string(
                metric.get("reason"), f"workloads.{workload}.metric.reason"
            ),
        }
    _require(mode == "hpa", f"workloads.{workload}.metric.mode must be hpa or fixed")
    name = _string(metric.get("name"), f"workloads.{workload}.metric.name")
    _require(
        _METRIC_RE.fullmatch(name) is not None,
        f"workloads.{workload}.metric.name is not a safe metric family",
    )
    _require(
        name == _EXPECTED_METRICS[workload],
        f"workloads.{workload} must use the governed metric {_EXPECTED_METRICS[workload]!r}",
    )
    target = _string(metric.get("target"), f"workloads.{workload}.metric.target")
    _require(
        _TARGET_RE.fullmatch(target) is not None,
        f"workloads.{workload}.metric.target must be a bounded numeric quantity",
    )
    return {
        "mode": mode,
        "name": name,
        "target": target,
        "adapter_ref": _safe_ref(
            metric.get("adapter_ref"), f"workloads.{workload}.metric.adapter_ref"
        ),
    }


def validate(
    document: dict[str, Any], *, engine_identity_contract: dict[str, Any]
) -> dict[str, Any]:
    """Validate and normalize all capacity, identity, and authority inputs."""

    _closed(
        document,
        "input",
        {
            "$schema",
            "apiVersion",
            "kind",
            "authority",
            "environment",
            "engine_identity_contract",
            "namespace",
            "resource_pool",
            "engine",
            "oidc",
            "authorities",
            "release",
            "workloads",
        },
    )
    for field, expected in {
        "apiVersion": "arbora.services/v1",
        "kind": "ArboraProductionTopologyInput",
        "environment": "production",
    }.items():
        _require(document.get(field) == expected, f"{field} must be {expected!r}")
    authority = _string(document.get("authority"), "authority")
    contract = _validate_engine_contract(engine_identity_contract)
    _validate_engine_binding(document.get("engine_identity_contract"), contract)
    namespace = _dns(document.get("namespace"), "namespace")

    pool = _closed(
        document.get("resource_pool"),
        "resource_pool",
        {"id", "measured_at", "evidence_ref", "nodes"},
    )
    pool_id = _string(pool.get("id"), "resource_pool.id")
    measured_at = _string(pool.get("measured_at"), "resource_pool.measured_at")
    _require(
        _RFC3339_RE.fullmatch(measured_at) is not None,
        "resource_pool.measured_at must be RFC3339",
    )
    pool_evidence = _safe_ref(pool.get("evidence_ref"), "resource_pool.evidence_ref")
    raw_nodes = pool.get("nodes")
    _require(
        isinstance(raw_nodes, list) and raw_nodes,
        "resource_pool.nodes must be non-empty",
    )
    assert isinstance(raw_nodes, list)
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordinals: set[int] = set()
    zones: set[str] = set()
    for index, raw in enumerate(raw_nodes):
        raw = _closed(
            raw,
            f"resource_pool.nodes[{index}]",
            {"ordinal", "id", "hostname", "zone", "allocatable"},
        )
        ordinal = raw.get("ordinal")
        _require(
            isinstance(ordinal, int) and not isinstance(ordinal, bool) and ordinal >= 0,
            f"resource_pool.nodes[{index}].ordinal must be non-negative",
        )
        assert isinstance(ordinal, int)
        node_id = _string(raw.get("id"), f"resource_pool.nodes[{index}].id")
        hostname = _string(
            raw.get("hostname"), f"resource_pool.nodes[{index}].hostname"
        )
        zone = _string(raw.get("zone"), f"resource_pool.nodes[{index}].zone")
        _require(
            _HOST_RE.fullmatch(hostname) is not None,
            f"resource_pool.nodes[{index}].hostname is unsafe",
        )
        _require(
            node_id not in seen and hostname not in seen and ordinal not in ordinals,
            f"duplicate node identity at resource_pool.nodes[{index}]",
        )
        alloc = _closed(
            raw.get("allocatable"),
            f"resource_pool.nodes[{index}].allocatable",
            {"cpu_millicores", "memory_bytes"},
        )
        node: dict[str, Any] = {
            "ordinal": ordinal,
            "id": node_id,
            "hostname": hostname,
            "zone": zone,
            "cpu": _positive(
                alloc.get("cpu_millicores"),
                f"resource_pool.nodes[{index}].allocatable.cpu_millicores",
            ),
            "memory": _positive(
                alloc.get("memory_bytes"),
                f"resource_pool.nodes[{index}].allocatable.memory_bytes",
            ),
        }
        nodes.append(node)
        seen.update({node_id, hostname})
        ordinals.add(ordinal)
        zones.add(zone)
    nodes.sort(key=lambda node: node["ordinal"])
    _require(
        [node["ordinal"] for node in nodes] == list(range(len(nodes))),
        "resource_pool.node ordinals must be contiguous",
    )
    _require(zones, "resource_pool must expose at least one failure domain")

    engine = _closed(
        document.get("engine"),
        "engine",
        {
            "client_service_name",
            "peer_service_name",
            "peer_port",
            "client_port",
            "metrics_port",
            "tls_server_name",
            "tls_verified",
            "ca_bundle_configmap",
            "ca_bundle_path",
            "discovery_ref",
            "discovery_verified",
        },
    )
    engine_client = _dns(
        engine.get("client_service_name"), "engine.client_service_name"
    )
    engine_peer = _dns(engine.get("peer_service_name"), "engine.peer_service_name")
    _require(
        engine_client != engine_peer,
        "engine client and peer Service identities must differ",
    )
    engine_peer_port = _positive(engine.get("peer_port"), "engine.peer_port")
    engine_port = _positive(engine.get("client_port"), "engine.client_port")
    engine_metrics_port = _positive(engine.get("metrics_port"), "engine.metrics_port")
    _require(
        all(
            port <= 65535
            for port in (engine_peer_port, engine_port, engine_metrics_port)
        ),
        "engine ports must be <= 65535",
    )
    _require(
        len({engine_peer_port, engine_port, engine_metrics_port}) == 3,
        "engine ports must be distinct",
    )
    tls_server_name = _string(engine.get("tls_server_name"), "engine.tls_server_name")
    _require(
        tls_server_name == contract["tls_server_name"],
        "engine.tls_server_name must match the canonical engine identity contract",
    )
    _require(
        engine_client == contract["client_service_name"],
        "engine.client_service_name disagrees with the canonical engine identity contract",
    )
    _require(
        engine_peer == contract["peer_service_name"],
        "engine.peer_service_name disagrees with the canonical engine identity contract",
    )
    _require(
        engine_peer_port == contract["peer_port"],
        "engine.peer_port disagrees with the canonical engine identity contract",
    )
    _require(
        engine_port == contract["client_port"],
        "engine.client_port disagrees with the canonical engine identity contract",
    )
    _require(
        engine_metrics_port == contract["metrics_port"],
        "engine.metrics_port disagrees with the canonical engine identity contract",
    )
    _require(
        engine.get("tls_verified") is True,
        "engine.tls_verified must be true after the TLS preflight",
    )
    ca_bundle_configmap = _dns(
        engine.get("ca_bundle_configmap"), "engine.ca_bundle_configmap"
    )
    ca_bundle_path = _string(engine.get("ca_bundle_path"), "engine.ca_bundle_path")
    _require(
        ca_bundle_path.startswith("/") and " " not in ca_bundle_path,
        "engine.ca_bundle_path must be an absolute path",
    )
    _require(
        PurePosixPath(ca_bundle_path).name == "ca-bundle.pem",
        "engine.ca_bundle_path must name the ca-bundle.pem file",
    )
    engine_discovery = _safe_ref(engine.get("discovery_ref"), "engine.discovery_ref")
    _require(
        engine.get("discovery_verified") is True,
        "engine.discovery_verified must be true after the discovery preflight",
    )

    oidc = _closed(
        document.get("oidc"),
        "oidc",
        {"issuer", "audience", "jwks_url", "client_id", "verified"},
    )
    issuer = _string(oidc.get("issuer"), "oidc.issuer")
    jwks_url = _string(oidc.get("jwks_url"), "oidc.jwks_url")
    parsed_issuer = urlparse(issuer)
    parsed_jwks = urlparse(jwks_url)
    _require(
        parsed_issuer.scheme == "https" and parsed_issuer.netloc,
        "oidc.issuer must be an HTTPS URL",
    )
    _require(
        parsed_jwks.scheme == "https" and parsed_jwks.netloc == parsed_issuer.netloc,
        "oidc.jwks_url must use the issuer's HTTPS authority",
    )
    oidc_audience = _string(oidc.get("audience"), "oidc.audience")
    oidc_client_id = _string(oidc.get("client_id"), "oidc.client_id")
    _require(
        oidc.get("verified") is True,
        "oidc.verified must be true after the OIDC discovery preflight",
    )

    authorities = _closed(
        document.get("authorities"),
        "authorities",
        {
            "configmap_name",
            "configmap_retention",
            "configmap_ref",
            "secret_name",
            "secret_retention",
            "secret_ref",
            "session_store",
        },
    )
    configmap_name = _dns(
        authorities.get("configmap_name"), "authorities.configmap_name"
    )
    _require(
        authorities.get("configmap_retention") == "Retain",
        "configmap authority retention must be Retain",
    )
    configmap_ref = _safe_ref(
        authorities.get("configmap_ref"), "authorities.configmap_ref"
    )
    secret_name = _dns(authorities.get("secret_name"), "authorities.secret_name")
    _require(
        authorities.get("secret_retention") == "Retain",
        "secret authority retention must be Retain",
    )
    secret_ref = _safe_ref(authorities.get("secret_ref"), "authorities.secret_ref")
    session = _closed(
        authorities.get("session_store"),
        "authorities.session_store",
        {"kind", "service_name", "namespace", "authority_ref", "retention"},
    )
    _require(
        session.get("kind") == "external",
        "Arbora production requires an external session store authority",
    )
    session_service = _dns(
        session.get("service_name"), "authorities.session_store.service_name"
    )
    session_namespace = _dns(
        session.get("namespace"), "authorities.session_store.namespace"
    )
    session_ref = _safe_ref(
        session.get("authority_ref"), "authorities.session_store.authority_ref"
    )
    _require(
        session.get("retention") == "Retain", "session store retention must be Retain"
    )

    release = _closed(
        document.get("release"),
        "release",
        {"rollout_evidence_ref", "rollback_evidence_ref"},
    )
    rollout_evidence = _safe_ref(
        release.get("rollout_evidence_ref"), "release.rollout_evidence_ref"
    )
    rollback_evidence = _safe_ref(
        release.get("rollback_evidence_ref"), "release.rollback_evidence_ref"
    )

    workloads = _closed(
        document.get("workloads"),
        "workloads",
        {"gateway", "dispatch", "ingest", "mining"},
    )
    normalized: dict[str, dict[str, Any]] = {}
    names: set[str] = set()
    total_cpu = 0
    total_cpu_limits = 0
    total_memory = 0
    total_memory_limits = 0
    for workload_key in ("gateway", "dispatch", "ingest", "mining"):
        raw = _closed(
            workloads.get(workload_key),
            f"workloads.{workload_key}",
            {
                "name",
                "entrypoint",
                "image",
                "previous_image",
                "replicas",
                "resources",
                "pdb_max_unavailable",
                "termination_grace_seconds",
                "pre_stop_seconds",
                "metric",
                "service_port",
                "public_host",
                "public_tls_secret",
            },
        )
        name = _dns(raw.get("name"), f"workloads.{workload_key}.name")
        _require(name not in names, f"duplicate workload name: {name}")
        names.add(name)
        entrypoint = _string(
            raw.get("entrypoint"), f"workloads.{workload_key}.entrypoint"
        )
        _require(
            _TOKEN_RE.fullmatch(entrypoint) is not None,
            f"workloads.{workload_key}.entrypoint must be one executable token",
        )
        image = _string(raw.get("image"), f"workloads.{workload_key}.image")
        _require(
            _DIGEST_RE.fullmatch(image) is not None,
            f"workloads.{workload_key}.image must be an immutable @sha256 digest",
        )
        previous_image = _string(
            raw.get("previous_image"), f"workloads.{workload_key}.previous_image"
        )
        _require(
            _DIGEST_RE.fullmatch(previous_image) is not None,
            f"workloads.{workload_key}.previous_image must be an immutable @sha256 digest",
        )
        replicas = _closed(
            raw.get("replicas"),
            f"workloads.{workload_key}.replicas",
            {
                "min",
                "desired",
                "max",
                "scale_up_step",
                "scale_down_step",
                "up_stabilization_seconds",
                "down_stabilization_seconds",
            },
        )
        minimum = _positive(
            replicas.get("min"), f"workloads.{workload_key}.replicas.min"
        )
        desired = _positive(
            replicas.get("desired"), f"workloads.{workload_key}.replicas.desired"
        )
        maximum = _positive(
            replicas.get("max"), f"workloads.{workload_key}.replicas.max"
        )
        _require(
            minimum <= desired <= maximum,
            f"workloads.{workload_key}.replicas must satisfy min <= desired <= max",
        )
        _require(
            maximum <= len(zones),
            f"workloads.{workload_key}.replicas.max exceeds measured failure-domain count",
        )
        scale_up = _positive(
            replicas.get("scale_up_step"),
            f"workloads.{workload_key}.replicas.scale_up_step",
        )
        scale_down = _positive(
            replicas.get("scale_down_step"),
            f"workloads.{workload_key}.replicas.scale_down_step",
        )
        up_stabilization = replicas.get("up_stabilization_seconds")
        down_stabilization = replicas.get("down_stabilization_seconds")
        _require(
            isinstance(up_stabilization, int) and up_stabilization >= 0,
            f"workloads.{workload_key}.replicas.up_stabilization_seconds must be non-negative",
        )
        _require(
            isinstance(down_stabilization, int) and down_stabilization >= 0,
            f"workloads.{workload_key}.replicas.down_stabilization_seconds must be non-negative",
        )
        resources = _closed(
            raw.get("resources"),
            f"workloads.{workload_key}.resources",
            {
                "cpu_request_millicores",
                "cpu_limit_millicores",
                "memory_request_bytes",
                "memory_limit_bytes",
            },
        )
        cpu_request = _positive(
            resources.get("cpu_request_millicores"),
            f"workloads.{workload_key}.resources.cpu_request_millicores",
        )
        cpu_limit = _positive(
            resources.get("cpu_limit_millicores"),
            f"workloads.{workload_key}.resources.cpu_limit_millicores",
        )
        memory_request = _positive(
            resources.get("memory_request_bytes"),
            f"workloads.{workload_key}.resources.memory_request_bytes",
        )
        memory_limit = _positive(
            resources.get("memory_limit_bytes"),
            f"workloads.{workload_key}.resources.memory_limit_bytes",
        )
        _require(
            cpu_request <= cpu_limit and memory_request <= memory_limit,
            f"workloads.{workload_key}.resource request must not exceed limit",
        )
        for node in nodes:
            _require(
                node["cpu"] >= cpu_limit and node["memory"] >= memory_limit,
                f"measured node capacity cannot host {workload_key} limit",
            )
        total_cpu += maximum * cpu_request
        total_cpu_limits += maximum * cpu_limit
        total_memory += maximum * memory_request
        total_memory_limits += maximum * memory_limit
        pdb_max_unavailable = raw.get("pdb_max_unavailable")
        _require(
            isinstance(pdb_max_unavailable, int) and 0 <= pdb_max_unavailable < minimum,
            f"workloads.{workload_key}.pdb_max_unavailable must leave one minimum replica available",
        )
        assert isinstance(pdb_max_unavailable, int)
        termination = _positive(
            raw.get("termination_grace_seconds"),
            f"workloads.{workload_key}.termination_grace_seconds",
        )
        pre_stop = raw.get("pre_stop_seconds")
        _require(
            isinstance(pre_stop, int) and 0 <= pre_stop < termination,
            f"workloads.{workload_key}.pre_stop_seconds must be below termination grace",
        )
        metric = _validate_metric(raw.get("metric"), workload_key)
        item: dict[str, Any] = {
            "name": name,
            "entrypoint": entrypoint,
            "image": image,
            "previous_image": previous_image,
            "min": minimum,
            "desired": desired,
            "max": maximum,
            "scale_up": scale_up,
            "scale_down": scale_down,
            "up_stabilization": up_stabilization,
            "down_stabilization": down_stabilization,
            "cpu_request": str(cpu_request),
            "cpu_limit": str(cpu_limit),
            "memory_request": str(memory_request),
            "memory_limit": str(memory_limit),
            "pdb_min_available": minimum - pdb_max_unavailable,
            "termination": termination,
            "pre_stop": pre_stop,
            "metric": metric,
        }
        if workload_key == "gateway":
            service_port = _positive(
                raw.get("service_port"), "workloads.gateway.service_port"
            )
            _require(
                service_port <= 65535, "workloads.gateway.service_port must be <= 65535"
            )
            public_host = _string(
                raw.get("public_host"), "workloads.gateway.public_host"
            )
            _require(
                _HOST_RE.fullmatch(public_host) is not None,
                "workloads.gateway.public_host is unsafe",
            )
            public_tls_secret = _dns(
                raw.get("public_tls_secret"), "workloads.gateway.public_tls_secret"
            )
            item.update(
                {
                    "service_port": service_port,
                    "public_host": public_host,
                    "public_tls_secret": public_tls_secret,
                }
            )
        elif any(
            key in raw for key in ("service_port", "public_host", "public_tls_secret")
        ):
            raise InputError(
                f"workloads.{workload_key} cannot declare gateway-only public fields"
            )
        normalized[workload_key] = item
    _require(
        total_cpu <= sum(node["cpu"] for node in nodes),
        "maximum workload CPU requests exceed measured pool capacity",
    )
    _require(
        total_cpu_limits <= sum(node["cpu"] for node in nodes),
        "maximum workload CPU limits exceed measured pool capacity",
    )
    _require(
        total_memory <= sum(node["memory"] for node in nodes),
        "maximum workload memory requests exceed measured pool capacity",
    )
    _require(
        total_memory_limits <= sum(node["memory"] for node in nodes),
        "maximum workload memory limits exceed measured pool capacity",
    )
    _require(
        normalized["gateway"]["max"] > 1,
        "gateway topology must model a multi-replica-safe API/MCP surface",
    )

    topology = {
        "authority": authority,
        "namespace": namespace,
        "pool_id": pool_id,
        "measured_at": measured_at,
        "pool_evidence": pool_evidence,
        "nodes": nodes,
        "engine_client": engine_client,
        "engine_peer": engine_peer,
        "engine_namespace": contract["namespace"],
        "engine_peer_port": engine_peer_port,
        "engine_port": engine_port,
        "engine_metrics_port": engine_metrics_port,
        "engine_endpoint": f"tls://{engine_client}.{contract['namespace']}.svc.cluster.local:{engine_port}",
        "engine_tls_name": tls_server_name,
        "engine_identity_ref": contract["ref"],
        "engine_identity_digest": contract["digest"],
        "engine_ca_configmap": ca_bundle_configmap,
        "engine_ca_path": ca_bundle_path,
        "engine_discovery": engine_discovery,
        "oidc_issuer": issuer,
        "oidc_audience": oidc_audience,
        "oidc_jwks": jwks_url,
        "oidc_client_id": oidc_client_id,
        "configmap_name": configmap_name,
        "configmap_ref": configmap_ref,
        "secret_name": secret_name,
        "secret_ref": secret_ref,
        "session_service": session_service,
        "session_namespace": session_namespace,
        "session_ref": session_ref,
        "rollout_evidence": rollout_evidence,
        "rollback_evidence": rollback_evidence,
        "workloads": normalized,
    }
    topology["input_digest"] = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return topology


def _q(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _labels(topology: dict[str, Any], workload: str) -> list[str]:
    return [
        'app: "graph-os"',
        f"graphos_workload: {_q(workload)}",
        'managed-by: "arbora-production-topology-renderer"',
    ]


def _block(topology: dict[str, Any], workload: str, indent: int) -> str:
    return "\n".join(f"{' ' * indent}{line}" for line in _labels(topology, workload))


def _common_env(topology: dict[str, Any]) -> str:
    return f"""            - name: HOME
              value: "/tmp"
            - name: ARBORA_CONFIG_DIR
              value: "/tmp/.config/arbora"
            - name: ARBORA_DATA_DIR
              value: "/tmp/.local/share/arbora"
            - name: MCP_CONFIG
              value: "/tmp/.config/arbora/mcp_config.json"
            - name: GRAPH_SERVICE_ENDPOINTS
              value: {_q(topology["engine_endpoint"])}
            - name: ENGINE_CA_BUNDLE
              value: {_q(topology["engine_ca_path"])}
            - name: ENGINE_TLS_SERVER_NAME
              value: {_q(topology["engine_tls_name"])}
            - name: ENGINE_IDENTITY_CONTRACT_REF
              value: {_q(topology["engine_identity_ref"])}
            - name: ENGINE_IDENTITY_CONTRACT_DIGEST
              value: {_q(topology["engine_identity_digest"])}
            - name: ENGINE_NAMESPACE
              value: {_q(topology["engine_namespace"])}
            - name: ENGINE_CLIENT_PORT
              value: {_q(topology["engine_port"])}
            - name: ENGINE_PEER_PORT
              value: {_q(topology["engine_peer_port"])}
            - name: ENGINE_METRICS_PORT
              value: {_q(topology["engine_metrics_port"])}
            - name: ENGINE_DISCOVERY_REF
              value: {_q(topology["engine_discovery"])}
            - name: AUTH_TYPE
              value: "jwt"
            - name: AUTH_JWT_ISSUER
              value: {_q(topology["oidc_issuer"])}
            - name: AUTH_JWT_AUDIENCE
              value: {_q(topology["oidc_audience"])}
            - name: AUTH_JWT_JWKS_URI
              value: {_q(topology["oidc_jwks"])}
            - name: OIDC_ISSUER
              value: {_q(topology["oidc_issuer"])}
            - name: OIDC_AUDIENCE
              value: {_q(topology["oidc_audience"])}
            - name: OIDC_CLIENT_ID
              value: {_q(topology["oidc_client_id"])}
            - name: ARBORA_SESSION_STORE_SERVICE
              value: {_q(topology["session_service"] + "." + topology["session_namespace"] + ".svc.cluster.local")}
            - name: ARBORA_SESSION_STORE_AUTHORITY_REF
              value: {_q(topology["session_ref"])}
"""


def _render(topology: dict[str, Any], *, rollback: bool = False) -> str:
    nodes = topology["nodes"]
    hostnames = ", ".join(_q(node["hostname"]) for node in nodes)
    labels_contract = "\n".join(
        f"    {line}"
        for line in [
            'app: "graph-os"',
            'managed-by: "arbora-production-topology-renderer"',
        ]
    )
    workload_docs: list[str] = []
    for workload_key, item in topology["workloads"].items():
        selected_image = item["previous_image"] if rollback else item["image"]
        workload_docs.append(
            f"  IMAGE_{workload_key.upper()}_DIGEST: {_q(selected_image)}"
        )
        workload_docs.append(
            f"  WORKLOAD_{workload_key.upper()}_NAME: {_q(item['name'])}"
        )
        workload_docs.append(
            f"  WORKLOAD_{workload_key.upper()}_MIN_REPLICAS: {_q(item['min'])}"
        )
        workload_docs.append(
            f"  WORKLOAD_{workload_key.upper()}_DESIRED_REPLICAS: {_q(item['desired'])}"
        )
        workload_docs.append(
            f"  WORKLOAD_{workload_key.upper()}_MAX_REPLICAS: {_q(item['max'])}"
        )
        metric = item["metric"]
        if metric["mode"] == "hpa":
            workload_docs.append(
                f"  METRIC_{workload_key.upper()}_NAME: {_q(metric['name'])}"
            )
            workload_docs.append(
                f"  METRIC_{workload_key.upper()}_ADAPTER_REF: {_q(metric['adapter_ref'])}"
            )
        else:
            workload_docs.append(
                f"  METRIC_{workload_key.upper()}_STATUS: {_q('fixed: ' + metric['reason'])}"
            )
    output = [
        "# GENERATED by k8s/production/render.py from a measured production input.",
        f"# Render mode: {'rollback' if rollback else 'forward'}; not evidence of a lab/live/1M deployment.",
        f"# Input digest: {topology['input_digest']}",
        "---",
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        "  name: graph-os-production-contract",
        f"  namespace: {_q(topology['namespace'])}",
        "  labels:",
        labels_contract,
        "data:",
        f"  AUTHORITY: {_q(topology['authority'])}",
        f"  INPUT_DIGEST: {_q(topology['input_digest'])}",
        f"  RESOURCE_POOL_ID: {_q(topology['pool_id'])}",
        f"  RESOURCE_POOL_MEASURED_AT: {_q(topology['measured_at'])}",
        f"  RESOURCE_POOL_EVIDENCE_REF: {_q(topology['pool_evidence'])}",
        f"  ROLLOUT_EVIDENCE_REF: {_q(topology['rollout_evidence'])}",
        f"  ROLLBACK_EVIDENCE_REF: {_q(topology['rollback_evidence'])}",
        f"  ENGINE_ENDPOINT: {_q(topology['engine_endpoint'])}",
        f"  ENGINE_NAMESPACE: {_q(topology['engine_namespace'])}",
        f"  ENGINE_CLIENT_SERVICE: {_q(topology['engine_client'])}",
        f"  ENGINE_PEER_SERVICE: {_q(topology['engine_peer'])}",
        f"  ENGINE_PEER_PORT: {_q(topology['engine_peer_port'])}",
        f"  ENGINE_CLIENT_PORT: {_q(topology['engine_port'])}",
        f"  ENGINE_METRICS_PORT: {_q(topology['engine_metrics_port'])}",
        f"  ENGINE_IDENTITY_CONTRACT_REF: {_q(topology['engine_identity_ref'])}",
        f"  ENGINE_IDENTITY_CONTRACT_DIGEST: {_q(topology['engine_identity_digest'])}",
        f"  ENGINE_TLS_SERVER_NAME: {_q(topology['engine_tls_name'])}",
        '  ENGINE_TLS_VERIFIED: "true"',
        f"  ENGINE_CA_BUNDLE_CONFIGMAP: {_q(topology['engine_ca_configmap'])}",
        f"  ENGINE_DISCOVERY_REF: {_q(topology['engine_discovery'])}",
        '  ENGINE_DISCOVERY_VERIFIED: "true"',
        f"  OIDC_ISSUER: {_q(topology['oidc_issuer'])}",
        f"  OIDC_AUDIENCE: {_q(topology['oidc_audience'])}",
        f"  OIDC_JWKS_URI: {_q(topology['oidc_jwks'])}",
        f"  OIDC_CLIENT_ID: {_q(topology['oidc_client_id'])}",
        '  OIDC_VERIFIED: "true"',
        f"  CONFIG_AUTHORITY: {_q(topology['configmap_name'])}",
        f"  CONFIG_AUTHORITY_REF: {_q(topology['configmap_ref'])}",
        f"  SECRET_AUTHORITY: {_q(topology['secret_name'])}",
        f"  SECRET_AUTHORITY_REF: {_q(topology['secret_ref'])}",
        f"  SESSION_STORE_SERVICE: {_q(topology['session_service'] + '.' + topology['session_namespace'] + '.svc.cluster.local')}",
        f"  SESSION_STORE_AUTHORITY_REF: {_q(topology['session_ref'])}",
        *workload_docs,
        "---",
        "apiVersion: v1",
        "kind: Service",
        "metadata:",
        f"  name: {_q(topology['workloads']['gateway']['name'])}",
        f"  namespace: {_q(topology['namespace'])}",
        "  labels:",
        _block(topology, "gateway", 4),
        "spec:",
        "  type: ClusterIP",
        "  selector:",
        _block(topology, "gateway", 4),
        "  ports:",
        "    - name: http",
        f"      port: {topology['workloads']['gateway']['service_port']}",
        "      targetPort: http",
        "      protocol: TCP",
        "---",
        "apiVersion: networking.k8s.io/v1",
        "kind: Ingress",
        "metadata:",
        f"  name: {_q(topology['workloads']['gateway']['name'])}",
        f"  namespace: {_q(topology['namespace'])}",
        "  annotations:",
        '    nginx.ingress.kubernetes.io/ssl-redirect: "true"',
        "spec:",
        "  rules:",
        f"    - host: {_q(topology['workloads']['gateway']['public_host'])}",
        "      http:",
        "        paths:",
        "          - path: /",
        "            pathType: Prefix",
        "            backend:",
        "              service:",
        f"                name: {_q(topology['workloads']['gateway']['name'])}",
        "                port:",
        "                  name: http",
        "  tls:",
        f"    - hosts: [{_q(topology['workloads']['gateway']['public_host'])}]",
        f"      secretName: {_q(topology['workloads']['gateway']['public_tls_secret'])}",
    ]

    for workload_key, item in topology["workloads"].items():
        selected_image = item["previous_image"] if rollback else item["image"]
        selector = _block(topology, workload_key, 8)
        nested_selector = _block(topology, workload_key, 18)
        spread_selector = _block(topology, workload_key, 16)
        output.extend(
            [
                "---",
                "apiVersion: policy/v1",
                "kind: PodDisruptionBudget",
                "metadata:",
                f"  name: {_q(item['name'] + '-pdb')}",
                f"  namespace: {_q(topology['namespace'])}",
                "  labels:",
                _block(topology, workload_key, 4),
                "spec:",
                f"  minAvailable: {item['pdb_min_available']}",
                "  selector:",
                "    matchLabels:",
                selector,
                "---",
                "apiVersion: apps/v1",
                "kind: Deployment",
                "metadata:",
                f"  name: {_q(item['name'])}",
                f"  namespace: {_q(topology['namespace'])}",
                "  labels:",
                _block(topology, workload_key, 4),
                "  annotations:",
                f"    arbora.io/topology-input-digest: {_q(topology['input_digest'])}",
                f"    arbora.io/resource-pool-evidence: {_q(topology['pool_evidence'])}",
                f"    arbora.io/rollout-evidence: {_q(topology['rollout_evidence'])}",
                f"    arbora.io/rollback-evidence: {_q(topology['rollback_evidence'])}",
                "spec:",
                f"  replicas: {item['desired']}",
                "  revisionHistoryLimit: 5",
                "  strategy:",
                "    type: RollingUpdate",
                "    rollingUpdate:",
                f"      maxSurge: {item['scale_up']}",
                "      maxUnavailable: 0",
                "  selector:",
                "    matchLabels:",
                selector,
                "  template:",
                "    metadata:",
                "      labels:",
                _block(topology, workload_key, 8),
                "      annotations:",
                f"        arbora.io/topology-input-digest: {_q(topology['input_digest'])}",
                "    spec:",
                "      automountServiceAccountToken: false",
                "      terminationGracePeriodSeconds: " + str(item["termination"]),
                "      securityContext:",
                "        runAsNonRoot: true",
                "        runAsUser: 10001",
                "        runAsGroup: 10001",
                "        fsGroup: 10001",
                "        seccompProfile:",
                "          type: RuntimeDefault",
                "      affinity:",
                "        nodeAffinity:",
                "          requiredDuringSchedulingIgnoredDuringExecution:",
                "            nodeSelectorTerms:",
                "              - matchExpressions:",
                "                  - key: kubernetes.io/hostname",
                "                    operator: In",
                f"                    values: [{hostnames}]",
                "        podAntiAffinity:",
                "          requiredDuringSchedulingIgnoredDuringExecution:",
                "            - labelSelector:",
                "                matchLabels:",
                nested_selector,
                "              topologyKey: kubernetes.io/hostname",
                "            - labelSelector:",
                "                matchLabels:",
                nested_selector,
                "              topologyKey: topology.kubernetes.io/zone",
                "      topologySpreadConstraints:",
                "        - maxSkew: 1",
                "          topologyKey: topology.kubernetes.io/zone",
                "          whenUnsatisfiable: DoNotSchedule",
                "          labelSelector:",
                "            matchLabels:",
                spread_selector,
                "      initContainers:",
                "        - name: config-seed",
                f"          image: {_q(selected_image)}",
                "          imagePullPolicy: IfNotPresent",
                '          command: ["/bin/sh", "-ec"]',
                "          args:",
                "            - |",
                "              set -eu",
                "              cp /config-source/config.json /config/config.json",
                "              cp /config-source/mcp_config.json /config/mcp_config.json",
                "          securityContext:",
                "            allowPrivilegeEscalation: false",
                "            capabilities:",
                '              drop: ["ALL"]',
                "          volumeMounts:",
                "            - name: config-source",
                "              mountPath: /config-source",
                "              readOnly: true",
                "            - name: config",
                "              mountPath: /config",
                "      containers:",
                f"        - name: {_q(item['name'])}",
                f"          image: {_q(selected_image)}",
                "          imagePullPolicy: IfNotPresent",
                "          securityContext:",
                "            allowPrivilegeEscalation: false",
                "            capabilities:",
                '              drop: ["ALL"]',
                f"          command: [{_q(item['entrypoint'])}]",
                "          env:",
                _common_env(topology).rstrip("\n"),
            ]
        )
        if workload_key == "gateway":
            output.extend(
                [
                    "            - name: KG_DAEMON_ROLE",
                    '              value: "client"',
                    "            - name: KG_LOOP",
                    '              value: "0"',
                    "            - name: MESSAGING_INTAKE_ENABLED",
                    '              value: "false"',
                ]
            )
        elif workload_key == "dispatch":
            output.extend(
                [
                    "            - name: KG_DAEMON_ROLE",
                    '              value: "client"',
                    "            - name: AGENT_DISPATCH_BACKEND",
                    '              value: "queue"',
                ]
            )
        elif workload_key == "ingest":
            output.extend(
                [
                    "            - name: KG_DAEMON_ROLE",
                    '              value: "client"',
                    "            - name: TASK_QUEUE_BACKEND",
                    '              value: "kafka"',
                ]
            )
        else:
            output.extend(
                ["            - name: KG_DAEMON_ROLE", '              value: "client"']
            )
        output.extend(
            [
                "          envFrom:",
                "            - configMapRef:",
                "                name: graph-os-production-contract",
                "            - configMapRef:",
                f"                name: {_q(topology['configmap_name'])}",
                "            - secretRef:",
                f"                name: {_q(topology['secret_name'])}",
                "          volumeMounts:",
                "            - name: config",
                "              mountPath: /tmp/.config/arbora",
                "            - name: local-data",
                "              mountPath: /tmp/.local/share/arbora",
                "            - name: ca-bundle",
                f"              mountPath: {_q(str(PurePosixPath(topology['engine_ca_path']).parent))}",
                "              readOnly: true",
            ]
        )
        if workload_key == "gateway":
            output.extend(
                [
                    "          ports:",
                    "            - name: http",
                    f"              containerPort: {item['service_port']}",
                    "              protocol: TCP",
                    "          readinessProbe:",
                    "            httpGet:",
                    "              path: /health/ready",
                    "              port: http",
                    "            periodSeconds: 10",
                    "            failureThreshold: 6",
                    "          livenessProbe:",
                    "            httpGet:",
                    "              path: /health",
                    "              port: http",
                    "            periodSeconds: 20",
                ]
            )
        output.extend(
            [
                "          lifecycle:",
                "            preStop:",
                "              exec:",
                "                command:",
                "                  - /bin/sh",
                "                  - -c",
                f"                  - sleep {item['pre_stop']}",
                "          resources:",
                "            requests:",
                f"              cpu: {_q(item['cpu_request'])}",
                f"              memory: {_q(item['memory_request'])}",
                "            limits:",
                f"              cpu: {_q(item['cpu_limit'])}",
                f"              memory: {_q(item['memory_limit'])}",
                "      volumes:",
                "        - name: config",
                "          emptyDir: {}",
                "        - name: config-source",
                "          configMap:",
                f"            name: {_q(topology['configmap_name'])}",
                "            optional: false",
                "        - name: local-data",
                "          emptyDir: {}",
                "        - name: ca-bundle",
                "          configMap:",
                f"            name: {_q(topology['engine_ca_configmap'])}",
                "            optional: false",
                "            items:",
                "              - key: ca-bundle.pem",
                "                path: ca-bundle.pem",
            ]
        )
        metric = item["metric"]
        if metric["mode"] == "hpa":
            output.extend(
                [
                    "---",
                    "apiVersion: autoscaling/v2",
                    "kind: HorizontalPodAutoscaler",
                    "metadata:",
                    f"  name: {_q(item['name'])}",
                    f"  namespace: {_q(topology['namespace'])}",
                    "  labels:",
                    _block(topology, workload_key, 4),
                    "  annotations:",
                    f"    arbora.io/metric-adapter-ref: {_q(metric['adapter_ref'])}",
                    "spec:",
                    "  scaleTargetRef:",
                    "    apiVersion: apps/v1",
                    "    kind: Deployment",
                    f"    name: {_q(item['name'])}",
                    f"  minReplicas: {item['min']}",
                    f"  maxReplicas: {item['max']}",
                    "  behavior:",
                    "    scaleUp:",
                    "      stabilizationWindowSeconds: "
                    + str(item["up_stabilization"]),
                    "      policies:",
                    f"        - type: Pods\n          value: {item['scale_up']}\n          periodSeconds: 60",
                    "    scaleDown:",
                    "      stabilizationWindowSeconds: "
                    + str(item["down_stabilization"]),
                    "      policies:",
                    f"        - type: Pods\n          value: {item['scale_down']}\n          periodSeconds: 60",
                    "  metrics:",
                    "    - type: External",
                    "      external:",
                    "        metric:",
                    f"          name: {_q(metric['name'])}",
                    "          selector:",
                    "            matchLabels:",
                    f"              graphos_workload: {_q(workload_key)}",
                    "        target:",
                    "          type: AverageValue",
                    f"          averageValue: {_q(metric['target'])}",
                ]
            )
    return "\n".join(output) + "\n"


def render_manifests(
    topology_path: Path, contract_path: Path, *, rollback: bool = False
) -> str:
    """Validate the topology file against the contract file and render it.

    The output is byte-identical for equal input: nothing here reads the clock,
    the environment or the network.
    """
    topology = validate(
        _load(topology_path), engine_identity_contract=_load(contract_path)
    )
    return _render(topology, rollback=rollback)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the flags of ``arbora deploy render`` on a parser."""
    parser.add_argument(
        "--topology", required=True, type=Path, help="topology JSON file"
    )
    parser.add_argument(
        "--contract",
        required=True,
        type=Path,
        help="engine identity contract JSON file",
    )
    parser.add_argument("--out", default="-", help="YAML output path, or - for stdout")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="render each workload's previous immutable image",
    )
    parser.add_argument(
        "--first-start",
        action="store_true",
        help="render the first-start Job derived from the rendered gateway Deployment",
    )
    parser.add_argument(
        "--prerelease-image",
        help="render an isolated Deployment, Job and Service for this candidate image",
    )
    parser.add_argument(
        "--prerelease-name",
        default="arbora-prerelease",
        help="name and label of the isolated set",
    )


def run(args: argparse.Namespace) -> int:
    """Execute ``arbora deploy render``; exit 0, 2 for an invalid topology, 3 for an output failure."""
    try:
        rendered = render_manifests(
            args.topology, args.contract, rollback=args.rollback
        )
        if args.first_start or args.prerelease_image:
            from arbora.deployment import render_job

            if args.prerelease_image:
                rendered = render_job.prerelease_manifests(
                    rendered, image=args.prerelease_image, name=args.prerelease_name
                )
            else:
                rendered = render_job.render_first_start_job(rendered)
        if args.out == "-":
            sys.stdout.write(rendered)
        else:
            Path(args.out).write_text(rendered, encoding="utf-8")
    except InputError as exc:
        print(f"Arbora production topology rejected: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Arbora production topology output failed: {exc}", file=sys.stderr)
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arbora deploy render", description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))
