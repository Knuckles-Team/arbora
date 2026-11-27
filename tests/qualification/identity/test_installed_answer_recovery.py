"""An answer survives host and native restarts (ARBORA-IDENTITY-R004.9.3.2, L6).

``ARBORA_QUAL_RECOVERY_BASE_URL`` is an installed local anonymous host and
``ARBORA_QUAL_RECOVERY_RESTART`` a command that restarts the host and its native process and
returns when the host serves again. The test sends a message, restarts, and reads the same
task: the committed answer is returned once, never replayed as a second run.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import uuid

import pytest
from _identity_live import request, require


def _rpc(base: str, method: str, params: dict) -> dict:
    status, _, body = request(
        base,
        "POST",
        "/a2a",
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert status == 200
    return json.loads(body)


@pytest.mark.spec("ARBORA-IDENTITY-R004.9.3.2")
def test_replay_survives_restarts() -> None:
    env = require("ARBORA_QUAL_RECOVERY_BASE_URL", "ARBORA_QUAL_RECOVERY_RESTART")
    base = env["ARBORA_QUAL_RECOVERY_BASE_URL"].rstrip("/")
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "text", "text": "reply with the word ready"}],
    }
    first = _rpc(base, "message/send", {"message": message})["result"]
    subprocess.run(  # noqa: S603 - the owner's restart command, never request input
        shlex.split(env["ARBORA_QUAL_RECOVERY_RESTART"]), check=True, timeout=600
    )
    again = _rpc(base, "tasks/get", {"id": first["id"]})["result"]
    assert again["id"] == first["id"]
    assert again["artifacts"] == first.get("artifacts", again["artifacts"])
    replay = _rpc(base, "message/send", {"message": message})["result"]
    assert replay["id"] == first["id"], "the same message must not start a second run"
