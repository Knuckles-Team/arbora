"""Record a connector release from the public package index (ARBORA-HOST-R037).

A connector's integrity pin is the digest set of one published release. Arbora
does not wait for a release job to post it: when it onboards a connector
version it reads that version's JSON metadata from the package index, downloads
the wheel, checks the wheel's sha256 against the index, computes the wheel
content digest with the connector SDK's own ``wheel_release_evidence``, and
writes one sealed engine ``Release`` record through ``RecordRelease``. The
runner then verifies the connector it runs against that record
(``SCION-CONNECTOR-CONTROL-R650.1.3``).

No credential, endpoint or key takes part. The index serves each file's sha256
over TLS and the record is create-only, so a version's pin cannot be replaced.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from scion_sdk.manifest.release_digest import (
    ReleaseDigestError,
    ReleaseEvidence,
    wheel_release_evidence,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PACKAGE_INDEX",
    "PackageIndex",
    "PublishedRelease",
    "ReleaseRecordRefused",
    "ReleaseRecorder",
    "release_request",
]

#: The public package index's JSON metadata root.
PACKAGE_INDEX = "https://pypi.org/pypi"
#: The largest wheel arbora downloads to hash.
MAX_WHEEL_BYTES = 256 * 1024 * 1024
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_HEX = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseRecordRefused(RuntimeError):
    """A release could not be recorded; ``code`` is the stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PublishedRelease:
    """What the package index publishes for one distribution version."""

    wheel_url: str
    wheel_sha256: str
    sdist_sha256: str | None
    published_at: int


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _published_at(value: object) -> int:
    try:
        return int(
            datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        )
    except ValueError:
        raise ReleaseRecordRefused("RELEASE_INDEX_INCOMPLETE") from None


def _file_digest(entry: Mapping[str, Any]) -> str:
    digest = (entry.get("digests") or {}).get("sha256")
    if not isinstance(digest, str) or not _HEX.fullmatch(digest):
        raise ReleaseRecordRefused("RELEASE_INDEX_INCOMPLETE")
    return digest


