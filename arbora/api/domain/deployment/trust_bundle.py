"""Secrets default to the vault; the trust bundle reaches every component (ARBORA-DEPLOY-R013.7).

Two steps of the confirmed deployment live here. ``store-secret`` finds each
secret-shaped value in the resolved configuration, writes it through the vault
fleet tool named by ``tools.secret_write`` and replaces it with an
``openbao://`` reference; Arbora keeps no value. ``propagate-trust`` mounts the
enterprise bundle named by ``trust.bundle_path`` into every deployed component
and checks each with a TLS connection that uses the bundle. Certificate
verification is never disabled.
"""

from __future__ import annotations

import hashlib
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.security.operation_result import OpError

#: A key that names a secret; its string value is never stored inline.
SECRET_KEY = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential)", re.IGNORECASE
)
#: A value that already is a reference is not a secret value.
REFERENCE_SCHEMES = ("env://", "openbao://", "vault://", "file://")
#: The components the bundle must reach, besides every connector.
CORE_COMPONENTS = ("engine", "gateway", "webui", "sdk-transport")
MOUNT_PATH = "/etc/arbora/trust/bundle.pem"
ENV_VAR = "SSL_CERT_FILE"

StepInvoke = Callable[[str, Mapping[str, Any], Any, str], Awaitable[Any]]
TlsConnect = Callable[[str, int, str], None]


@dataclass(frozen=True, slots=True)
class TrustRow:
    """One component's trust mount and its verification result."""

    component: str
    mount_path: str
    env_var: str
    verify: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "mount_path": self.mount_path,
            "env_var": self.env_var,
            "verify": self.verify,
        }


def inline_secrets(config: Mapping[str, Any], _path: str = "") -> Iterator[str]:
    """The dotted paths of secret-shaped string values that are not references."""
    for key, value in config.items():
        path = f"{_path}.{key}" if _path else str(key)
        if isinstance(value, Mapping):
            yield from inline_secrets(value, path)
        elif (
            isinstance(value, str)
            and value
            and SECRET_KEY.search(str(key))
            and not value.startswith(REFERENCE_SCHEMES)
        ):
            yield path


def _reference(path: str) -> str:
    return "openbao://" + path.replace(".", "/")


def _replace(config: Mapping[str, Any], path: str, reference: str) -> dict[str, Any]:
    head, _, rest = path.partition(".")
    out = dict(config)
    out[head] = _replace(config[head], rest, reference) if rest else reference
    return out


def _value_at(config: Mapping[str, Any], path: str) -> str:
    node: Any = config
    for part in path.split("."):
        node = node[part]
    return str(node)


async def store_secrets(
    config: Mapping[str, Any],
    invoke: StepInvoke,
    caller: Any,
    *,
    plan_digest: str,
) -> tuple[dict[str, Any], list[str]]:
    """Write each inline secret to the vault tool and return the referenced config.

    The tool comes from ``tools.secret_write``. Without it an inline secret
    refuses ``SECRET_INLINE_REFUSED`` naming the key (never the value), and
    nothing is written. The value goes only to the vault tool.
    """
    paths = sorted(inline_secrets(config))
    if not paths:
        return dict(config), []
    tool = (config.get("tools") or {}).get("secret_write")
    if not (isinstance(tool, Mapping) and tool.get("server") and tool.get("tool")):
        raise OperationRefused("SECRET_INLINE_REFUSED", {"keys": paths})
    resolved: dict[str, Any] = dict(config)
    for path in paths:
        reference = _reference(path)
        key = f"secret:{plan_digest}:{hashlib.sha256(path.encode()).hexdigest()[:16]}"
        outcome = await invoke(
            "fleet.call",
            {
                "server": tool["server"],
                "tool": tool["tool"],
                "arguments": {"path": reference, "value": _value_at(config, path)},
            },
            caller,
            key,
        )
        if isinstance(outcome, OpError) or getattr(outcome, "code", "OK") != "OK":
            raise OperationRefused("SECRET_INLINE_REFUSED", {"keys": [path]})
        resolved = _replace(resolved, path, reference)
    return resolved, paths


def tls_connect(host: str, port: int, bundle_path: str) -> None:
    """Open a TLS connection that trusts exactly the bundle; verification stays on."""
    context = ssl.create_default_context(cafile=bundle_path)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    with (
        socket.create_connection((host, port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname=host),
    ):
        return


def _endpoint(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host, int(port)


def _trust_inputs(config: Mapping[str, Any]) -> tuple[str, Mapping[str, Any], Any]:
    """The bundle path, mount tool and TLS endpoints, or ``TRUST_BUNDLE_MISSING``."""
    trust = config.get("trust") or {}
    bundle = trust.get("bundle_path")
    if not (isinstance(bundle, str) and bundle and Path(bundle).is_file()):
        raise OperationRefused("TRUST_BUNDLE_MISSING", {"bundle_path": bool(bundle)})
    tool = (config.get("tools") or {}).get("trust_mount")
    if not (isinstance(tool, Mapping) and tool.get("server") and tool.get("tool")):
        raise OperationRefused("TRUST_BUNDLE_MISSING", {"missing": "tools.trust_mount"})
    return bundle, tool, trust.get("endpoints") or {}


def _verify_tls(
    endpoint: object, bundle: str, component: str, connect: TlsConnect | None
) -> None:
    """Open the component's TLS endpoint against the bundle or refuse it by name."""
    try:
        if not endpoint:
            raise ValueError("no endpoint")
        (connect or tls_connect)(*_endpoint(str(endpoint)), bundle)
    except (OSError, ValueError, ssl.SSLError) as error:
        raise OperationRefused(
            "TRUST_VERIFY_FAILED", {"component": component}
        ) from error


async def propagate_trust(
    config: Mapping[str, Any],
    invoke: StepInvoke,
    caller: Any,
    *,
    components: Sequence[str],
    plan_digest: str,
    connect: TlsConnect | None = None,
) -> list[TrustRow]:
    """Mount the bundle into each component and verify each over TLS.

    ``trust.bundle_path`` names the bundle file, ``tools.trust_mount`` the
    fleet tool that mounts it, and ``trust.endpoints`` maps each component to
    the ``host:port`` its TLS check opens. A missing bundle refuses
    ``TRUST_BUNDLE_MISSING`` before any call; a component whose TLS check fails
    refuses ``TRUST_VERIFY_FAILED`` naming it.
    """
    bundle, tool, endpoints = _trust_inputs(config)
    rows: list[TrustRow] = []
    for component in sorted(set(components) | set(CORE_COMPONENTS)):
        outcome = await invoke(
            "fleet.call",
            {
                "server": tool["server"],
                "tool": tool["tool"],
                "arguments": {
                    "component": component,
                    "bundle_path": bundle,
                    "mount_path": MOUNT_PATH,
                    "env_var": ENV_VAR,
                },
            },
            caller,
            f"trust:{plan_digest}:{component}",
        )
        if isinstance(outcome, OpError) or getattr(outcome, "code", "OK") != "OK":
            raise OperationRefused("TRUST_VERIFY_FAILED", {"component": component})
        _verify_tls(endpoints.get(component), bundle, component, connect)
        rows.append(TrustRow(component, MOUNT_PATH, ENV_VAR, True))
    return rows
