"""Graph OS configuration reads and the platform configuration location.

Every Graph OS module reads a setting through :func:`setting` (the environment
at call time, typed by the default) and locates its configuration files through
:func:`config_dir` / :func:`platform_config_file`. Graph OS owns
``~/.config/graph-os``: ``GRAPH_OS_CONFIG_DIR`` wins, then
``$XDG_CONFIG_HOME/graph-os``. For one release a file that exists only under the
legacy ``AGENT_UTILITIES_CONFIG_DIR`` (or ``$XDG_CONFIG_HOME/agent-utilities``)
location is still found, with a single deprecation warning.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

from agent_connector_sdk.config import csv_values, setting

__all__ = [
    "CONFIG_DIR_SETTING",
    "config_dir",
    "csv_values",
    "platform_config_file",
    "setting",
]

CONFIG_DIR_SETTING = "GRAPH_OS_CONFIG_DIR"
_LEGACY_CONFIG_DIR_SETTING = "AGENT_UTILITIES_CONFIG_DIR"
_APP_DIR = "graph-os"
_LEGACY_APP_DIR = "agent-utilities"
_warned_legacy: set[Path] = set()


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _env_dir(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def config_dir() -> Path:
    """``GRAPH_OS_CONFIG_DIR`` or ``$XDG_CONFIG_HOME/graph-os`` (not created)."""
    return _env_dir(CONFIG_DIR_SETTING) or _config_home() / _APP_DIR


def _legacy_config_dir() -> Path:
    return _env_dir(_LEGACY_CONFIG_DIR_SETTING) or _config_home() / _LEGACY_APP_DIR


def platform_config_file(name: str) -> Path:
    """Return the path of configuration file ``name``.

    The Graph OS location is returned unless the file is absent there, no
    explicit ``GRAPH_OS_CONFIG_DIR`` is set, and it exists at the legacy
    location; that fallback warns once per legacy directory.
    """
    preferred = config_dir() / name
    if preferred.exists() or _env_dir(CONFIG_DIR_SETTING) is not None:
        return preferred
    legacy_dir = _legacy_config_dir()
    legacy = legacy_dir / name
    if not legacy.exists():
        return preferred
    if legacy_dir not in _warned_legacy:
        _warned_legacy.add(legacy_dir)
        warnings.warn(
            f"reading Graph OS configuration from legacy {legacy_dir}; "
            f"move it to {config_dir()}",
            DeprecationWarning,
            stacklevel=2,
        )
    return legacy
