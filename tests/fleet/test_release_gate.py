# ruff: noqa: F811
"""Every connector launch is verified at one gate (ARBORA-HOST-R037.3)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
from pathlib import Path
from typing import Any

import pytest
from scion_sdk.runner.errors import ReleaseVerificationError

from arbora.fleet import child_transport as child_transport_module
from arbora.fleet.onboarding_capture import FleetEndpoint
from arbora.fleet.release_gate import install_release_gate
from arbora.fleet.release_record import PackageIndex, ReleaseRecorder
from arbora.security.session import use_session
from tests.fleet.test_fleet_onboarding import (
    _HEAD_CATALOG,
    ALPHA,
    _head_digest,
    _onboarding,
    _session,
    eg,  # noqa: F401
)
from tests.fleet.test_multiplexer_transports import _mux, transports  # noqa: F401
from tests.fleet.test_release_record import (
    DISTRIBUTION,
    VERSION,
    FakeIndex,
    build_wheel,
    engine_records,
    install,
    served_session,
    serving,
)

pytestmark = [
    pytest.mark.real_release_gate,
    pytest.mark.spec("ARBORA-HOST-R037.3"),
]


class _ChildSession:
    """What a launched child serves: its release resource, and a tool list that is counted."""

    tool_lists = 0

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._served: Any = None

    async def __aenter__(self) -> _ChildSession:
        self._served = await served_session(DISTRIBUTION)
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def initialize(self) -> None:
        return None

    async def list_resources(self) -> Any:
        return await self._served.list_resources()

    async def read_resource(self, uri: str) -> str:
        return await self._served.read_resource(uri)

    async def list_tools(self) -> Any:
        type(self).tool_lists += 1
        return type("Listed", (), {"tools": [], "nextCursor": None})()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _edit_installed(site: Path, *, rewrite_record: bool) -> None:
    """Edit an installed file; optionally re-sign the wheel's ``RECORD`` to match."""
    target = site / "fakeconn/__init__.py"
    target.write_bytes(b"VALUE = 2\n")
    if not rewrite_record:
        return
    record = site / f"fakeconn_mcp-{VERSION}.dist-info/RECORD"
    lines = []
    for line in record.read_text().splitlines():
        if line.startswith("fakeconn/__init__.py,"):
            data = target.read_bytes()
            line = f"fakeconn/__init__.py,sha256={_b64(hashlib.sha256(data).digest())},{len(data)}"
        lines.append(line)
    record.write_text("\n".join(lines) + "\n")


@pytest.fixture
def gate(eg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    """The process gate, composed as the host composes it, over a recorded engine."""
    wheel = build_wheel(tmp_path)
    install(wheel, tmp_path / "site", monkeypatch)
    engine_records(eg)
    monkeypatch.setattr(child_transport_module, "ClientSession", _ChildSession)
    _ChildSession.tool_lists = 0
    index = FakeIndex(wheel)
    with serving(index):
        recorder = ReleaseRecorder(PackageIndex(index.url))
        onboarding = dataclasses.replace(_onboarding(eg), release_recorder=recorder)
        install_release_gate(onboarding.release_gate)
        yield onboarding, tmp_path / "site"


async def _launch(mux: Any) -> Any:
    with use_session(_session()):
        return await mux._start_child("fakeconn", {"command": "arbora", "args": []})


async def _open(mux: Any) -> None:
    async with contextlib.AsyncExitStack() as stack:
        with use_session(_session()):
            await mux.transport._open_one_session(
                "fakeconn", {"command": "arbora", "args": []}, stack
            )


def test_a_child_started_through_the_launch_path_is_admitted_when_intact(
    transports: Any, gate: Any, tmp_path: Path
) -> None:
    mux = _mux(tmp_path / "c.json")
    started = asyncio.run(_launch(mux))

    assert started is not None
    assert _ChildSession.tool_lists >= 1
    assert mux.transport.release_refusals == {}


@pytest.mark.parametrize(
    ("rewrite_record", "code"),
    [(False, "RELEASE_DIGEST_UNREPORTED"), (True, "RELEASE_DIGEST_MISMATCH")],
)
def test_a_child_whose_installed_file_was_edited_is_refused_before_any_tool_call(
    transports: Any, gate: Any, tmp_path: Path, rewrite_record: bool, code: str
) -> None:
    _onboarding_, site = gate
    mux = _mux(tmp_path / "c.json")
    assert asyncio.run(_launch(mux)) is not None  # intact: pinned and admitted
    _ChildSession.tool_lists = 0
    _edit_installed(site, rewrite_record=rewrite_record)

    with pytest.raises(ReleaseVerificationError) as refusal:
        asyncio.run(_open(mux))

    assert refusal.value.code == code
    assert mux.transport.release_refusals == {"fakeconn": code}
    assert mux.status_snapshot()["release_refusals"] == {"fakeconn": code}
    assert _ChildSession.tool_lists == 0


def test_a_child_installed_outside_arbora_has_no_pin_and_is_refused(
    transports: Any,
    eg: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    wheel = build_wheel(tmp_path)
    install(wheel, tmp_path / "site", monkeypatch)
    engine_records(eg)
    monkeypatch.setattr(child_transport_module, "ClientSession", _ChildSession)
    # The package index does not publish this version: nothing can be recorded.
    recorder = ReleaseRecorder(PackageIndex("http://127.0.0.1:9/pypi"))
    onboarding = dataclasses.replace(_onboarding(eg), release_recorder=recorder)
    install_release_gate(onboarding.release_gate)
    mux = _mux(tmp_path / "c.json")

    with pytest.raises(ReleaseVerificationError) as refusal:
        asyncio.run(_open(mux))

    assert refusal.value.code == "RELEASE_PIN_MISSING"


def test_a_process_with_no_gate_refuses_every_launch(
    transports: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(child_transport_module, "ClientSession", _ChildSession)
    install_release_gate(None)
    mux = _mux(tmp_path / "c.json")

    with pytest.raises(ReleaseVerificationError) as refusal:
        asyncio.run(_open(mux))

    assert refusal.value.code == "RELEASE_PIN_UNAVAILABLE"


def test_onboarding_refuses_an_edited_install_before_it_reads_or_imports_a_pack(
    gate: Any,
    eg: Any,
    tmp_path: Path,  # noqa: F811
) -> None:
    onboarding, site = gate
    captured: list[str] = []

    async def capture(endpoint: FleetEndpoint, *, auth: Any) -> Any:
        captured.append(endpoint.name)
        raise AssertionError("no pack is read from a refused connector")

    @contextlib.asynccontextmanager
    async def session(_endpoint: FleetEndpoint, *, auth: Any) -> Any:
        yield await served_session(DISTRIBUTION)

    digest = asyncio.run(_head_digest(ALPHA, _HEAD_CATALOG))
    eg.heads["alpha-mcp"] = (_HEAD_CATALOG, digest)
    onboarding = dataclasses.replace(
        onboarding, capture=capture, release_session=session
    )
    _edit_installed(site, rewrite_record=True)

    with pytest.raises(ReleaseVerificationError) as refusal:
        with use_session(_session()):
            asyncio.run(onboarding.onboard(ALPHA, auth=None))

    assert refusal.value.code == "RELEASE_DIGEST_MISMATCH"
    report = onboarding.surfaces[ALPHA.name]
    assert (report["onboarding_state"], report["cause"]) == (
        "release_refused",
        "RELEASE_DIGEST_MISMATCH",
    )
    assert not captured
    assert not [c for c in eg.calls if c[0].startswith("ConnectorPack")]
