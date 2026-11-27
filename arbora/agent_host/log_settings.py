"""Host log bounds from ``config.json`` (ARBORA-HOST-R009.4).

``host_logs.max_bytes`` (default 10485760, range 65536 to 1073741824) is the size at which
the sink rotates ``server.log``; ``host_logs.keep_files`` (default 5, range 1 to 50) is the
number of log files kept, the live one included. An absent key takes its default and a
present key outside its range refuses with ``invalid_host_log_settings``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "HostLogSettings",
    "configured_host_log_settings",
    "host_log_settings",
    "resolve_host_log_settings",
]

MAX_BYTES_KEY = "host_logs.max_bytes"
KEEP_FILES_KEY = "host_logs.keep_files"
DEFAULT_MAX_BYTES = 10_485_760
DEFAULT_KEEP_FILES = 5


@dataclass(frozen=True)
class HostLogSettings:
    max_bytes: int = DEFAULT_MAX_BYTES
    keep_files: int = DEFAULT_KEEP_FILES


def _bounded(
    document: Mapping[str, str], key: str, default: int, low: int, high: int
) -> int:
    raw = document.get(key)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError("invalid_host_log_settings") from None
    if not low <= value <= high:
        raise ValueError("invalid_host_log_settings")
    return value


def host_log_settings(document: Mapping[str, str]) -> HostLogSettings:
    """The bounds a loaded ``config.json`` document names."""
    return HostLogSettings(
        max_bytes=_bounded(document, MAX_BYTES_KEY, DEFAULT_MAX_BYTES, 65_536, 1 << 30),
        keep_files=_bounded(document, KEEP_FILES_KEY, DEFAULT_KEEP_FILES, 1, 50),
    )


def configured_host_log_settings() -> HostLogSettings:
    """The bounds in the active ``config.json``, or the defaults when it is absent."""
    from arbora.core.settings import platform_config_file, read_config_document

    target = platform_config_file("config.json")
    if not target.is_file():
        return HostLogSettings()
    return host_log_settings(read_config_document(target))


def resolve_host_log_settings(given: HostLogSettings | None) -> HostLogSettings:
    """The caller's bounds, else the configured ones."""
    return given or configured_host_log_settings()
