# ruff: noqa: F811
"""Arbora records a connector release from the package index (ARBORA-HOST-R037)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import json
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest
from medulla import EngineResponseError
from scion_sdk.manifest.release_digest import (
    ReleaseDigestError,
    installed_release_evidence,
    wheel_release_evidence,
)
from scion_sdk.runner.errors import ReleaseVerificationError
from scion_sdk.runner.provisioning import verify_release
from scion_sdk.runner.release_pins import MedullaReleasePins

from arbora.fleet.onboarding_capture import FleetEndpoint
from arbora.fleet.release_record import PackageIndex, ReleaseRecorder
from tests.fleet.test_fleet_onboarding import (
    _HEAD_CATALOG,
    ALPHA,
    GRAPH,
    _head_digest,
    _onboarding,
    _run_in_session,
    eg,  # noqa: F401
)

DISTRIBUTION = "fakeconn-mcp"
VERSION = "1.2.3"
SDIST = b"sdist bytes"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def build_wheel(directory: Path, body: bytes = b"VALUE = 1\n") -> Path:
    """A real wheel: package, metadata and a ``RECORD`` with every file's hash."""
    info = f"fakeconn_mcp-{VERSION}.dist-info"
    files = {
        "fakeconn/__init__.py": body,
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {DISTRIBUTION}\nVersion: {VERSION}\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(
        f"{name},sha256={_b64(hashlib.sha256(data).digest())},{len(data)}\n"
        for name, data in files.items()
    )
    files[f"{info}/RECORD"] = (record + f"{info}/RECORD,,\n").encode()
    wheel = directory / f"fakeconn_mcp-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return wheel


class FakeIndex:
    """A loopback package index serving one release's JSON and its wheel."""

    def __init__(self, wheel: Path, *, index_wheel_digest: str | None = None) -> None:
        self.wheel = wheel
        self.hits: list[str] = []
        wheel_digest = (
            index_wheel_digest or hashlib.sha256(wheel.read_bytes()).hexdigest()
        )
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.hits.append(self.path)
                if self.path == f"/pypi/{DISTRIBUTION}/{VERSION}/json":
                    base = f"http://127.0.0.1:{outer.port}"
                    body = json.dumps(
                        {
                            "urls": [
                                {
                                    "packagetype": "bdist_wheel",
                                    "url": f"{base}/files/{wheel.name}",
                                    "digests": {"sha256": wheel_digest},
                                    "upload_time_iso_8601": "2026-11-27T20:15:53.123456Z",
                                },
                                {
                                    "packagetype": "sdist",
                                    "url": f"{base}/files/sdist.tar.gz",
                                    "digests": {
                                        "sha256": hashlib.sha256(SDIST).hexdigest()
                                    },
                                    "upload_time_iso_8601": "2026-11-27T20:15:50.000000Z",
                                },
                            ]
                        }
                    ).encode()
                elif self.path == f"/files/{wheel.name}":
                    body = wheel.read_bytes()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/pypi"


@contextmanager
def serving(index: FakeIndex) -> Iterator[FakeIndex]:
    thread = threading.Thread(target=index.server.serve_forever, daemon=True)
    thread.start()
    try:
        yield index
    finally:
        index.server.shutdown()
        index.server.server_close()


def install(wheel: Path, site: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the wheel the way an installer does: unpack it into a site directory."""
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    monkeypatch.syspath_prepend(str(site))


def engine_records(eg: Any) -> dict[str, dict[str, Any]]:
    """Give the fake engine the engine's own ``Release`` node shape and read answer."""
    nodes: dict[str, dict[str, Any]] = {}

    def record(params: Any, _key: Any) -> Any:
        release = params["release"]
        node_id = f"release:{release['repo']}:{release['version']}"
        body = json.dumps(release, sort_keys=True)
        stored = nodes.get(node_id)
        if stored is not None and stored["release_record"] != body:
            raise EngineResponseError("RELEASE_VERSION_TAKEN")
        nodes[node_id] = {"type": "Release", "release_record": body}
        return {"node_id": node_id}

    def read(params: Any, _key: Any) -> Any:
        return msgpack.packb(nodes.get(params["node_id"], {}))

    eg._on_RecordRelease = record
    eg._on_GetNodeProperties = read
    return nodes


async def served_session(distribution: str) -> Any:
    """What the connector's server serves at ``connector://release``."""
    try:
        evidence = installed_release_evidence(distribution)
    except ReleaseDigestError:
        resources: list[Any] = []  # the server serves nothing for a tampered install
        body = ""
    else:
        resources = [SimpleNamespace(uri="connector://release")]
        body = json.dumps(
            {
                "distribution": evidence.distribution,
                "version": evidence.version,
                "content_sha256": evidence.content_sha256,
            }
        )

    class Session:
        async def list_resources(self) -> list[Any]:
            return resources

        async def read_resource(self, _uri: str) -> str:
            return body

    return Session()


@contextlib.asynccontextmanager
async def served_release_session(_endpoint: FleetEndpoint, *, auth: Any) -> Any:
    """The session onboarding opens to the connector: it serves the installed wheel's release."""
    del auth
    yield await served_session(DISTRIBUTION)


class SpyRecorder:
    """Keeps what the recorder reported, which the refusal alone does not carry."""

    def __init__(self, inner: ReleaseRecorder) -> None:
        self.inner = inner
        self.reports: list[Any] = []

    async def record(self, served: Any, send: Any) -> Any:
        report = await self.inner.record(served, send)
        self.reports.append(report)
        return report


def onboard(eg: Any, index: Any) -> Any:
    """One whole onboarding pass of an already-imported pack, which records the release.

    Returns what the recorder reported; a connector the gate refuses raises.
    """
    digest = asyncio.run(_head_digest(ALPHA, _HEAD_CATALOG))
    eg.heads["alpha-mcp"] = (_HEAD_CATALOG, digest)
    spy = SpyRecorder(ReleaseRecorder(PackageIndex(index.url)))
    onboarding = dataclasses.replace(
        _onboarding(eg), release_recorder=spy, release_session=served_release_session
    )
    _run_in_session(onboarding.onboard(ALPHA, auth=None))
    return spy.reports[-1]


def refused(eg: Any, index: Any) -> tuple[Any, str]:
    """The recording and the typed refusal of a pass whose release cannot be admitted."""
    digest = asyncio.run(_head_digest(ALPHA, _HEAD_CATALOG))
    eg.heads["alpha-mcp"] = (_HEAD_CATALOG, digest)
    spy = SpyRecorder(ReleaseRecorder(PackageIndex(index.url)))
    onboarding = dataclasses.replace(
        _onboarding(eg), release_recorder=spy, release_session=served_release_session
    )
    with pytest.raises(ReleaseVerificationError) as refusal:
        _run_in_session(onboarding.onboard(ALPHA, auth=None))
    assert onboarding.surfaces[ALPHA.name]["onboarding_state"] == "release_refused"
    return spy.reports[-1], refusal.value.code


@pytest.mark.real_release_gate
@pytest.mark.spec("ARBORA-HOST-R037.1")
def test_onboarding_records_the_release_from_the_index_and_the_runner_accepts(
    eg: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    wheel = build_wheel(tmp_path)
    install(wheel, tmp_path / "site", monkeypatch)
    nodes = engine_records(eg)
    with serving(FakeIndex(wheel)) as index:
        report = onboard(eg, index)
        again = onboard(eg, index)

    node_id = f"release:{DISTRIBUTION}:{VERSION}"
    assert report == again == {"state": "recorded", "release": node_id}
    stored = json.loads(nodes[node_id]["release_record"])
    evidence = wheel_release_evidence(wheel)
    assert stored["artifact_digests"] == sorted(
        [
            f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}",
            f"sha256:{hashlib.sha256(SDIST).hexdigest()}",
            evidence.pin,
        ]
    )
    assert (stored["repo"], stored["version"]) == (DISTRIBUTION, VERSION)
    assert stored["published_at"] == 1795810553  # the index's upload time, in seconds
    records = [c for c in eg.calls if c[0] == "RecordRelease"]
    assert [c[1] for c in records] == [GRAPH, GRAPH]  # the connector's own graph
    # The runner reads the pin the way it always does and accepts the installed wheel.
    pins = MedullaReleasePins(eg, GRAPH)
    session = _run_in_session(served_session(DISTRIBUTION))
    _run_in_session(verify_release(session, "alpha", pins))


@pytest.mark.real_release_gate
@pytest.mark.spec("ARBORA-HOST-R037.1")
def test_a_tampered_install_or_an_unrecorded_version_refuses(
    eg: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    wheel = build_wheel(tmp_path)
    site = tmp_path / "site"
    install(wheel, site, monkeypatch)
    nodes = engine_records(eg)
    pins = MedullaReleasePins(eg, GRAPH)

    async def verdict() -> str:
        try:
            await verify_release(await served_session(DISTRIBUTION), "alpha", pins)
        except ReleaseVerificationError as refusal:
            return str(refusal)
        return "accepted"

    assert _run_in_session(verdict()) == "RELEASE_PIN_MISSING"  # nothing recorded yet
    with serving(FakeIndex(wheel)) as index:
        onboard(eg, index)
    assert nodes and _run_in_session(verdict()) == "accepted"
    (site / "fakeconn/__init__.py").write_bytes(b"VALUE = 2\n")  # edited after install
    assert _run_in_session(verdict()) == "RELEASE_DIGEST_UNREPORTED"


@pytest.mark.real_release_gate
@pytest.mark.spec("ARBORA-HOST-R037.2")
def test_a_wheel_that_differs_from_the_index_digest_is_not_recorded(
    eg: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    wheel = build_wheel(tmp_path)
    install(wheel, tmp_path / "site", monkeypatch)
    nodes = engine_records(eg)
    with serving(FakeIndex(wheel, index_wheel_digest="0" * 64)) as index:
        report, code = refused(eg, index)

    assert code == "RELEASE_PIN_MISSING"  # the gate stops the pass
    assert report == {"state": "not_recorded", "code": "RELEASE_INDEX_DIGEST_MISMATCH"}
    assert not nodes and not [c for c in eg.calls if c[0] == "RecordRelease"]
    pins = MedullaReleasePins(eg, GRAPH)
    session = _run_in_session(served_session(DISTRIBUTION))
    with pytest.raises(ReleaseVerificationError, match="RELEASE_PIN_MISSING"):
        _run_in_session(verify_release(session, "alpha", pins))


@pytest.mark.real_release_gate
@pytest.mark.spec("ARBORA-HOST-R037.2")
def test_an_unreachable_index_or_a_taken_version_is_reported_not_raised(
    eg: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    wheel = build_wheel(tmp_path)
    install(wheel, tmp_path / "site", monkeypatch)
    nodes = engine_records(eg)
    nodes[f"release:{DISTRIBUTION}:{VERSION}"] = {
        "type": "Release",
        "release_record": '{"another": "record"}',
    }
    with serving(FakeIndex(wheel)) as index:
        taken, taken_code = refused(eg, index)
        down, down_code = refused(eg, SimpleNamespace(url="http://127.0.0.1:9/pypi"))

    assert taken_code == down_code == "RELEASE_PIN_MISSING"
    assert taken == {"state": "not_recorded", "code": "RELEASE_VERSION_TAKEN"}
    assert down == {"state": "not_recorded", "code": "RELEASE_INDEX_UNAVAILABLE"}
