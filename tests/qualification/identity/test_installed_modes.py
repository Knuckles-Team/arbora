"""Each identity mode starts or fails closed on an installed host (ARBORA-IDENTITY-R004.7.2, L6).

``ARBORA_QUAL_MODE_HOSTS`` is a JSON object of mode name to the base URL of an installed host
started in that mode, or to ``null`` for a mode the install refused to start. A started host
publishes its discovery document and refuses ``/auth/session`` without a session; a refused
start answers nothing.
"""

from __future__ import annotations

import json

import pytest
from _identity_live import request, require


@pytest.mark.spec("ARBORA-IDENTITY-R004.7.2")
def test_each_mode_starts_or_fails_closed() -> None:
    hosts = json.loads(require("ARBORA_QUAL_MODE_HOSTS")["ARBORA_QUAL_MODE_HOSTS"])
    assert hosts, "name at least one identity mode"
    for mode, base in sorted(hosts.items()):
        if base is None:
            continue
        status, _, _ = request(base, "GET", "/auth/session")
        assert status in {401, 403}, f"{mode}: an installed host must refuse no session"
        discovery, _, _ = request(base, "GET", "/.well-known/openid-configuration")
        assert discovery in {200, 404}, f"{mode}: discovery answers or is absent"
