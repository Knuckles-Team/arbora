"""Policy bundle files: strict loading for the engine's policy family.

ARBORA-DEPLOY-POLICY-R004. A bundle is a YAML file under the Arbora config
directory. Arbora checks its shape with the strict loader (unknown and duplicate
keys are errors) and sends it unchanged to the engine identity policy family,
which compiles it into its isolation layer. Arbora holds no rule table and no
evaluator.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arbora.deployment.image_digests import StrictLoader

__all__ = ["BundleRefused", "PolicyBundle", "bundle_name_ok", "load_bundle"]

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ID = re.compile(r"^[a-z0-9._:-]{1,128}$")


class BundleRefused(Exception):
    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(code)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BundleScope(_Closed):
    kind: Literal["tenant", "graph"]
    graph: str | None = None


class BundleRule(_Closed):
    principal_group: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    resource_scope: dict[str, Any] | str
    effect: Literal["allow", "deny"]


class PolicyBundle(_Closed):
    schema_version: int
    bundle_id: str
    scope: BundleScope
    rules: tuple[BundleRule, ...] = Field(min_length=1, max_length=256)

    def wire(self) -> dict[str, Any]:
        """The bundle as the engine's ``PolicyRuleBundle`` body."""
        body = self.model_dump(mode="json")
        scope = body["scope"]
        body["scope"] = (
            "tenant"
            if scope["kind"] == "tenant"
            else {"graph": {"graph": scope["graph"]}}
        )
        return body


def bundle_name_ok(name: str) -> bool:
    return _NAME.fullmatch(name) is not None


def load_bundle(root: Path, name: str) -> PolicyBundle:
    """Read ``<root>/<name>.yaml`` with the strict loader."""
    if not bundle_name_ok(name):
        raise BundleRefused("POLICY_BUNDLE_INVALID", {"field": "name"})
    try:
        document = yaml.load(  # noqa: S506 - strict safe loader
            (root / f"{name}.yaml").read_text(encoding="utf-8"), Loader=StrictLoader
        )
        bundle = PolicyBundle.model_validate(document)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise BundleRefused(
            "POLICY_BUNDLE_INVALID", {"reason": type(exc).__name__}
        ) from exc
    if not _ID.fullmatch(bundle.bundle_id) or (
        bundle.scope.kind == "graph" and not bundle.scope.graph
    ):
        raise BundleRefused("POLICY_BUNDLE_INVALID", {"field": "bundle_id"})
    return bundle
