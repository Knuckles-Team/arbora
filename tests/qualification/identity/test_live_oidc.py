"""Live OIDC sign-in against a test realm (ARBORA-IDENTITY-R024, level L6)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _identity_live import require

PROBE = Path(__file__).resolve().parents[3] / "scripts" / "identity_probe.py"


@pytest.mark.spec("ARBORA-IDENTITY-R024")
def test_live_oidc_sign_in() -> None:
    env = require(
        "ARBORA_QUAL_OIDC_BASE_URL", "ARBORA_PROBE_USER", "ARBORA_PROBE_PASSWORD"
    )
    done = subprocess.run(  # noqa: S603 - the committed probe, fixed argv
        [
            sys.executable,
            "-I",
            str(PROBE),
            "--base-url",
            env["ARBORA_QUAL_OIDC_BASE_URL"],
            "--sign-in",
        ],
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout
    report = json.loads(done.stdout)
    assert report["ok"] is True and report["sign_in"]["signed_in"] is True
    assert report["sign_in"]["principal_digest"]
    for secret in (env["ARBORA_PROBE_PASSWORD"], env["ARBORA_PROBE_USER"]):
        assert secret not in done.stdout + done.stderr
