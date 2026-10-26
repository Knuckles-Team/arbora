"""Disposable CI-only HTTPS and real Vault assets. Never import in production.

Not executed until the complete installed qualification job receives review.
All secret-bearing files live in a caller-owned mode-0700 temporary directory.
"""

from __future__ import annotations

import contextlib
import datetime
import http.client
import http.server
import ipaddress
import json
import os
import secrets
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def private_write(path: Path, value: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)


def browser_ca(root: Path) -> tuple[Path, Path, Path]:
    """Trust a fresh CA only in this child's HOME/NSS, never system trust."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Disposable browser CI CA")]
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = (
        root / name for name in ("ca.pem", "server.pem", "server.key")
    )
    private_write(ca_path, ca.public_bytes(serialization.Encoding.PEM))
    private_write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    private_write(
        key_path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    nss = root / "home" / ".pki" / "nssdb"
    nss.mkdir(parents=True, mode=0o700)
    for args in (
        ["-N", "--empty-password"],
        ["-A", "-n", "Disposable qualification CA", "-t", "C,,", "-i", str(ca_path)],
    ):
        subprocess.run(
            ["certutil", "-d", f"sql:{nss}", *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return ca_path, cert_path, key_path


@contextlib.contextmanager
def https_origin(port: int, upstream_port: int, cert: Path, key: Path) -> Iterator[str]:
    """Loopback-only TLS terminator, no forwarding-header trust from the browser.

    Uses Uvicorn's existing default loopback proxy admission, without widening
    forwarded-allow-ips or changing production listener/security settings.
    Only bounded HTTP requests are supported; WebSocket qualification is separate.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def dispatch(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1048576 or self.headers.get("Transfer-Encoding"):
                    self.send_error(413)
                    return
                excluded = {"connection", "transfer-encoding", "upgrade", "forwarded"}
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in excluded
                    and not k.lower().startswith("x-forwarded-")
                }
                headers["X-Forwarded-Proto"] = "https"
                headers["X-Forwarded-For"] = "127.0.0.1"
                with contextlib.closing(
                    http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=20)
                ) as conn:
                    conn.request(
                        self.command, self.path, self.rfile.read(length), headers
                    )
                    result = conn.getresponse()
                    body = result.read(16 * 1024 * 1024 + 1)
                    if len(body) > 16 * 1024 * 1024:
                        raise ValueError("oversized upstream response")
                    self.send_response(result.status)
                    for name, value in result.getheaders():
                        if name.lower() not in excluded | {"content-length"}:
                            self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
            except (OSError, ValueError, http.client.HTTPException):
                self.send_error(502)

        do_GET = do_POST = do_HEAD = do_OPTIONS = dispatch

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def vault(root: Path, binary: Path, port: int) -> Iterator[Any]:
    """Real pinned Vault dev server; root token never printed or persisted."""
    import hvac

    token = secrets.token_urlsafe(32)
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "HOME"}}
    env["VAULT_DEV_ROOT_TOKEN_ID"] = token
    process = subprocess.Popen(
        [
            str(binary),
            "server",
            "-dev",
            f"-dev-listen-address=127.0.0.1:{port}",
            "-dev-no-store-token",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = hvac.Client(url=f"http://127.0.0.1:{port}", token=token)
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise RuntimeError("Disposable Vault exited")
            try:
                if client.sys.is_initialized() and not client.sys.is_sealed():
                    break
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Disposable Vault readiness")
            time.sleep(0.1)
        yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def configure_signer(client: Any, private_jwk: dict[str, Any]) -> str:
    """Fixture admin writes once; runtime receives read-only exact-path token."""
    path = "arbora/identity/issuer-signing-keys"
    jwk = {**private_jwk, "alg": "RS256", "use": "sig"}
    client.secrets.kv.v2.create_or_update_secret(
        path=path, secret={"value": json.dumps({"active": jwk})}, mount_point="secret"
    )
    client.sys.create_or_update_policy(
        "browser-signer-reader",
        f'path "secret/data/{path}" {{ capabilities = ["read"] }}',
    )
    return client.auth.token.create(
        policies=["browser-signer-reader"],
        no_default_policy=True,
        ttl="30m",
        renewable=False,
    )["auth"]["client_token"]
