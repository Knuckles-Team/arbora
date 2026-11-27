"""The fleet takes host objects as arguments and never reaches the host (ARBORA-HOST-R038.2)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

_PROBE = """
import json, sys
import arbora.fleet.multiplexer
import arbora.fleet.shared_multiplexer
import arbora.gateway.daemon
import arbora.deployment.self_deploy
print(json.dumps(sorted(m for m in sys.modules if m.startswith("arbora.mcp_server"))))
"""


@pytest.mark.spec("ARBORA-HOST-R038.2")
def test_multiplexer_uses_only_the_engine_it_was_given() -> None:
    """Importing the multiplexer, the gateway daemon and self-deploy loads no host module.

    The engine, the tool table and the effect journal reach these modules as arguments, so
    none of them imports the ``mcp_server`` unit that owned the globals.
    """
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []
