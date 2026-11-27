"""The finance guide projection (ARBORA-DATA-MARKET-R003.15).

The guide is a skill resource the venue connector serves. Its YAML front matter
carries the revision header; Arbora copies the header fields and the digest of the
whole body and derives nothing. A missing or unparsable header refuses
``UNAVAILABLE``: no revision is invented.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arbora.api.invoke.pipeline import OperationRefused

GUIDE_SERVER = "emerald-exchange"
GUIDE_URI = "skill://emerald-exchange-leveraged-trading/SKILL.md"
MAX_GUIDE_BYTES = 1_048_576


class GuideProjection(BaseModel):
    """The six fields of the finance guide, closed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guide_id: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=64)
    reviewed_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}(T[0-9:.]+Z)?$")
    reviewer: str = Field(min_length=1, max_length=256)
    source_cards: list[str] = Field(max_length=256)
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _unavailable() -> OperationRefused:
    return OperationRefused("UNAVAILABLE", {"reason": "guide revision header absent"})


def _text(resource: Any) -> str:
    """The text of a fleet resource read, whichever envelope carried it."""
    node = resource
    if isinstance(node, Mapping):
        for key in ("structured_content", "structuredContent", "result", "value"):
            if isinstance(node.get(key), Mapping):
                node = node[key]
                break
    if isinstance(node, Mapping):
        contents = node.get("contents")
        if isinstance(contents, list) and contents and isinstance(contents[0], Mapping):
            node = contents[0]
        text = node.get("text")
        if isinstance(text, str):
            return text
    raise _unavailable()


def project_guide(resource: Any) -> dict[str, Any]:
    """The closed projection of one guide resource read."""
    text = _text(resource)
    raw = text.encode()
    if len(raw) > MAX_GUIDE_BYTES or not text.startswith("---\n"):
        raise _unavailable()
    head, separator, _body = text[4:].partition("\n---")
    if not separator:
        raise _unavailable()
    try:
        header = yaml.safe_load(head)
        if not isinstance(header, Mapping):
            raise _unavailable()
        return GuideProjection(
            guide_id=header["name"],
            revision=str(header["revision"]),
            reviewed_at=str(header["reviewed_at"]),
            reviewer=header["reviewer"],
            source_cards=header["source_cards"],
            body_sha256=hashlib.sha256(raw).hexdigest(),
        ).model_dump()
    except (yaml.YAMLError, KeyError, TypeError, ValidationError):
        raise _unavailable() from None


__all__ = ["GUIDE_SERVER", "GUIDE_URI", "GuideProjection", "project_guide"]
