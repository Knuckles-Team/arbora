"""The actual isolated installed/native browser gate, never a source substitute."""

import json
import os
import signal
from pathlib import Path

import pytest


@pytest.mark.spec("ARBORA-IDENTITY-R004.4.2")
@pytest.mark.spec("ARBORA-IDENTITY-R004.5.3")
def test_installed_native_browser_lifecycle():
    if os.environ.get("ARBORA_BROWSER_QUALIFICATION") != "1":
        pytest.skip("requires the isolated installed-browser qualification job")
    import qualify

    previous = {
        sig: signal.signal(sig, qualify.interrupted)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        qualify.main()
        receipt = json.loads(
            (Path(os.environ["QUALIFICATION_OUTPUT"]) / "receipt.json").read_text()
        )
        assert receipt["status"] == "passed"
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
