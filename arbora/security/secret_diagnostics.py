"""Value-free diagnostics for Arbora secret references.

This module deliberately has no API that accepts a secret value.  Callers provide
an explicit allowlist and, for SecretRefs, a small status observation produced by
the secret provider.  Environment diagnostics use *membership* of ``os.environ``
only; the value is never read.  The returned schema is intentionally small so a
diagnostic can be attached to a deployment report without becoming a credential
exfiltration path.

The module is dependency-free and can be used by a Kubernetes job, a deployment
runbook, or a focused test fixture.  It is not a secret-manager client and must
not be changed into one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "secret-diagnostics.v1"
MAX_ENTRIES = 64
MAX_NAME_LENGTH = 192
MAX_INPUT_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024


class DiagnosticInputError(ValueError):
    """A safe, fixed-code input error.

    Error messages never include a name, path, parser detail, exception text, or
    caller-provided data.  ``code`` is deliberately drawn from a finite set so a
    caller can safely expose it in a deployment event.
    """

    _CODES = frozenset(
        {
            "allowlist-required",
            "allowlist-too-large",
            "allowlist-duplicate",
            "allowlist-invalid",
            "observation-duplicate",
            "observation-unallowlisted",
            "observation-invalid",
            "observation-missing",
            "source-kind-mismatch",
            "input-too-large",
            "invalid-json",
            "invalid-file",
            "sensitive-field",
            "artifact-too-large",
            "artifact-invalid",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            code = "allowlist-invalid"
        self.code = code
        super().__init__(code)


class SourceKind(StrEnum):
    """Supported non-secret source authorities."""

    ENVIRONMENT = "environment"
    SECRET_REF = "secret_ref"  # nosec B105 - a label, not a credential


class Presence(StrEnum):
    UNKNOWN = "unknown"
    PRESENT = "present"
    ABSENT = "absent"


class Health(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class ArtifactStatus(StrEnum):
    SAFE = "safe"
    SECRET_LIKE_CONTENT = "secret-like-content"  # sanitizer:ignore - a fixed status code name, not a secret  # nosec B105 - a label, not a credential
    TOO_LARGE = "artifact-too-large"
    INVALID = "artifact-invalid"
    UNREADABLE = "artifact-unreadable"


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_REF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_\-.])(?:secret|token|password|passwd|credential|api[_-]?key|"
    r"private[_-]?key|authorization|bearer)(?:$|[_\-.])",
    re.IGNORECASE,
)
_SECRET_LIKE_TEXT = (
    re.compile(
        r"(?im)(?:^|[\s,{])(?:[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|"
        r"PASSWD|CREDENTIAL|API[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*)\s*=\s*\S+"
    ),
    re.compile(
        r"(?i)\b(?:authorization\s*:\s*(?:bearer|basic)|"
        r"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,})"
    ),
    re.compile(
        r'(?i)["\'](?:secret|token|password|passwd|credential|api[_-]?key|'
        r'private[_-]?key|authorization)["\']\s*:\s*(?!null|false|true)[^,}\n]+'
    ),
)


def _validate_name(name: str, source_kind: SourceKind) -> None:
    if not isinstance(name, str) or not name or len(name) > MAX_NAME_LENGTH:
        raise DiagnosticInputError("allowlist-invalid")
    pattern = _ENV_NAME if source_kind is SourceKind.ENVIRONMENT else _REF_NAME
    if pattern.fullmatch(name) is None:
        raise DiagnosticInputError("allowlist-invalid")


def _coerce_source_kind(value: object) -> SourceKind:
    if not isinstance(value, str):
        raise DiagnosticInputError("source-kind-mismatch")
    try:
        return SourceKind(value)
    except ValueError as exc:
        del exc
        raise DiagnosticInputError("source-kind-mismatch") from None


@dataclass(frozen=True, slots=True)
class AllowedSecret:
    """One explicitly approved public identifier.

    The object has no field for a value, path contents, or provider payload.  A
    SecretRef name is an identifier only; it is not dereferenced here.
    """

    name: str
    source_kind: SourceKind

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, SourceKind):
            raise DiagnosticInputError("source-kind-mismatch")
        _validate_name(self.name, self.source_kind)


@dataclass(frozen=True, slots=True)
class SecretRefObservation:
    """Provider-produced status metadata with no secret-bearing field."""

    name: str
    present: bool
    healthy: bool | None = None

    def __post_init__(self) -> None:
        _validate_name(self.name, SourceKind.SECRET_REF)
        if not isinstance(self.present, bool):
            raise DiagnosticInputError("observation-invalid")
        if self.healthy is not None and not isinstance(self.healthy, bool):
            raise DiagnosticInputError("observation-invalid")
        if not self.present and self.healthy is True:
            raise DiagnosticInputError("observation-invalid")


@dataclass(frozen=True, slots=True)
class DiagnosticEntry:
    """The complete per-identifier public result."""

    name: str
    source_kind: SourceKind
    presence: Presence
    health: Health

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "source_kind": self.source_kind.value,
            "presence": self.presence.value,
            "health": self.health.value,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    """Bounded, serializable, value-free diagnostics."""

    status: str
    entries: tuple[DiagnosticEntry, ...]

    def __post_init__(self) -> None:
        if self.status not in {"complete", "incomplete"}:
            raise DiagnosticInputError("allowlist-invalid")
        if len(self.entries) > MAX_ENTRIES:
            raise DiagnosticInputError("allowlist-too-large")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class ArtifactScan:
    """Only a fixed scan outcome is returned; input content is never echoed."""

    status: ArtifactStatus

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status.value}


def _validated_allowlist(
    allowlist: Sequence[AllowedSecret],
) -> tuple[AllowedSecret, ...]:
    if not allowlist:
        raise DiagnosticInputError("allowlist-required")
    if len(allowlist) > MAX_ENTRIES:
        raise DiagnosticInputError("allowlist-too-large")
    result: list[AllowedSecret] = []
    seen: set[str] = set()
    for item in allowlist:
        if not isinstance(item, AllowedSecret):
            raise DiagnosticInputError("allowlist-invalid")
        if item.name in seen:
            raise DiagnosticInputError("allowlist-duplicate")
        seen.add(item.name)
        result.append(item)
    return tuple(result)


def diagnose(
    allowlist: Sequence[AllowedSecret],
    *,
    environment_names: Collection[str] | None = None,
    secret_refs: Iterable[SecretRefObservation] = (),
) -> DiagnosticReport:
    """Return status for an explicit allowlist without reading secret values.

    ``environment_names`` must contain names only (normally ``os.environ``'s
    keys).  The function never accepts a mapping of environment values.  A
    missing SecretRef observation is represented as ``unknown`` and makes the
    report ``incomplete``; it is never treated as absent or healthy.
    """

    allowed = _validated_allowlist(allowlist)
    names = set(environment_names or ())
    if any(not isinstance(name, str) for name in names):
        raise DiagnosticInputError("observation-invalid")

    by_ref: dict[str, SecretRefObservation] = {}
    for observation in secret_refs:
        if not isinstance(observation, SecretRefObservation):
            raise DiagnosticInputError("observation-invalid")
        if observation.name in by_ref:
            raise DiagnosticInputError("observation-duplicate")
        by_ref[observation.name] = observation

    allowed_by_name = {item.name: item for item in allowed}
    if any(name not in allowed_by_name for name in by_ref):
        raise DiagnosticInputError("observation-unallowlisted")

    entries: list[DiagnosticEntry] = []
    incomplete = False
    for item in allowed:
        if item.source_kind is SourceKind.ENVIRONMENT:
            presence = Presence.PRESENT if item.name in names else Presence.ABSENT
            # Presence is safe to report.  Health would require reading the
            # value, so it is always unknown for an environment source.
            entries.append(
                DiagnosticEntry(
                    name=item.name,
                    source_kind=item.source_kind,
                    presence=presence,
                    health=Health.UNKNOWN,
                )
            )
            continue

        observed = by_ref.get(item.name)
        if observed is None:
            incomplete = True
            entries.append(
                DiagnosticEntry(
                    name=item.name,
                    source_kind=item.source_kind,
                    presence=Presence.UNKNOWN,
                    health=Health.UNKNOWN,
                )
            )
            continue
        presence = Presence.PRESENT if observed.present else Presence.ABSENT
        if observed.healthy is None:
            health = Health.UNKNOWN
        else:
            health = Health.HEALTHY if observed.healthy else Health.UNHEALTHY
        entries.append(
            DiagnosticEntry(
                name=item.name,
                source_kind=item.source_kind,
                presence=presence,
                health=health,
            )
        )

    return DiagnosticReport(
        status="incomplete" if incomplete else "complete",
        entries=tuple(entries),
    )


def _reject_sensitive_mapping_keys(value: object) -> None:
    """Reject arbitrary JSON fields before they can become a report input."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise DiagnosticInputError("sensitive-field")
            normalized = key.replace("-", "_").replace(".", "_").lower()
            if _SENSITIVE_KEY.search(normalized):
                raise DiagnosticInputError("sensitive-field")
            _reject_sensitive_mapping_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_mapping_keys(child)


