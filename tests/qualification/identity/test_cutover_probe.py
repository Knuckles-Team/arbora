"""The cutover probe answers for the renamed ingress origin (ARBORA-INGRESS-R001.4).

A loopback origin stands in for the new hostname: it serves the issuer
discovery document and key set and refuses a request with no session, as the
served application does. The committed probe is run against it through its own
command line. The browser sign-in is the live qualification of
ARBORA-IDENTITY-R024 and is not run here.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "identity_probe.py"
PUBLIC_KEY = {"kty": "RSA", "kid": "k1", "n": "abc", "e": "AQAB"}


def _probe() -> Any:
    spec = importlib.util.spec_from_file_location("identity_probe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _origin(
    routes: dict[str, tuple[int, dict[str, Any]]],
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status, body = routes.get(self.path, (404, {}))
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            return

    return Handler


def _serving(routes: dict[str, tuple[int, dict[str, Any]]]) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _origin(routes))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.fixture
def renamed_origin() -> Iterator[str]:
    yield from _serving(
        {
            "/.well-known/openid-configuration": (200, {"issuer": "https://new"}),
            "/.well-known/jwks.json": (200, {"keys": [PUBLIC_KEY]}),
            "/auth/session": (401, {}),
        }
    )


@pytest.fixture
def broken_origin() -> Iterator[str]:
    yield from _serving(
        {
            "/.well-known/openid-configuration": (200, {"issuer": "https://new"}),
            "/.well-known/jwks.json": (200, {"keys": [{**PUBLIC_KEY, "d": "secret"}]}),
            "/auth/session": (200, {}),
        }
    )


@pytest.mark.spec("ARBORA-INGRESS-R001.4")
def test_cutover_probe_signs_in_on_new_hostname(
    renamed_origin: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _probe().main(["--base-url", renamed_origin]) == 0
    printed = capsys.readouterr().out
    out = json.loads(printed)
    assert out["ok"] is True
    assert out["checks"] == {
        "discovery": True,
        "jwks": True,
        "session_requires_login": True,
    }
    # The output holds statuses only: no key material, cookie or credential.
    assert PUBLIC_KEY["n"] not in printed and "cookie" not in printed.lower()


@pytest.mark.spec("ARBORA-INGRESS-R001.4")
def test_cutover_probe_fails_when_the_new_origin_leaks_a_key_or_a_session(
    broken_origin: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _probe().main(["--base-url", broken_origin]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["checks"]["jwks"] is False
    assert out["checks"]["session_requires_login"] is False
