"""The one accessor of the process environment (ARBORA-HOST-R010.2).

Every environment read in ``arbora/`` goes through :func:`read_env`, :func:`read_text`
or :func:`read_reference`, and every environment write through :func:`write_env` or
:func:`unset_env`. This module is the only place of the package that touches
``os.environ``; ``tests/architecture/test_environment_reads_declared.py`` fails on any
other raw read. A read of a name the manifest does not declare refuses
``ENV_UNDECLARED_READ`` before it reads (``arbora.core.environment_manifest`` holds the
declarations, the census and the startup validation).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from functools import cache
from typing import TYPE_CHECKING, Any

from scion_sdk.utilities import to_boolean

if TYPE_CHECKING:
    from arbora.core.environment_manifest import EnvironmentDeclaration

__all__ = [
    "ENV_REFERENCE_FAMILY",
    "environment_names",
    "environment_view",
    "inherited_environment",
    "read_env",
    "read_reference",
    "read_text",
    "unset_env",
    "write_env",
]

#: The declaration name of the ``env://NAME`` secret-reference family.
ENV_REFERENCE_FAMILY = "env://*"
_UNSET: Any = object()
_REFERENCE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_WRITE_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SLOT = re.compile(r"\{([A-Z]+)\}")


def _refuse(code: str, name: str) -> Exception:
    from arbora.core.environment_manifest import EnvironmentRefusal

    return EnvironmentRefusal(code, [name])


@cache
def _index() -> tuple[dict[str, EnvironmentDeclaration], tuple[Any, ...]]:
    from arbora.core.environment_manifest import arbora_declarations

    exact: dict[str, EnvironmentDeclaration] = {}
    templates: list[tuple[re.Pattern[str], EnvironmentDeclaration]] = []
    for item in arbora_declarations():
        if "{" in item.name:
            parts = _SLOT.split(item.name)
            body = "".join(
                re.escape(p) if i % 2 == 0 else "([A-Z0-9]+)"
                for i, p in enumerate(parts)
            )
            templates.append((re.compile(f"^{body}$"), item))
        else:
            exact[item.name] = item
    return exact, tuple(templates)


def _platforms() -> frozenset[str]:
    from arbora.messaging.capabilities import CAPABILITY_MATRIX

    return frozenset(key.upper() for key in CAPABILITY_MATRIX)


def _declaration(name: str) -> EnvironmentDeclaration:
    exact, templates = _index()
    found = exact.get(name)
    if found is not None:
        return found
    for pattern, template in templates:
        match = pattern.match(name)
        if match and match.group(1) in _platforms():
            return template
    raise _refuse("ENV_UNDECLARED_READ", name)


def _typed(kind: str, raw: str) -> Any:
    if kind == "bool":
        return to_boolean(raw)
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    return raw


def read_env(name: str, fallback: Any = _UNSET) -> Any:
    """The typed value of one declared name now, or its declared default.

    An unset or empty variable, and a value its declared type rejects, give the default.
    ``fallback`` replaces the default for a caller whose default is computed at the call
    (a host name, a path under the caller's home); the declaration then holds ``None``.
    An undeclared name refuses ``ENV_UNDECLARED_READ`` before any read.
    """
    declaration = _declaration(name)
    kind = declaration.type
    try:
        default = (
            _typed(kind, declaration.default)
            if fallback is _UNSET and declaration.default is not None
            else (None if fallback is _UNSET else fallback)
        )
        raw = os.environ.get(name)
        return default if raw is None or raw == "" else _typed(kind, raw)
    except (TypeError, ValueError):
        return default


def read_text(name: str) -> str | None:
    """The raw text of one declared name, or ``None`` when it is unset."""
    _declaration(name)
    return os.environ.get(name)


def read_reference(name: str, default: str | None = "") -> str | None:
    """The text of the variable an operator's ``env://NAME`` reference names.

    The family is declared once as :data:`ENV_REFERENCE_FAMILY`; the name comes from
    operator configuration, never from code, and must be a plain identifier. An unset
    or empty variable gives ``default``.
    """
    if not _REFERENCE_NAME.match(name):
        raise _refuse("ENV_UNDECLARED_READ", ENV_REFERENCE_FAMILY)
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


def inherited_environment() -> dict[str, str]:
    """A copy of the process environment, for a child process that inherits it."""
    return dict(os.environ)


def environment_names() -> frozenset[str]:
    """The names, never the values, present in the process environment now."""
    return frozenset(os.environ)


def _writable(name: str) -> None:
    if not _WRITE_NAME.match(name):
        raise _refuse("ENV_UNDECLARED_WRITE", name)


def write_env(name: str, value: str) -> None:
    """Project one configuration key into the process environment."""
    _writable(name)
    os.environ[name] = value


def unset_env(name: str) -> None:
    """Remove one configuration key from the process environment."""
    _writable(name)
    os.environ.pop(name, None)


def environment_view(source: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """``source``, or a copy of the process environment when it is ``None``."""
    return inherited_environment() if source is None else source
