"""HostContext owns run-time state; two contexts share none (ARBORA-HOST-R039)."""

from __future__ import annotations

import dataclasses

import pytest

from arbora import run_origin
from arbora.a2a import mcp as a2a_mcp
from arbora.mcp_server.host_context import HostContext, build_host_context
from arbora.security.session import GraphSession, use_session
from tests.test_run_origin import session

pytestmark = pytest.mark.spec("ARBORA-HOST-R039")


@pytest.mark.spec("ARBORA-HOST-R039.1")
def test_two_host_contexts_share_no_module_state() -> None:
    first = build_host_context()
    second = build_host_context()
    assert first.runs.origins is not second.runs.origins
    assert first.runs.a2a is not second.runs.a2a
    # The state moved off the modules: no registry or service global remains.
    assert not hasattr(run_origin, "_runs")
    assert not hasattr(a2a_mcp, "_SERVICE")
    assert not hasattr(a2a_mcp, "_service")


def test_host_context_is_frozen_and_slotted() -> None:
    host = build_host_context()
    assert dataclasses.is_dataclass(HostContext)
    with pytest.raises(dataclasses.FrozenInstanceError):
        host.runs = host.runs  # type: ignore[misc]
    assert not hasattr(host, "__dict__")


async def test_a_run_registered_on_one_context_is_invisible_to_the_other() -> None:
    first = build_host_context()
    second = build_host_context()
    admitted: GraphSession = session()
    with (
        use_session(admitted),
        first.runs.origins.agent_run_origin_scope(admitted, "web", "run"),
    ):
        assert len(first.runs.origins) == 1
        assert len(second.runs.origins) == 0
        second.runs.origins.revoke(admitted, "run")
        origin = run_origin._run.get()
        assert origin is not None and origin.is_live()
        first.runs.origins.revoke(admitted, "run")
        assert not origin.is_live()
    assert len(first.runs.origins) == 0


#: One fresh interpreter imports every unit, then lists the module-level objects that
#: hold run-time state: a mutable container, a lock, a client or a thread. A
#: ``ContextVar`` key, a ``tuple``, a ``frozenset`` and a ``MappingProxyType`` are
#: not listed. It also fingerprints the MCP toolset class around the import of
#: ``fleet.protocol_compat`` and around ``build_host_context``.
_IMPORT_SURVEY = """
import collections, collections.abc, importlib, json, pkgutil, threading, asyncio
import warnings
warnings.simplefilter("ignore")
import arbora

try:
    from pydantic_ai.mcp import MCPToolset
except ImportError:
    MCPToolset = None

def fingerprint():
    return None if MCPToolset is None else {
        name: id(value) for name, value in vars(MCPToolset).items()
    }

before = fingerprint()
state_types = (
    dict, list, set, bytearray, collections.deque,
    collections.abc.MutableMapping, collections.abc.MutableSequence,
    collections.abc.MutableSet,
    type(threading.Lock()), type(threading.RLock()), threading.Event,
    threading.Condition, threading.Semaphore, threading.Thread,
    asyncio.Lock, asyncio.Event, asyncio.Queue,
)
try:
    import httpx
    state_types += (httpx.Client, httpx.AsyncClient)
except ImportError:
    pass
found, failed, imported = [], [], 0
for unit in pkgutil.walk_packages(arbora.__path__, "arbora."):
    try:
        module = importlib.import_module(unit.name)
    except BaseException as exc:
        failed.append([unit.name, type(exc).__name__])
        continue
    imported += 1
    for name, value in vars(module).items():
        if not name.startswith("__") and isinstance(value, state_types):
            found.append([unit.name, name, type(value).__name__])
import arbora.fleet.protocol_compat as compat
after_import = fingerprint()
from arbora.mcp_server.host_context import build_host_context
build_host_context()
print(json.dumps({
    "found": found,
    "failed": failed,
    "imported": imported,
    "patched_at_import": before != after_import,
    "patched_by_build": after_import != fingerprint(),
    "compat_names": sorted(vars(compat)),
}))
"""


@pytest.mark.spec("ARBORA-HOST-R039.13")
def test_import_of_every_unit_creates_no_mutable_module_state() -> None:
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), *sys.path])}
    done = subprocess.run(
        [sys.executable, "-c", _IMPORT_SURVEY],
        capture_output=True,
        text=True,
        env=environment,
        cwd=root,
        timeout=600,
        check=True,
    )
    survey = json.loads(done.stdout.strip().splitlines()[-1])
    assert survey["imported"] > 500
    assert survey["failed"] == []
    assert survey["found"] == [], survey["found"]
    # The MCP compatibility module patches nothing: not at import, not in the build,
    # and it keeps no installer or flag that could.
    assert not survey["patched_at_import"]
    assert not survey["patched_by_build"]
    assert not any(name.startswith("install_") for name in survey["compat_names"])
