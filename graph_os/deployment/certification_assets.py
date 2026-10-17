"""Bounded readers and validators for production certification assets.

The host doctor checks that the runtime configuration, the model registry and
the identity authority settings are fit for certification. It reads each asset
from a regular file with a size bound, rejects duplicate JSON keys and derives
the model-registry proof. Certification itself runs outside the host.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

AUTHORITY_MODE = "ephemeral-https-loopback"
DEFAULT_TOKEN_TTL_SECONDS = 300
MIN_TOKEN_TTL_SECONDS = 180
MAX_TOKEN_TTL_SECONDS = 3_600


def validated_token_ttl_seconds(value: int | None) -> int:
    """Return the bounded certification token lifetime."""
    ttl = DEFAULT_TOKEN_TTL_SECONDS if value is None else value
    if (
        isinstance(ttl, bool)
        or not isinstance(ttl, int)
        or not MIN_TOKEN_TTL_SECONDS <= ttl <= MAX_TOKEN_TTL_SECONDS
    ):
        raise ValueError("token lifetime is outside the certification range")
    return ttl


_MODEL_DEFAULTS: dict[str, Any] = {
    "intelligence_level": "normal",
    "base_url": None,
    "api_key_ref": None,
    "oauth2": None,
    "headers_ref": None,
}


def _chat_model(item: Any) -> SimpleNamespace:
    """A read-only view of one model registry entry (``id`` and ``provider`` required)."""
    if not isinstance(item, dict):
        raise TypeError("model entry must be a mapping")
    for key in ("id", "provider"):
        if not isinstance(item.get(key), str) or not item[key]:
            raise ValueError(f"model entry needs {key}")
    return SimpleNamespace(**{**_MODEL_DEFAULTS, **item})


_PRIVATE_MODEL_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class CertificationAssetError(RuntimeError):
    """Path-free, content-free fail-closed certification error."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _stat_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_regular_readonly(path: Path, *, code: str) -> int:
    if not path.is_absolute():
        raise CertificationAssetError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise CertificationAssetError(code) from exc


def _validated_regular_stat(
    descriptor: int, *, limit: int, code: str
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= limit
    ):
        raise CertificationAssetError(code)
    return metadata


def _confirm_path_signature(
    path: Path, signature: tuple[int, int, int, int, int], *, code: str
) -> None:
    try:
        path_metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CertificationAssetError(code) from exc
    if _stat_signature(path_metadata) != signature:
        raise CertificationAssetError(code)


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_regular(path: Path, *, limit: int, code: str) -> bytes:
    descriptor = _open_regular_readonly(path, code=code)
    try:
        metadata = _validated_regular_stat(descriptor, limit=limit, code=code)
        before = _stat_signature(metadata)
        payload = _read_bounded(descriptor, limit)
        after = os.fstat(descriptor)
        if (
            len(payload) != metadata.st_size
            or len(payload) > limit
            or before != _stat_signature(after)
        ):
            raise CertificationAssetError(code)
        _confirm_path_signature(path, before, code=code)
        return payload
    finally:
        os.close(descriptor)


