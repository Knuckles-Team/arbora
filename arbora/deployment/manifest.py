"""Declarative deployment manifest: plan, drift, destructive change and apply.

ARBORA-DEPLOY-POLICY-R001 to R003 and R007. The manifest file names the desired
components. Live state comes from one read tool through :class:`LiveStateReader`;
Arbora keeps no copy. The diff, its digest and the destructive set are pure
functions of those two inputs, so a plan and the apply that confirms it agree
exactly. Apply runs only the changed components through the confirmed step
graph and records each rollout; a converged state has an empty diff and makes no
effect call.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arbora.deployment.image_digests import is_digest_pinned, load_strict

__all__ = [
    "ApplyReceipt",
    "DeploymentDiff",
    "DeploymentManifest",
    "LiveStateReader",
    "ManifestRefused",
    "apply_manifest",
    "canonical_digest",
    "load_manifest",
    "plan_manifest",
    "record_rollout",
]

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SECRET_REF = re.compile(r"^(openbao|env)://\S+$")
Change = Literal["create", "update", "scale", "destroy"]


class ManifestRefused(Exception):
    """A typed refusal of the manifest path; ``code`` is the served refusal code."""

    def __init__(self, code: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Capacity(_Closed):
    cpu_millis: int = Field(ge=0)
    memory_bytes: int = Field(ge=0)
    replicas: int = Field(ge=0)


class ManifestComponent(_Closed):
    id: str = Field(min_length=1, max_length=128)
    kind: Literal["engine_shard", "gateway", "connector", "webui"]
    image: str
    revision: str = Field(min_length=1, max_length=128)
    identity_mode: Literal["none", "local", "external"]
    secrets_ref: str | None = None
    capacity: Capacity
    persistent_volumes: tuple[str, ...] = ()


class DeploymentManifest(_Closed):
    schema_version: Literal[1]
    name: str
    components: tuple[ManifestComponent, ...] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class DeploymentDiff:
    entries: tuple[dict[str, Any], ...]
    drift: tuple[dict[str, Any], ...]
    destructive: tuple[dict[str, Any], ...]
    manifest_digest: str

    @property
    def plan_digest(self) -> str:
        return canonical_digest(
            {"manifest": self.manifest_digest, "entries": list(self.entries)}
        )

    @property
    def destructive_digest(self) -> str:
        return canonical_digest(list(self.destructive))


@dataclass(frozen=True, slots=True)
class ApplyReceipt:
    """What an apply converged to; it carries no timestamp, so a rerun is identical."""

    manifest_digest: str
    components: tuple[dict[str, str], ...]

    @property
    def receipt_digest(self) -> str:
        return canonical_digest(
            {"manifest_digest": self.manifest_digest, "components": self.components}
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_digest": self.manifest_digest,
            "components": [dict(c) for c in self.components],
            "receipt_digest": self.receipt_digest,
        }


class LiveStateReader(Protocol):
    """One read of the deployed components: ``{id, image, revision, replicas, ...}``."""

    async def read(self, target: str) -> list[Mapping[str, Any]]: ...


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def load_manifest(root: Path, name: str) -> DeploymentManifest:
    """Read ``<root>/<name>.yaml`` with the strict loader and validate it."""
    if not NAME_PATTERN.fullmatch(name):
        raise ManifestRefused("MANIFEST_INVALID", {"field": "name"})
    path = root / f"{name}.yaml"
    try:
        document = load_strict(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestRefused("MANIFEST_INVALID", {"reason": "unreadable"}) from exc
    except yaml.YAMLError as exc:
        raise ManifestRefused("MANIFEST_INVALID", {"reason": str(exc)[:200]}) from exc
    try:
        manifest = DeploymentManifest.model_validate(document)
    except ValidationError as exc:
        raise ManifestRefused(
            "MANIFEST_INVALID", {"reason": str(exc.errors()[0]["loc"])}
        ) from exc
    if manifest.name != name:
        raise ManifestRefused("MANIFEST_INVALID", {"field": "name"})
    for component in manifest.components:
        if not is_digest_pinned(component.image):
            raise ManifestRefused(
                "IMAGE_NOT_DIGEST_PINNED", {"component_id": component.id}
            )
        ref = component.secrets_ref
        if ref is not None and not _SECRET_REF.fullmatch(ref):
            raise ManifestRefused("MANIFEST_INVALID", {"field": "secrets_ref"})
    return manifest


def _state(component: ManifestComponent) -> dict[str, Any]:
    return {
        "image": component.image,
        "revision": component.revision,
        "replicas": component.capacity.replicas,
    }


def _live_state(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "image": row.get("image"),
        "revision": row.get("revision"),
        "replicas": row.get("replicas"),
    }


def plan_manifest(
    manifest: DeploymentManifest, live: list[Mapping[str, Any]]
) -> DeploymentDiff:
    """The typed diff of the manifest against live state; nothing is written."""
    by_id = {str(row.get("id")): row for row in live}
    entries: list[dict[str, Any]] = []
    destructive: list[dict[str, Any]] = []
    wanted = {c.id: c for c in manifest.components}
    for cid in sorted(wanted | by_id):
        component, row = wanted.get(cid), by_id.get(cid)
        if component is None:
            if row is None:
                continue
            entries.append(
                {
                    "component_id": cid,
                    "change": "destroy",
                    "from": _live_state(row),
                    "to": None,
                }
            )
            destructive.append({"component_id": cid, "reason": "destroy"})
            continue
        if row is None:
            entries.append(
                {
                    "component_id": cid,
                    "change": "create",
                    "from": None,
                    "to": _state(component),
                }
            )
            continue
        before, after = _live_state(row), _state(component)
        if before == after:
            removed = set(row.get("persistent_volumes") or ()) - set(
                component.persistent_volumes
            )
            if removed:
                destructive.append({"component_id": cid, "reason": "volume_removed"})
            continue
        same = {k: before[k] == after[k] for k in ("image", "revision")}
        change: Change = "scale" if all(same.values()) else "update"
        entries.append(
            {"component_id": cid, "change": change, "from": before, "to": after}
        )
        if after["replicas"] < 1 and change != "create":
            destructive.append({"component_id": cid, "reason": "scale_below_one"})
        if set(row.get("persistent_volumes") or ()) - set(component.persistent_volumes):
            destructive.append({"component_id": cid, "reason": "volume_removed"})
    drift = tuple(e for e in entries if e["change"] in {"update", "scale"})
    manifest_digest = canonical_digest(manifest.model_dump(mode="json"))
    return DeploymentDiff(
        tuple(entries),
        drift,
        tuple(sorted(destructive, key=_destructive_key)),
        manifest_digest,
    )


def _destructive_key(item: Mapping[str, Any]) -> tuple[str, str]:
    return (str(item["component_id"]), str(item["reason"]))


def missing_inputs(
    manifest: DeploymentManifest, entries: tuple[dict[str, Any], ...]
) -> list[str]:
    """The inputs a changed component needs and the manifest does not give."""
    changed = {
        e["component_id"] for e in entries if e["change"] in {"create", "update"}
    }
    return [
        f"components.{c.id}.secrets_ref"
        for c in manifest.components
        if c.id in changed and c.identity_mode != "none" and c.secrets_ref is None
    ]


#: ``rollout(component_id, key_prefix)`` runs one component's confirmed steps.
Rollout = Callable[[str, str], Awaitable[Mapping[str, Any]]]
Record = Callable[[str, str], Awaitable[bool]]


async def apply_manifest(
    manifest: DeploymentManifest,
    diff: DeploymentDiff,
    *,
    rollout: Rollout,
    record: Record,
) -> ApplyReceipt:
    """Run the changed components, record each rollout, and return the receipt.

    The caller checked the confirmed ``plan_digest`` and the approval. An empty
    diff runs nothing. A failed step stops the apply with ``ROLLOUT_FAILED``; a
    failed record marks the component ``unrecorded`` and refuses ``UNAVAILABLE``.
    """
    unrecorded: list[str] = []
    for entry in diff.entries:
        if entry["change"] not in {"create", "update", "scale"}:
            continue
        cid = entry["component_id"]
        run = await rollout(cid, f"deploy:{diff.manifest_digest}:{cid}")
        if not run.get("ok"):
            raise ManifestRefused("ROLLOUT_FAILED", {"component_id": cid})
        if not await record(cid, entry["to"]["image"]):
            unrecorded.append(cid)
    if unrecorded:
        raise ManifestRefused(
            "UNAVAILABLE",
            {
                "reason": "the deployment record was not accepted",
                "components": [{"id": c, "outcome": "unrecorded"} for c in unrecorded],
            },
        )
    return ApplyReceipt(
        diff.manifest_digest,
        tuple(
            {"id": c.id, "revision": c.revision, "outcome": "converged"}
            for c in manifest.components
        ),
    )


async def record_rollout(
    invoke: Callable[[str, Mapping[str, Any], Any, str], Awaitable[Any]],
    caller: Any,
    *,
    manifest_digest: str,
    component_id: str,
    artifact_digest: str,
    target: str,
    now_ms: int,
) -> bool:
    """Send one rollout to the served ``deployment.record`` operation."""
    outcome = await invoke(
        "deployment.record",
        {
            "artifact_digest": artifact_digest.rsplit("@", 1)[-1],
            "target": target,
            "component": component_id,
            "recorded_at_ms": now_ms,
        },
        caller,
        f"record:{manifest_digest}:{component_id}",
    )
    return getattr(outcome, "code", None) == "OK"
