"""Versioned memory file models and outcome mapping (ARBORA-OPS-R024.4.1).

The engine owns content, revisions and conflicts. These models carry the
request fields of the engine methods ``ReadMemorySnapshot``,
``ListMemorySnapshots`` and ``SyncMemorySnapshot`` and map the engine's reply to
the served result. Arbora never merges and never keeps a copy.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from arbora.api.ops._common import Params
from arbora.core.settings import RuntimeConfig

_DIGEST = r"^[0-9a-f]{64}$"


class MemoryFilesRead(Params):
    """Read the head of one document, or one retained revision."""

    doc_id: str = Field(min_length=1, max_length=512)
    revision: int | None = Field(default=None, ge=0)


class MemoryFilesList(Params):
    """List document heads under a prefix, in document order."""

    prefix: str = Field(default="", max_length=512)
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=50, ge=1, le=200)


class MemoryFilesSave(Params):
    """Submit new content, or resolve a retained conflict."""

    action: Literal["submit", "resolve"] = "submit"
    doc_id: str = Field(min_length=1, max_length=512)
    content: str | None = None
    content_digest: str | None = Field(default=None, pattern=_DIGEST)
    expected_revision: int | None = Field(default=None, ge=0)
    candidate_revision: int | None = Field(default=None, ge=0)
    expected_head_revision: int | None = Field(default=None, ge=0)
    resolution: Literal["keep_head", "take_candidate", "merged"] | None = None

    @model_validator(mode="after")
    def _shape(self) -> MemoryFilesSave:
        if self.action == "submit":
            if self.content is None or self.content_digest is None:
                raise ValueError("submit needs content and content_digest")
            if (
                self.candidate_revision,
                self.expected_head_revision,
                self.resolution,
            ) != (None, None, None):
                raise ValueError("submit takes no resolution fields")
        else:
            if (
                self.candidate_revision is None
                or self.expected_head_revision is None
                or self.resolution is None
            ):
                raise ValueError("resolve needs both revisions and a resolution")
            if (self.resolution == "merged") != (self.content is not None):
                raise ValueError("only a merged resolution carries content")
            if self.resolution == "merged" and self.content_digest is None:
                raise ValueError("a merged resolution needs content_digest")
        return self


def digest_matches(content: str, digest: str) -> bool:
    """Whether ``digest`` is the SHA-256 of the UTF-8 ``content``."""
    return hashlib.sha256(content.encode()).hexdigest() == digest


def sync_request(params: Mapping[str, Any], request_id: str) -> dict[str, Any]:
    """The engine ``SyncRequest`` for one save, fields forwarded unchanged."""
    if params["action"] == "submit":
        action: dict[str, Any] = {
            "action": "submit",
            "content": params["content"],
            "content_digest": params["content_digest"],
        }
        if params.get("expected_revision") is not None:
            action["expected_revision"] = params["expected_revision"]
    else:
        resolution: dict[str, Any] = {"resolution": params["resolution"]}
        if params["resolution"] == "merged":
            resolution["content"] = params["content"]
            resolution["content_digest"] = params["content_digest"]
        action = {
            "action": "resolve",
            "candidate_revision": params["candidate_revision"],
            "expected_head_revision": params["expected_head_revision"],
            "resolution": resolution,
        }
    return {
        "request": {
            "action": action,
            "doc_id": params["doc_id"],
            "request_id": request_id,
        }
    }


def sync_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Map the engine outcome to the served result.

    A refused save carries no code and no head, so a denied request for an
    existing document, an absent one and a creation attempt look identical.
    """
    kind = outcome["outcome"]
    if kind == "refused":
        return {"outcome": "refused"}
    return {key: value for key, value in outcome.items() if key != "code"}


_logger = logging.getLogger(__name__)


class MemoryMaterializer:
    """Writes accepted heads to the hosted directory, then reports them.

    ARBORA-OPS-R024.4.4. The directory is ``MEMORY_FILES_ROOT``. Unset, the
    materializer refuses before any engine call. A failed file write sends no
    acknowledgment. The observation generation only grows, so the engine
    ignores a late acknowledgment for an older generation.
    """

    def __init__(
        self, config: RuntimeConfig, root: str | os.PathLike[str] | None = None
    ) -> None:
        self._config = config
        self._root = root
        self._generation = 0

    def root(self) -> Path:
        """The configured directory, or a fail-closed refusal."""
        from arbora.api.invoke.pipeline import OperationRefused

        configured = self._root or self._config.memory_files_root
        if not configured:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "memory files root is not configured"}
            )
        return Path(configured)

    def next_generation(self) -> int:
        self._generation = max(self._generation + 1, int(time.time() * 1000))
        return self._generation

    def path_for(self, doc_id: str) -> Path:
        """The file of one document; a path outside the root is refused."""
        from arbora.api.invoke.pipeline import OperationRefused

        root = self.root().resolve()
        parts = doc_id.split("/")
        if (
            doc_id.startswith("/")
            or "\x00" in doc_id
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise OperationRefused("INVALID_ARGUMENT", {"field": "doc_id"})
        target = root.joinpath(*parts)
        if not target.resolve().is_relative_to(root):
            raise OperationRefused("INVALID_ARGUMENT", {"field": "doc_id"})
        return target

    def write(self, doc_id: str, content: str) -> bool:
        """Write the projection atomically; ``False`` when the write fails."""
        try:
            target = self.path_for(doc_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.parent.resolve().is_relative_to(self.root().resolve()):
                return False
            scratch = target.with_name(f".{target.name}.{os.getpid()}.part")
            scratch.write_text(content, encoding="utf-8")
            os.replace(scratch, target)
        except OSError:
            _logger.warning("memory file projection failed for one document")
            return False
        return True

    def acknowledgment(self, doc_id: str, head: Mapping[str, Any], key: str) -> dict:
        """The engine ``acknowledge`` request for one materialized head."""
        return {
            "request": {
                "action": {
                    "action": "acknowledge",
                    "revision": head["revision"],
                    "content_digest": head["content_digest"],
                    "observation_generation": self.next_generation(),
                },
                "doc_id": doc_id,
                "request_id": f"{key}:ack",
            }
        }
