"""Select the shipped deployment artifact per target and check its image pins.

The deployment decision names a target; the artifact is the file the
repository already ships under ``deploy/``. Nothing here renders anything
(ARBORA-DEPLOY-R017.6). Image references pass the one digest checker in
:mod:`arbora.api.domain.deployment.image_digests` (ARBORA-DEPLOY-R017.18); the
environment profile decides whether that check is required
(ARBORA-DEPLOY-R017.16).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from arbora.api.domain.deployment.genesis_environments import (
    EnvironmentProfile,
    load_environment_profile,
)
from arbora.api.domain.deployment.image_digests import check_paths

#: The shipped artifact of each supported target, relative to ``deploy/``.
ARTIFACTS: Mapping[str, str] = {
    "compose": "compose/compose.yaml",
    "helm": "helm/arbora",
    "swarm": "swarm/stack.yml",
}

#: The bare-metal reference a ``systemd`` request is pointed to.
BARE_METAL_REFERENCE = "docs/deployment.md"

SKIPPED_BY_POLICY = "skipped by policy"


class ArtifactRefused(ValueError):
    """A closed refusal: ``code`` is one of ``unsupported_target``,
    ``ARTIFACT_MISSING`` or ``TAG_POLICY_UNKNOWN``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DigestVerdict:
    """What one digest check found; ``ok`` only when nothing was refused."""

    checked: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class DigestCheckDecision:
    """Whether the environment requires the digest check, and the note to record."""

    required: bool
    note: str = ""


def default_deploy_root() -> Path:
    """The ``deploy/`` directory of the source tree this package ships from."""
    return Path(__file__).resolve().parents[4] / "deploy"


def select_artifact(target: str, *, deploy_root: Path | None = None) -> Path:
    """The existing file for ``target``: compose, helm or swarm.

    ``systemd`` and any other target refuse ``unsupported_target``; a shipped
    artifact that is absent refuses ``ARTIFACT_MISSING``.
    """
    relative = ARTIFACTS.get(target)
    if relative is None:
        detail = f"target {target!r} has no shipped artifact"
        if target == "systemd":
            detail += f"; see {BARE_METAL_REFERENCE} for bare-metal hosts"
        raise ArtifactRefused("unsupported_target", detail)
    path = (deploy_root or default_deploy_root()) / relative
    if not path.exists():
        raise ArtifactRefused("ARTIFACT_MISSING", f"{path} is not shipped")
    return path


def verify_digests(
    paths: Sequence[Path], *, values: Mapping[str, str] | None = None
) -> DigestVerdict:
    """Run the one digest checker over rendered or shipped files.

    ``values`` is the resolve-mode ``--set`` substitution. The verdict carries
    the checked count and every refusal, each naming document and field path.
    """
    checked, errors = check_paths(list(paths), values)
    return DigestVerdict(checked=checked, errors=tuple(errors))


def digest_check_decision(
    environment: str,
    *,
    loader: Callable[[str], EnvironmentProfile] = load_environment_profile,
) -> DigestCheckDecision:
    """Map ``release.tag_policy`` of the named profile to the check decision."""
    policy = loader(environment).release.tag_policy
    if policy == "digest-pinned":
        return DigestCheckDecision(required=True)
    if policy == "floating-tag":
        return DigestCheckDecision(required=False, note=SKIPPED_BY_POLICY)
    raise ArtifactRefused(
        "TAG_POLICY_UNKNOWN", f"environment {environment!r} has tag_policy {policy!r}"
    )
