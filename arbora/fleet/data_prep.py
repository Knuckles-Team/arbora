"""Host bootstrap declares data-prep fields to the engine (ARBORA-FLEET-R058).

The host reads ``data_prep.yml`` from the config directory and calls
``eg.ingest.DataPrepDeclare`` once per capability. The file holds data only: a
capability name, its field refs and a policy digest. Any other key (an import
path, a command) is refused, so the file can carry no executable.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from arbora.core.settings import data_prep_path

__all__ = ["DataPrepError", "declare_data_prep", "load_declarations"]

_ALLOWED = frozenset({"capability", "field_refs", "policy_digest"})
_DIGEST = re.compile(r"[0-9a-f]{64}")

#: ``declare(request)`` sends one ``DataPrepDeclare`` and returns the receipt.
Declare = Callable[[dict[str, Any]], Awaitable[Any]]


class DataPrepError(ValueError):
    """The data-prep file is malformed; nothing is declared."""


def load_declarations(path: Path | None = None) -> list[dict[str, Any]]:
    """Read and validate the declarations; an absent file declares nothing."""
    target = path or data_prep_path()
    if not target.is_file():
        return []
    document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    rows = document.get("declarations") if isinstance(document, Mapping) else None
    if not isinstance(rows, list):
        raise DataPrepError("data_prep.yml needs a declarations list")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        declaration = _declaration(row, seen)
        seen.add(declaration["capability"])
        out.append(declaration)
    return out


def _declaration(row: Any, seen: set[str]) -> dict[str, Any]:
    """One validated declaration; ``seen`` holds the capabilities already declared."""
    if not isinstance(row, Mapping) or set(row) - _ALLOWED:
        raise DataPrepError(
            "a declaration holds only capability, field_refs, policy_digest"
        )
    capability = _capability(row.get("capability"), seen)
    return {
        "capability": capability,
        "field_refs": _field_refs(row.get("field_refs")),
        "policy_digest": _policy_digest(row.get("policy_digest")),
    }


def _capability(value: Any, seen: set[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataPrepError("a declaration needs a capability")
    if value in seen:
        raise DataPrepError(f"capability {value} is declared twice")
    return value


def _field_refs(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(r, str) and r for r in value):
        raise DataPrepError("field_refs must be a list of non-empty strings")
    return list(value)


def _policy_digest(value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise DataPrepError("policy_digest must be 64 lowercase hex characters")
    return value


async def declare_data_prep(
    declare: Declare, *, tenant: str, path: Path | None = None
) -> list[str]:
    """Declare each capability once; return the capabilities declared.

    The engine's refusal is propagated unchanged: a receipt that carries a
    ``refusal_code`` raises.
    """
    declared: list[str] = []
    for row in load_declarations(path):
        receipt = await declare({"payload": row, "tenant": tenant})
        code = (
            receipt.get("refusal_code")
            if isinstance(receipt, Mapping)
            else getattr(receipt, "refusal_code", None)
        )
        if code:
            raise DataPrepError(str(code))
        declared.append(row["capability"])
    return declared