def _files(document: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    entries = document.get("urls") or ()
    return [
        f for f in entries if isinstance(f, Mapping) and f.get("packagetype") == kind
    ]


def _unique_files(
    document: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
    """The one wheel and at most one sdist of an index document; anything else refuses."""
    wheels, sdists = _files(document, "bdist_wheel"), _files(document, "sdist")
    if len(wheels) != 1 or len(sdists) > 1:
        raise ReleaseRecordRefused("RELEASE_INDEX_INCOMPLETE")
    if any(f.get("yanked") for f in (*wheels, *sdists)):
        raise ReleaseRecordRefused("RELEASE_INDEX_INCOMPLETE")
    return wheels[0], (sdists[0] if sdists else None)


def parse_index_release(document: Mapping[str, Any]) -> PublishedRelease:
    """The wheel and sdist digests of one index JSON document; ambiguity refuses."""
    wheel, sdist = _unique_files(document)
    url = wheel.get("url")
    if not isinstance(url, str):
        raise ReleaseRecordRefused("RELEASE_INDEX_INCOMPLETE")
    return PublishedRelease(
        wheel_url=url,
        wheel_sha256=_file_digest(wheel),
        sdist_sha256=_file_digest(sdist) if sdist is not None else None,
        published_at=_published_at(wheel.get("upload_time_iso_8601")),
    )


@dataclass(frozen=True, slots=True)
class PackageIndex:
    """The package index's JSON metadata and file downloads, over TLS."""

    base_url: str = PACKAGE_INDEX
    timeout_seconds: float = 30.0

    def _origin_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme == "https":
            return True
        return parts.scheme == "http" and parts.hostname in _LOOPBACK

    async def release(self, distribution: str, version: str) -> PublishedRelease:
        """The index's published files for one exact distribution version."""
        if not self._origin_allowed(self.base_url):
            raise ReleaseRecordRefused("RELEASE_INDEX_UNAVAILABLE")
        url = f"{self.base_url.rstrip('/')}/{distribution}/{version}/json"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
                response = await http.get(url)
                response.raise_for_status()
                document = response.json()
        except (httpx.HTTPError, ValueError):
            raise ReleaseRecordRefused("RELEASE_INDEX_UNAVAILABLE") from None
        if not isinstance(document, Mapping):
            raise ReleaseRecordRefused("RELEASE_INDEX_INCOMPLETE")
        return parse_index_release(document)

    async def download_wheel(self, release: PublishedRelease, target: Path) -> None:
        """Download the wheel to ``target``; a digest other than the index's refuses."""
        if not self._origin_allowed(release.wheel_url):
            raise ReleaseRecordRefused("RELEASE_INDEX_INCOMPLETE")
        digest, size = hashlib.sha256(), 0
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
                async with http.stream("GET", release.wheel_url) as response:
                    response.raise_for_status()
                    with target.open("wb") as out:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_WHEEL_BYTES:
                                raise ReleaseRecordRefused("RELEASE_INDEX_INCOMPLETE")
                            digest.update(chunk)
                            out.write(chunk)
        except httpx.HTTPError:
            raise ReleaseRecordRefused("RELEASE_INDEX_UNAVAILABLE") from None
        if digest.hexdigest() != release.wheel_sha256:
            raise ReleaseRecordRefused("RELEASE_INDEX_DIGEST_MISMATCH")


def release_request(
    evidence: ReleaseEvidence, release: PublishedRelease
) -> dict[str, Any]:
    """The engine ``RecordRelease`` request for one indexed release.

    ``artifact_digests`` are the strings ``connector-release-digest`` prints:
    ``sha256:`` for the wheel and the sdist and ``content-sha256:`` for the wheel
    content digest. The package index names no source commit, so the record
    carries none (``changeset_head`` and ``git_ref`` are empty).
    """
    digests = {f"sha256:{release.wheel_sha256}", evidence.pin}
    if release.sdist_sha256 is not None:
        digests.add(f"sha256:{release.sdist_sha256}")
    return {
        "repo": evidence.distribution,
        "version": evidence.version,
        "git_ref": "",
        "changeset_head": "",
        "artifact_digests": sorted(digests),
        "published_at": release.published_at,
    }


RecordSender = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(slots=True)
class ReleaseRecorder:
    """Record each connector version once from the package index.

    ``record`` is called by onboarding with the distribution and version a
    connector serves at ``connector://release``. A version already recorded in
    this process is not fetched again; the engine itself replays an identical
    record and refuses a different one.
    """

    index: PackageIndex = field(default_factory=PackageIndex)
    recorded: dict[tuple[str, str], str] = field(default_factory=dict)

    async def record(
        self, served: ReleaseEvidence, send: RecordSender
    ) -> Mapping[str, Any]:
        """Record ``served``'s release; return the report surface of the attempt."""
        key = (_normalised(served.distribution), served.version)
        if key in self.recorded:
            return {"state": "recorded", "release": self.recorded[key]}
        try:
            release = await self.index.release(served.distribution, served.version)
            with tempfile.TemporaryDirectory(prefix="arbora-release-") as directory:
                wheel = Path(directory) / "connector.whl"
                await self.index.download_wheel(release, wheel)
                try:
                    evidence = wheel_release_evidence(wheel)
                except ReleaseDigestError:
                    raise ReleaseRecordRefused("RELEASE_WHEEL_INVALID") from None
            if (_normalised(evidence.distribution), evidence.version) != key:
                raise ReleaseRecordRefused("RELEASE_WHEEL_INVALID")
            await send(release_request(evidence, release))
        except ReleaseRecordRefused as refused:
            logger.warning(
                "%s %s: release not recorded: %s",
                served.distribution,
                served.version,
                refused.code,
            )
            return {"state": "not_recorded", "code": refused.code}
        node = f"release:{evidence.distribution}:{evidence.version}"
        self.recorded[key] = node
        return {"state": "recorded", "release": node}
