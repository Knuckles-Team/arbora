"""Compose AU checkpoint mechanism with EG's owner-bound native WorkItem store."""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Any

from arbora.admitted_run import AdmittedClaim, current_admitted_claim
from arbora.api.contracts.session import use_session
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH


@dataclass
class RunCheckpointPort:
    binding: AdmittedClaim
    client: Any
    revision: int | None = None
    restored: bool = False
    restore_consumed: bool = False
    publication_nonce: str = field(default_factory=lambda: token_hex(16), repr=False)
    pending: dict[str, Any] | None = None

    @property
    def admission_identity(self) -> str:
        identity = [
            self.binding.session.tenant,
            self.binding.work_item_id,
            self.binding.run_id,
        ]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def _identity(self) -> dict[str, Any]:
        self.binding.require_active()
        return {
            "tenant": self.binding.session.tenant,
            "work_item_id": self.binding.work_item_id,
            "run_id": self.binding.run_id,
        }

    def _claim(self) -> dict[str, Any]:
        return {
            "worker_id": self.binding.claim["lease_holder_ref"],
            "lease_epoch": self.binding.claim["lease_epoch"],
            "fencing_token": self.binding.claim["fencing_token"],
        }

    async def _native_call(self, method: str, payload: dict[str, Any]) -> Any:
        self.binding.require_active()
        try:
            with use_session(self.binding.session):
                result = await getattr(self.client, method)(**payload)
        except PermissionError as exc:
            raise PermissionError("native checkpoint authority refused") from exc
        except Exception as exc:
            raise RuntimeError("native checkpoint is unavailable") from exc
        self.binding.require_active()
        return result

    async def restore(self) -> str | None:
        if self.restore_consumed:
            raise RuntimeError("checkpoint restore already consumed")
        self.restore_consumed = True
        result = await self._native_call(
            "get_checkpoint", {**self._identity(), "current_claim": self._claim()}
        )
        if result is None:
            self.restored = True
            return None
        if (
            not isinstance(result, dict)
            or type(result.get("revision")) is not int
            or result["revision"] < 1
        ):
            raise RuntimeError("checkpoint projection is invalid")
        content = result.get("content")
        if (
            not isinstance(content, str)
            or len(content.encode()) > 1_048_576
            or hashlib.sha256(content.encode()).hexdigest()
            != result.get("payload_digest")
        ):
            raise RuntimeError("checkpoint projection is invalid")
        self.revision = result["revision"]
        self.restored = True
        return content

    async def save(self, content: str) -> None:
        if not self.restored:
            raise RuntimeError("checkpoint restore required before publish")
        if (
            not isinstance(content, str)
            or not content
            or len(content.encode()) > 1_048_576
        ):
            raise ValueError("checkpoint content is invalid")
        digest = hashlib.sha256(content.encode()).hexdigest()
        payload = {
            **self._identity(),
            **self._claim(),
            "expected_revision": self.revision,
            "snapshot": {
                "content": content,
                "payload_digest": digest,
                "short_term": [],
                "long_term": [],
            },
            "idempotency_key": hashlib.sha256(
                json.dumps(
                    [
                        self.admission_identity,
                        self.publication_nonce,
                        self.revision,
                        digest,
                    ]
                ).encode()
            ).hexdigest(),
        }
        if self.pending is not None and self.pending != payload:
            raise RuntimeError("checkpoint write outcome is uncertain")
        retrying = self.pending is not None
        self.pending = payload
        result = await self._native_call("publish_checkpoint", payload)
        expected = 1 if self.revision is None else self.revision + 1
        if (
            not isinstance(result, dict)
            or type(result.get("revision")) is not int
            or result["revision"] != expected
            or result.get("payload_digest") != digest
            or result.get("work_item_id") != self.binding.work_item_id
            or result.get("run_id") != self.binding.run_id
            or type(result.get("replayed")) is not bool
            or (result["replayed"] and not retrying)
        ):
            raise RuntimeError("checkpoint receipt is invalid")
        self.revision = result["revision"]
        self.pending = None


@contextlib.asynccontextmanager
async def checkpoint_scope(
    request: Any, plan: Any
) -> AsyncIterator[RunCheckpointPort | None]:
    """Bind only a native capability present in the installed producer."""
    binding = current_admitted_claim(request)
    if binding is None:
        yield None
        return
    client = binding.engine.graph_compute.for_graph(
        CONTROL_GRAPH
    ).async_client.work_items
    if not callable(getattr(client, "get_checkpoint", None)) or not callable(
        getattr(client, "publish_checkpoint", None)
    ):
        yield None
        return
    if "checkpoint" in binding.adapters:
        raise RuntimeError("checkpoint admission already consumed")
    port = RunCheckpointPort(binding, client)
    binding.adapters["checkpoint"] = port
    yield port
