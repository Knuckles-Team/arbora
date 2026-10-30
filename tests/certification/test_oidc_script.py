"""The public certification script owns and shuts down its issuer."""

import json
import selectors
import signal
import ssl
import subprocess
import sys
from pathlib import Path

import httpx
import pytest


@pytest.mark.spec("ARBORA-HOST-R027.3")
def test_script_serves_discovery_and_exits_on_signal():
    script = Path(__file__).resolve().parents[2] / "scripts/certification_oidc.py"
    process = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15), "issuer did not start"
        ready = json.loads(process.stdout.readline())
        context = ssl.create_default_context(cadata=ready["ca_pem"])
        with httpx.Client(verify=context, trust_env=False) as client:
            response = client.get(f"{ready['issuer']}/.well-known/openid-configuration")
            response.raise_for_status()
            assert response.json()["issuer"] == ready["issuer"]
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr.decode(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