def allowlist_from_json(value: object) -> tuple[AllowedSecret, ...]:
    """Parse the intentionally tiny allowlist JSON shape."""

    _reject_sensitive_mapping_keys(value)
    if not isinstance(value, list):
        raise DiagnosticInputError("allowlist-invalid")
    result: list[AllowedSecret] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise DiagnosticInputError("allowlist-invalid")
        if set(item) != {"name", "source_kind"}:
            raise DiagnosticInputError("allowlist-invalid")
        result.append(
            AllowedSecret(
                name=item["name"],
                source_kind=_coerce_source_kind(item["source_kind"]),
            )
        )
    return _validated_allowlist(result)


def observations_from_json(value: object) -> tuple[SecretRefObservation, ...]:
    """Parse status-only SecretRef observations."""

    _reject_sensitive_mapping_keys(value)
    if not isinstance(value, list):
        raise DiagnosticInputError("observation-invalid")
    result: list[SecretRefObservation] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise DiagnosticInputError("observation-invalid")
        if set(item) != {"name", "present", "healthy"}:
            raise DiagnosticInputError("observation-invalid")
        result.append(
            SecretRefObservation(
                name=item["name"],
                present=item["present"],
                healthy=item["healthy"],
            )
        )
    if len(result) > MAX_ENTRIES:
        raise DiagnosticInputError("allowlist-too-large")
    return tuple(result)


