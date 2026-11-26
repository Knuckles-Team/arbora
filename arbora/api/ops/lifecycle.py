"""Lifecycle operations over the engine's delivery records (ARBORA-OPS-R853).

``defect.*``, ``release.record``, ``deployment.record``, ``spec.status`` and
``lifecycle.trace`` serve the engine's lifecycle methods (``MEDULLA-SDLC-R001`` to
``R006``). Arbora keeps no defect, release or deployment record and computes no
trace: each handler sends one engine method as the verified caller and returns the
engine answer. The methods are engine ``main`` shapes the pinned release lacks, so each
goes through :func:`arbora.pin_lag.medulla.send_main` and refuses ``UNAVAILABLE``
naming the missing method until the re-pin.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.registry import OpSpec, Verb
from arbora.pin_lag.medulla import send_main

READ_SCOPE = "kg:read"
WRITE_SCOPE = "lifecycle:write"
_TEXT = Field(min_length=1, max_length=4096)
_REPO = Field(min_length=1, max_length=512)

DefectStatus = Literal[
    "open", "triaged", "fixed", "verified", "rejected", "reopened", "closed"
]
Severity = Literal["low", "medium", "high", "critical"]


class DefectFileParams(Params):
    repo: str = _REPO
    intake: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    severity: Severity | None = None
    source_record_id: str = Field(min_length=1, max_length=512)
    observed_at: int = Field(ge=0)
    text: str = _TEXT


class DefectTriageParams(Params):
    defect_id: str = Field(min_length=1, max_length=512)
    culprit: str | None = Field(default=None, max_length=512)
    path_owners: list[str] = Field(default_factory=list, max_length=64)
    as_of_ms: int | None = Field(default=None, ge=0)


class DefectTransitionParams(Params):
    repo: str = _REPO
    defect_id: str = Field(min_length=1, max_length=512)
    to: DefectStatus
    fix_changeset: str | None = Field(default=None, max_length=512)
    gate_id: str | None = Field(default=None, max_length=512)
    reason: str | None = Field(default=None, max_length=4096)


class ReleaseRecordParams(Params):
    repo: str = _REPO
    version: str = Field(min_length=1, max_length=128)
    git_ref: str = Field(min_length=1, max_length=512)
    changeset_head: str = Field(min_length=1, max_length=512)
    artifact_digests: list[str] = Field(default_factory=list, max_length=256)
    published_at: int = Field(ge=0)


class DeploymentRecordParams(Params):
    environment: str = Field(min_length=1, max_length=128)
    workload: str = Field(min_length=1, max_length=256)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    started_at: int = Field(ge=0)
    finished_at: int = Field(ge=0)
    outcome: Literal["succeeded", "failed", "rolled_back", "unattributed"]


class SpecStatusParams(Params):
    repo: str = _REPO
    rev: str = Field(min_length=1, max_length=512)
    spec_id: str | None = Field(default=None, min_length=1, max_length=512)


class LifecycleTraceParams(Params):
    anchor: str = Field(min_length=1, max_length=512)
    depth: int = Field(default=3, ge=0, le=16)
    kinds: list[str] = Field(default_factory=list, max_length=32)


class LifecycleResult(Params):
    value: dict[str, Any]


def _value(answer: Any) -> dict[str, Any]:
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


def _given(params: Mapping[str, Any], *names: str) -> dict[str, Any]:
    """The named request fields the caller supplied; an absent one stays absent."""
    return {name: params[name] for name in names if params.get(name) is not None}


async def handle_defect_file(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = _given(
        params,
        "repo",
        "intake",
        "title",
        "severity",
        "source_record_id",
        "observed_at",
        "text",
    )
    answer = await send_main(
        context, "FileDefect", {"request": request}, idempotent=True
    )
    return _value(answer)


async def handle_defect_triage(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Triage names the engine's own codes: ``DEFECT_UNKNOWN`` and ``DEFECT_CLOSED`` pass through."""
    request = {
        **_given(params, "defect_id", "culprit", "as_of_ms"),
        "path_owners": list(params.get("path_owners") or ()),
        "tenant_id": str(context.caller.tenant),
    }
    answer = await send_main(
        context, "TriageDefect", {"request": request}, idempotent=True
    )
    return _value(answer)


async def handle_defect_transition(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = _given(
        params, "repo", "defect_id", "to", "fix_changeset", "gate_id", "reason"
    )
    answer = await send_main(
        context, "TransitionDefect", {"request": request}, idempotent=True
    )
    return _value(answer)


async def handle_release_record(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    release = _given(
        params,
        "repo",
        "version",
        "git_ref",
        "changeset_head",
        "artifact_digests",
        "published_at",
    )
    release.setdefault("artifact_digests", [])
    answer = await send_main(
        context, "RecordRelease", {"release": release}, idempotent=True
    )
    return _value(answer)


async def handle_deployment_record(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    deployment = _given(
        params,
        "environment",
        "workload",
        "image_digest",
        "started_at",
        "finished_at",
        "outcome",
    )
    answer = await send_main(
        context, "RecordDeployment", {"deployment": deployment}, idempotent=True
    )
    return _value(answer)


async def handle_spec_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = _given(params, "repo", "rev", "spec_id")
    return _value(await send_main(context, "SpecStatus", {"request": request}))


async def handle_lifecycle_trace(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = {
        "anchor": params["anchor"],
        "depth": int(params.get("depth", 3)),
        "kinds": list(params.get("kinds") or ()),
    }
    return _value(await send_main(context, "LifecycleTrace", {"request": request}))


#: Writes replay under the engine method that holds the record, never here.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "defect.file": "medulla:FileDefect",
    "defect.triage": "medulla:TriageDefect",
    "defect.transition": "medulla:TransitionDefect",
    "release.record": "medulla:RecordRelease",
    "deployment.record": "medulla:RecordDeployment",
}


def operations() -> tuple[OpSpec, ...]:
    def read(op_id: str, summary: str, example: str, params: type[Params]) -> OpSpec:
        return build_read_op(
            verb=Verb.ASK,
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=LifecycleResult,
            handler=f"{__name__}.handle_{op_id.replace('.', '_')}",
            scope=READ_SCOPE,
        )

    def write(op_id: str, summary: str, example: str, params: type[Params]) -> OpSpec:
        return build_write_op(
            verb=Verb.WRITE,
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=LifecycleResult,
            handler=f"{__name__}.handle_{op_id.replace('.', '_')}",
            scope=WRITE_SCOPE,
        )

    return (
        write(
            "defect.file",
            "File a defect from one observed symptom",
            "file a defect for this failed verdict",
            DefectFileParams,
        ),
        write(
            "defect.triage",
            "Triage a defect to a culprit and owners",
            "triage this defect",
            DefectTriageParams,
        ),
        write(
            "defect.transition",
            "Move a defect along its status machine",
            "mark this defect fixed",
            DefectTransitionParams,
        ),
        write(
            "release.record",
            "Record a published release",
            "record this release",
            ReleaseRecordParams,
        ),
        write(
            "deployment.record",
            "Record a deployment of a release",
            "record this deployment",
            DeploymentRecordParams,
        ),
        read(
            "spec.status",
            "Read the computed status of a repository's spec rows",
            "which spec rows are verified",
            SpecStatusParams,
        ),
        read(
            "lifecycle.trace",
            "Trace the lifecycle records linked to one anchor",
            "trace this requirement to its deployment",
            LifecycleTraceParams,
        ),
    )


specs = operations
