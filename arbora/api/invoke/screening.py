"""The default output redactor (ARBORA-OPS-R042.2).

One mechanism masks secrets in content that leaves the host: invoke results and
every A2A task event. It replaces bearer tokens, ``sk-`` style keys, cloud
access keys and private-key blocks with ``[REDACTED]``, and masks the value
under any key named ``password``, ``secret`` or ``token``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = ["REDACTED", "default_output_redactor", "redact_value"]

REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
)
_SECRET_KEY = re.compile(r"^(?:.*[_-])?(?:password|secret|token)$", re.IGNORECASE)


def default_output_redactor(text: str) -> tuple[str, int]:
    """Mask the secrets in ``text``; return the masked text and the mask count."""
    count = 0
    for pattern in _PATTERNS:
        text, replaced = pattern.subn(REDACTED, text)
        count += replaced
    return text, count


def redact_value(value: Any) -> Any:
    """Redact every string of a JSON-shaped value; mask secret-keyed values."""
    if isinstance(value, str):
        return default_output_redactor(value)[0]
    if isinstance(value, Mapping):
        return {
            key: REDACTED
            if isinstance(key, str) and _SECRET_KEY.match(key) and value[key]
            else redact_value(value[key])
            for key in value
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value
