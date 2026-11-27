"""The committed identity probe prints no secret (ARBORA-IDENTITY-R024).

The probe runs against a local stand-in server: discovery, a key set that wrongly includes a
private member, and a login that sets a cookie. The report holds booleans, statuses and a
principal digest only.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SECRET_COOKIE = "session=cookie-value-1234"
PASSWORD = "".join(("probe-", "synthetic-", "value"))
PRINCIPAL = "person:probe-user"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return None

    def _send(self, status: int, body: object, cookie: bool = False) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", SECRET_COOKIE + "; HttpOnly")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/.well-known/openid-configuration":
            self._send(200, {"issuer": "http://issuer.test"})
        elif self.path == "/.well-known/jwks.json":
            self._send(200, {"keys": [{"kty": "RSA", "kid": "k", "d": "private-part"}]})
        elif self.path == "/auth/session":
            ok = self.headers.get("Cookie") == SECRET_COOKIE
            self._send(200 if ok else 401, {"principal": PRINCIPAL} if ok else {})
        else:
            self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        sent = json.loads(self.rfile.read(size) or b"{}")
        if self.path == "/auth/login" and sent.get("password") == PASSWORD:
            self._send(200, {"outcome": "ok"}, cookie=True)
        else:
            self._send(401, {})


def _probe_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "identity_probe.py"
    spec = importlib.util.spec_from_file_location("identity_probe_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join(5)
    httpd.server_close()


@pytest.mark.spec("ARBORA-IDENTITY-R024")
def test_script_output_holds_no_secret(
    server: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARBORA_PROBE_USER", "probe-user")
    monkeypatch.setenv("ARBORA_PROBE_PASSWORD", PASSWORD)
    code = _probe_module().main(["--base-url", server, "--sign-in"])
    out = capsys.readouterr().out
    report = json.loads(out)
    # The key set that carries a private member is refused, and the sign-in still reports.
    assert code == 2 and report["checks"]["jwks"] is False
    assert report["sign_in"]["signed_in"] is True
    assert len(report["sign_in"]["principal_digest"]) == 16
    for secret in (
        PASSWORD,
        SECRET_COOKIE,
        "cookie-value-1234",
        PRINCIPAL,
        "private-part",
    ):
        assert secret not in out


@pytest.mark.spec("ARBORA-IDENTITY-R024")
def test_a_wrong_password_reports_no_session(
    server: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARBORA_PROBE_USER", "probe-user")
    monkeypatch.setenv("ARBORA_PROBE_PASSWORD", "wrong")
    _probe_module().main(["--base-url", server, "--sign-in"])
    report = json.loads(capsys.readouterr().out)
    assert report["sign_in"]["signed_in"] is False and report["ok"] is False
