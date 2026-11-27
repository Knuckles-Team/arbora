"""Install one specialist package under a provider root (ARBORA-HOST-R034).

``specialists.install`` is the served door to :func:`arbora.host.specialists.install_package`.
The provider root is ``<config dir>/specialists/<provider>``; a caller names only the provider
and never a path. The engine write that registers the catalog nodes runs as the verified caller
through ``AddTriples``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops.engine_calls import engine_send
from arbora.api.registry import Effect, OpSpec
from arbora.host.specialists import (
    SpecialistInstallRefused,
    install_package,
    package_from_mapping,
)

SCOPE = "ops:admin"
METHOD = "AddTriples"
#: The engine replays the catalog write by the request key.
NATIVE_IDEMPOTENCY: dict[str, str] = {"specialists.install": "medulla:AddTriples"}
#: One asset's bytes in hex, so one asset is at most 4 MiB.
ASSET_HEX_MAX = 8 * 1024 * 1024


class ProviderAssetParams(_common.Params):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hex: str = Field(max_length=ASSET_HEX_MAX)


class SpecialistPackageParams(_common.Params):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    owner: str = Field(min_length=1, max_length=256)
    assets: list[ProviderAssetParams] = Field(min_length=1, max_length=256)


class SpecialistInstallParams(_common.Params):
    provider: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    package: SpecialistPackageParams


class SpecialistInstallResult(_common.Params):
    value: dict[str, Any]


async def handle_specialists_install(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Install the package; a closed refusal code leaves the marker as it was."""
    from arbora.core.settings import config_dir

    root = config_dir() / "specialists" / str(params["provider"])
    package = package_from_mapping(dict(params["package"]))

    async def register(ntriples: str) -> Any:
        return await engine_send(
            context, METHOD, {"ntriples": ntriples}, idempotent=True
        )

    try:
        installed = await install_package(root, package, register=register)
    except SpecialistInstallRefused as refused:
        raise OperationRefused(refused.code, {"detail": refused.detail}) from refused
    return {
        "value": {
            "package": installed.name,
            "version": installed.version,
            "generation": installed.generation,
            "previous": installed.previous,
        }
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_write_op(
            effect=Effect.ADMIN,
            op_id="specialists.install",
            summary="Install a specialist package generation and register its catalog nodes",
            examples=("install a specialist package",),
            params=SpecialistInstallParams,
            result=SpecialistInstallResult,
            handler="arbora.api.ops.specialists.handle_specialists_install",
            scope=SCOPE,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "SpecialistInstallParams",
    "handle_specialists_install",
    "operations",
    "specs",
]
