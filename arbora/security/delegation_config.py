"""The host's token-delegation settings (ARBORA-HOST-R039.13).

``McpAuthConfig`` is the ``delegation`` field of the host context. It reads its
defaults from the environment when the composition root builds it, never at
import, and the server composition layers parsed command-line flags onto it. It
holds its own lock and is reached by argument, never through a module accessor.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from typing import Any

from scion_sdk.utilities import to_boolean

from arbora.core.environment import read_env

__all__ = ["McpAuthConfig"]

#: The environment name each delegation setting starts from.
_ENV_KEYS: tuple[tuple[str, str], ...] = (
    ("audience", "AUDIENCE"),
    ("delegated_scopes", "DELEGATED_SCOPES"),
    ("oidc_client_id", "OIDC_CLIENT_ID"),
    ("oidc_config_url", "OIDC_CONFIG_URL"),
    ("jwt_jwks_uri", "FASTMCP_SERVER_AUTH_JWT_JWKS_URI"),
    ("jwt_issuer", "FASTMCP_SERVER_AUTH_JWT_ISSUER"),
    ("jwt_audience", "FASTMCP_SERVER_AUTH_JWT_AUDIENCE"),
    ("jwt_algorithm", "FASTMCP_SERVER_AUTH_JWT_ALGORITHM"),
    ("jwt_secret", "FASTMCP_SERVER_AUTH_JWT_PUBLIC_KEY"),
    ("jwt_required_scopes", "FASTMCP_SERVER_AUTH_JWT_REQUIRED_SCOPES"),
)


class McpAuthConfig(Mapping[str, Any]):
    """The MCP auth and delegation settings of one host, read as a mapping."""

    __slots__ = ("_lock", "_values")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        values: dict[str, Any] = {
            "enable_delegation": to_boolean(read_env("ENABLE_DELEGATION", "False")),
            "token_endpoint": None,
            "oidc_client_secret": None,
        }
        values.update({key: read_env(env) for key, env in _ENV_KEYS})
        self._values = values

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._values[key]

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(tuple(self._values))

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def set(self, key: str, value: Any) -> None:
        """Replace one setting; the key must be a known one."""
        with self._lock:
            if key not in self._values:
                raise KeyError(key)
            self._values[key] = value