def _read_json_file(path: Path) -> object:
    try:
        payload = path.read_bytes()
    except (OSError, ValueError):
        raise DiagnosticInputError("invalid-file") from None
    if len(payload) > MAX_INPUT_BYTES:
        raise DiagnosticInputError("input-too-large")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DiagnosticInputError("invalid-json") from None
    return value


def scan_text_artifact(text: str) -> ArtifactScan:
    """Classify a bounded log/artifact without returning its content.

    This is a conservative scanner, not proof that arbitrary text is free of
    secrets.  It catches common assignments, authorization headers, and JSON
    secret fields before an artifact is attached to a deployment report.
    """

    if not isinstance(text, str):
        return ArtifactScan(ArtifactStatus.INVALID)
    if len(text.encode("utf-8", errors="replace")) > MAX_ARTIFACT_BYTES:
        return ArtifactScan(ArtifactStatus.TOO_LARGE)
    if "\x00" in text:
        return ArtifactScan(ArtifactStatus.INVALID)
    if any(pattern.search(text) for pattern in _SECRET_LIKE_TEXT):
        return ArtifactScan(ArtifactStatus.SECRET_LIKE_CONTENT)
    return ArtifactScan(ArtifactStatus.SAFE)


def scan_artifact_file(path: Path) -> ArtifactScan:
    """Scan a file with bounded reads and fixed, non-disclosing outcomes."""

    try:
        with path.open("rb") as handle:
            payload = handle.read(MAX_ARTIFACT_BYTES + 1)
    except (OSError, ValueError):
        return ArtifactScan(ArtifactStatus.UNREADABLE)
    if len(payload) > MAX_ARTIFACT_BYTES:
        return ArtifactScan(ArtifactStatus.TOO_LARGE)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return ArtifactScan(ArtifactStatus.INVALID)
    return scan_text_artifact(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit value-free Arbora secret-reference diagnostics."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--allowlist-file",
        type=Path,
        help="JSON array of {name, source_kind}; no secret values are accepted.",
    )
    group.add_argument(
        "--scan-artifact",
        type=Path,
        help="Scan a bounded log/artifact and emit only a fixed status.",
    )
    parser.add_argument(
        "--secret-ref-status-file",
        type=Path,
        help="Optional JSON array of status-only SecretRef observations.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.scan_artifact is not None:
            result: dict[str, Any] = scan_artifact_file(args.scan_artifact).to_dict()
        else:
            allowlist = allowlist_from_json(_read_json_file(args.allowlist_file))
            observations: tuple[SecretRefObservation, ...] = ()
            if args.secret_ref_status_file is not None:
                observations = observations_from_json(
                    _read_json_file(args.secret_ref_status_file)
                )
            # os.environ is iterated for keys only.  No environment value is
            # read, copied, hashed, measured, or serialized.
            report = diagnose(
                allowlist,
                environment_names=os.environ.keys(),
                secret_refs=observations,
            )
            result = report.to_dict()
        sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
        sys.stdout.write("\n")
        return 0
    except DiagnosticInputError as error:
        # Only the fixed code is safe to expose.  Never print parser/path or
        # exception details, which can contain a credential in a bad artifact.
        sys.stderr.write(json.dumps({"status": error.code}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
