"""Remote workbench compatibility manifest and admission (ARBORA-HOST-R032).

Arbora creates no editor and no ACP adapter. The embedded editor's manifest
comes from the composed ``workbench`` port; there is no static manifest here.
Both operations read only, never expose a credential, and require an
authenticated workspace and session scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import bound_service, build_read_op
from arbora.api.registry import OpSpec, Verb

WORKBENCH_PROTOCOL_INCOMPATIBLE = "WORKBENCH_PROTOCOL_INCOMPATIBLE"
WORKBENCH_EXTENSION_INCOMPATIBLE = "WORKBENCH_EXTENSION_INCOMPATIBLE"
WORKBENCH_WORKSPACE_FORBIDDEN = "WORKBENCH_WORKSPACE_FORBIDDEN"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtensionSupport(_Closed):
    extension_id: str = Field(min_length=1, max_length=256)
    version_range: str = Field(min_length=1, max_length=128)
    capabilities: tuple[str, ...] = ()


class WorkbenchCompatibility(_Closed):
    schema_version: Literal[1] = 1
    editor_protocol: str = Field(min_length=1, max_length=64)
    editor_version: str = Field(min_length=1, max_length=64)
    extensions: tuple[ExtensionSupport, ...] = ()
    keybinding_profiles: tuple[str, ...] = ()
    workspace_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    focus_rules: tuple[str, ...] = ()
    reserved_shortcuts: tuple[str, ...] = ()


class ExtensionAdmission(_Closed):
    schema_version: Literal[1] = 1
    extension_id: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)
    outcome: Literal["admitted", "refused"]


class WorkbenchScopeParams(_Closed):
    workspace_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)


class WorkbenchAdmitParams(WorkbenchScopeParams):
    extension_id: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)
    editor_protocol: str = Field(min_length=1, max_length=64)


class WorkbenchCompatibilityResult(_Closed):
    value: WorkbenchCompatibility


class ExtensionAdmissionResult(_Closed):
    value: ExtensionAdmission


async def _manifest(context: Any, params: Mapping[str, Any]) -> WorkbenchCompatibility:
    """The manifest for one authorized workspace and session, or the refusal."""
    if context.caller.session is None:
        raise OperationRefused(WORKBENCH_WORKSPACE_FORBIDDEN, {})
    port = bound_service(
        context, "workbench", reason="the workbench manifest is not composed"
    )
    if not await port.authorizes(
        tenant=context.caller.tenant,
        workspace_id=params["workspace_id"],
        session_id=params["session_id"],
    ):
        # A foreign workspace answers exactly like an absent one.
        raise OperationRefused(WORKBENCH_WORKSPACE_FORBIDDEN, {})
    declared = await port.manifest(
        tenant=context.caller.tenant,
        workspace_id=params["workspace_id"],
        session_id=params["session_id"],
    )
    return WorkbenchCompatibility.model_validate(declared)


async def handle_workbench_compatibility(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    manifest = await _manifest(context, params)
    return {"value": manifest.model_dump(mode="json")}


def _in_range(version: str, version_range: str) -> bool:
    try:
        return Version(version) in SpecifierSet(version_range)
    except (InvalidVersion, InvalidSpecifier):
        return False


async def handle_workbench_admit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    manifest = await _manifest(context, params)
    if params["editor_protocol"] != manifest.editor_protocol:
        raise OperationRefused(
            WORKBENCH_PROTOCOL_INCOMPATIBLE,
            {"supported": manifest.editor_protocol},
        )
    declared = next(
        (e for e in manifest.extensions if e.extension_id == params["extension_id"]),
        None,
    )
    if declared is None or not _in_range(params["version"], declared.version_range):
        raise OperationRefused(
            WORKBENCH_EXTENSION_INCOMPATIBLE, {"extension_id": params["extension_id"]}
        )
    admission = ExtensionAdmission(
        extension_id=params["extension_id"],
        version=params["version"],
        outcome="admitted",
    )
    return {"value": admission.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="workbench.compatibility.get",
            summary="Read the remote workbench compatibility manifest",
            examples=("what does my workbench support",),
            params=WorkbenchScopeParams,
            result=WorkbenchCompatibilityResult,
            handler="arbora.api.ops.workbench.handle_workbench_compatibility",
            scope="kg:read",
            verb=Verb.ASK,
        ),
        build_read_op(
            op_id="workbench.extensions.admit",
            summary="Admit or refuse one workbench extension version",
            examples=("can my workbench load this extension",),
            params=WorkbenchAdmitParams,
            result=ExtensionAdmissionResult,
            handler="arbora.api.ops.workbench.handle_workbench_admit",
            scope="kg:read",
            verb=Verb.ASK,
        ),
    )


specs = operations