def _json_without_duplicates(payload: bytes, *, code: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise CertificationAssetError(code)
            value[key] = item
        return value

    try:
        return json.loads(payload, object_pairs_hook=pairs)
    except CertificationAssetError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CertificationAssetError(code) from exc


def _single_config_key(
    root: dict[str, Any], candidates: tuple[str, ...], *, code: str
) -> str:
    keys = [key for key in candidates if key in root]
    if len(keys) != 1:
        raise CertificationAssetError(code)
    return keys[0]


def _optional_config_key(
    root: dict[str, Any], candidates: tuple[str, ...], *, code: str
) -> str | None:
    keys = [key for key in candidates if key in root]
    if len(keys) > 1:
        raise CertificationAssetError(code)
    return keys[0] if keys else None


def _validated_model_list(raw_models: Any, raw_hosts: Any) -> None:
    if (
        not isinstance(raw_models, list)
        or not isinstance(raw_hosts, list)
        or any(not isinstance(host, str) for host in raw_hosts)
    ):
        raise CertificationAssetError("runtime_model_registry_invalid")


def _model_configuration(root: Any) -> tuple[list[Any], list[str]]:
    if not isinstance(root, dict):
        raise CertificationAssetError("runtime_configuration_invalid")
    model_key = _single_config_key(
        root, ("CHAT_MODELS", "chat_models"), code="runtime_model_registry_missing"
    )
    host_key = _single_config_key(
        root,
        ("MODEL_HTTP_ALLOWED_PRIVATE_HOSTS", "model_http_allowed_private_hosts"),
        code="runtime_model_registry_missing",
    )
    raw_models = root[model_key]
    raw_hosts = root[host_key]
    _validated_model_list(raw_models, raw_hosts)
    try:
        models = [_chat_model(item) for item in raw_models]
    except Exception as exc:
        raise CertificationAssetError("runtime_model_registry_invalid") from exc
    return models, [str(host) for host in raw_hosts]


def _validated_ttl(ttl: Any) -> int:
    try:
        return validated_token_ttl_seconds(ttl)
    except Exception as exc:
        raise CertificationAssetError("runtime_identity_authority_invalid") from exc


def _identity_authority_configuration(root: Any) -> dict[str, Any]:
    """Resolve the current-only lifecycle authority controls from AgentConfig."""

    if not isinstance(root, dict):
        raise CertificationAssetError("runtime_configuration_invalid")
    mode_key = _optional_config_key(
        root,
        ("SKILL_CERT_IDENTITY_AUTHORITY_MODE", "skill_cert_identity_authority_mode"),
        code="runtime_identity_authority_invalid",
    )
    ttl_key = _optional_config_key(
        root,
        (
            "SKILL_CERT_IDENTITY_TOKEN_TTL_SECONDS",
            "skill_cert_identity_token_ttl_seconds",
        ),
        code="runtime_identity_authority_invalid",
    )
    mode = root[mode_key] if mode_key else AUTHORITY_MODE
    ttl = root[ttl_key] if ttl_key else DEFAULT_TOKEN_TTL_SECONDS
    if mode != AUTHORITY_MODE:
        raise CertificationAssetError("runtime_identity_authority_invalid")
    token_ttl_seconds = _validated_ttl(ttl)
    return {
        "mode": AUTHORITY_MODE,
        "tokenTtlSeconds": token_ttl_seconds,
        "tlsVerificationRequired": True,
        "lifecycleOwned": True,
        "renewableCredentialsRequired": True,
    }


def _validate_model_identity(
    model: Any, model_ids: set[str], counts: dict[str, int]
) -> tuple[str, str]:
    model_id = str(getattr(model, "id", "") or "")
    level = str(getattr(model, "intelligence_level", "") or "").casefold()
    if not model_id or model_id in model_ids or level not in counts:
        raise CertificationAssetError("runtime_model_registry_class_invalid")
    model_ids.add(model_id)
    counts[level] += 1
    return model_id, level


def _is_valid_model_transport(
    parsed: Any, host: str, port: int | None, allowed: set[str]
) -> bool:
    if parsed.scheme.casefold() not in {"http", "https"}:
        return False
    if not host or host not in allowed:
        return False
    if port is not None and not 1 <= port <= 65_535:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    return not (parsed.query or parsed.fragment)


def _validate_model_transport(model: Any, allowed: set[str]) -> tuple[Any, str]:
    try:
        parsed = urlsplit(str(getattr(model, "base_url", "") or ""))
        host = str(parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise CertificationAssetError("runtime_model_transport_invalid") from exc
    if not _is_valid_model_transport(parsed, host, port, allowed):
        raise CertificationAssetError("runtime_model_transport_invalid")
    return parsed, host


def _model_locality(host: str) -> tuple[str, bool, bool]:
    """Return ``(locality label, is_literal_private, is_private_dns)``."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "private-dns-runtime-pinned", False, True
    if address.is_loopback:
        return "loopback", True, False
    if any(address in network for network in _PRIVATE_MODEL_NETWORKS):
        return "private", True, False
    raise CertificationAssetError("runtime_model_locality_unproven")


def _model_auth_modes(model: Any) -> list[str]:
    auth_modes: list[str] = []
    if getattr(model, "api_key_ref", None):
        auth_modes.append("api-key-reference")
    if getattr(model, "oauth2", None):
        auth_modes.append("oauth2-secret-reference")
    if not auth_modes:
        raise CertificationAssetError("runtime_model_credentials_unreferenced")
    if getattr(model, "headers_ref", None):
        auth_modes.append("supplemental-header-reference")
    return auth_modes


def derive_model_registry_proof(
    models: list[Any], allowed_private_hosts: list[str]
) -> dict[str, Any]:
    """Derive a privacy-safe proof for the exact light/normal model registry."""

    allowed = {
        str(host).strip().casefold().rstrip(".") for host in allowed_private_hosts
    }
    if len(models) != 2 or len(allowed) > 256:
        raise CertificationAssetError("runtime_model_registry_cardinality_invalid")
    canonical: list[dict[str, str]] = []
    counts = {"light": 0, "normal": 0}
    literal_private_model_count = 0
    private_dns_model_count = 0
    model_ids: set[str] = set()
    for model in models:
        model_id, level = _validate_model_identity(model, model_ids, counts)
        parsed, host = _validate_model_transport(model, allowed)
        locality, is_literal, is_dns = _model_locality(host)
        literal_private_model_count += int(is_literal)
        private_dns_model_count += int(is_dns)
        auth_modes = _model_auth_modes(model)
        canonical.append(
            {
                "modelIdentityDigest": _digest(model_id.encode("utf-8")),
                "class": level,
                "transport": locality,
                "scheme": parsed.scheme.casefold(),
                "authentication": "+".join(sorted(auth_modes)),
            }
        )
    if counts != {"light": 1, "normal": 1}:
        raise CertificationAssetError("runtime_model_registry_class_invalid")
    return {
        "digest": _digest(
            _canonical_bytes(sorted(canonical, key=lambda item: item["class"]))
        ),
        "modelCount": 2,
        "lightCount": 1,
        "normalCount": 1,
        "localPrivateTransportOnly": True,
        "referenceBackedCredentialsOnly": True,
        "literalPrivateModelCount": literal_private_model_count,
        "privateDnsModelCount": private_dns_model_count,
        "runtimePrivateResolutionRequired": True,
    }


def _validate_profile(
    payload: bytes,
    *,
    configuration_digest: str,
    model_registry_digest: str,
    identity_authority: dict[str, Any],
) -> None:
    value = _json_without_duplicates(payload, code="runtime_profile_invalid")
    expected = {
        "apiVersion": "graphos.io/v2",
        "kind": "SkillValidationRuntimeProfile",
        "configurationDigest": configuration_digest,
        "modelRegistryDigest": model_registry_digest,
        "identityAuthority": identity_authority,
        "engineTopology": "local-autostart",
        "observability": "metadata-only",
        "sequential": True,
    }
    if value != expected:
        raise CertificationAssetError("runtime_profile_invalid")


def _configuration_proof(payload: bytes) -> dict[str, Any]:
    root = _json_without_duplicates(payload, code="runtime_configuration_invalid")
    models, allowed = _model_configuration(root)
    return derive_model_registry_proof(models, allowed)
