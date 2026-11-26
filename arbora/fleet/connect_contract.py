"""The connector contract captured on first connect (ARBORA-FLEET-R036.1, R036.2).

A record of the digests of what a connector served: its manifest, each
ontology body and its seed. Arbora sends the whole captured pack through the
existing ``import_attested_pack`` call. The engine replaces only the changed
parts and answers ``unchanged`` for equal digests, so arbora keeps no second
diff authority: :func:`contract_delta` only reports which resource keys moved,
for the status read.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CONTRACT_REJECTION_CODES",
    "ContractRefused",
    "ConnectContract",
    "check_resource_sizes",
    "contract_rejection_code",
    "build_connect_contract",
    "contract_delta",
    "contract_from_pack",
    "digest_of",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def digest_of(body: str | bytes) -> str:
    data = body.encode() if isinstance(body, str) else body
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ConnectContract:
    connector: str
    manifest_digest: str
    ontology_digests: Mapping[str, str] = field(default_factory=dict)
    seed_digest: str = ""
    observed_at_ms: int = 0

    def __post_init__(self) -> None:
        if not self.connector.strip():
            raise ValueError("a connect contract names its connector")
        digests = [self.manifest_digest, *self.ontology_digests.values()]
        if self.seed_digest:
            digests.append(self.seed_digest)
        if any(not _HEX64.match(value) for value in digests):
            raise ValueError("every digest is lowercase SHA-256 hex")
        object.__setattr__(self, "ontology_digests", dict(self.ontology_digests))

    def keys(self) -> dict[str, str]:
        """Resource key to digest: ``manifest``, ``ontology:<uri>`` and ``seed``."""
        keys = {"manifest": self.manifest_digest}
        keys.update({f"ontology:{uri}": d for uri, d in self.ontology_digests.items()})
        if self.seed_digest:
            keys["seed"] = self.seed_digest
        return keys

    def as_dict(self) -> dict[str, object]:
        return {
            "connector": self.connector,
            "manifest_digest": self.manifest_digest,
            "ontology_digests": dict(self.ontology_digests),
            "seed_digest": self.seed_digest,
            "observed_at_ms": self.observed_at_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ConnectContract:
        return cls(**{name: data[name] for name in cls.__dataclass_fields__})  # type: ignore[arg-type]


def build_connect_contract(
    connector: str,
    manifest: str | bytes,
    ontologies: Mapping[str, str | bytes],
    seed: str | bytes | None,
    *,
    observed_at_ms: int,
) -> ConnectContract:
    """Digest the resources of one captured pack; an absent seed leaves it empty."""
    return ConnectContract(
        connector=connector,
        manifest_digest=digest_of(manifest),
        ontology_digests={uri: digest_of(body) for uri, body in ontologies.items()},
        seed_digest="" if seed is None else digest_of(seed),
        observed_at_ms=observed_at_ms,
    )


def contract_delta(
    previous: ConnectContract | None, current: ConnectContract
) -> frozenset[str]:
    """The resource keys whose digest differs, appeared or disappeared."""
    before = previous.keys() if previous is not None else {}
    after = current.keys()
    return frozenset(
        key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
    )


def _body_bytes(body: Any) -> bytes:
    if isinstance(body, bytes | bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body, sort_keys=True, default=str).encode()


_MANIFEST_URI = "connector://resources"
_SEED_URI = "connector://seed"


def contract_from_pack(
    connector: str, pack: Any, *, observed_at_ms: int
) -> ConnectContract:
    """The contract of one captured pack: manifest, ontology bodies and seed."""
    manifest: bytes = b""
    seed: bytes | None = None
    ontologies: dict[str, bytes] = {}
    for entry in pack.archive.entries:
        uri = str(getattr(entry, "uri", "") or "")
        body = _body_bytes(getattr(entry, "body", b""))
        if uri == _MANIFEST_URI:
            manifest = body
        elif uri == _SEED_URI:
            seed = body
        elif uri.startswith("ontology:"):
            ontologies[uri] = body
    return build_connect_contract(
        connector, manifest, ontologies, seed, observed_at_ms=observed_at_ms
    )


#: The engine codes that mark a connector ``contract_rejected``.
CONTRACT_REJECTION_CODES = (
    "ONTOLOGY_INVALID",
    "SHACL_VIOLATION",
    "SEED_SHACL_VIOLATION",
)


class ContractRefused(ValueError):
    """A connector contract refusal with its stable code (ARBORA-FLEET-R036.4)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def check_resource_sizes(pack: Any, max_bytes: int | None = None) -> None:
    """Refuse a contract resource over the configured size before any engine call."""
    if max_bytes is None:
        from arbora.core.settings import read_field

        max_bytes = int(read_field("contract_resource_max_bytes"))
    for entry in pack.archive.entries:
        uri = str(getattr(entry, "uri", "") or "")
        if uri == _MANIFEST_URI or uri == _SEED_URI or uri.startswith("ontology:"):
            body = getattr(entry, "body", b"")
            # A captured pack holds each body as a ``PackSection`` locator.
            length = getattr(body, "length", None)
            size = length if isinstance(length, int) else len(_body_bytes(body))
            if size > max_bytes:
                raise ContractRefused("CONTRACT_RESOURCE_TOO_LARGE", uri)


def contract_rejection_code(error: BaseException) -> str | None:
    """The engine contract code named in one import refusal, if any."""
    text = str(error)
    for code in ("SEED_SHACL_VIOLATION", "SHACL_VIOLATION", "ONTOLOGY_INVALID"):
        if code in text:
            return code
    return None
