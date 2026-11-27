#!/usr/bin/env python3
"""HTTP-level identity probe for the cutover runbook (ARBORA-IDENTITY-R015).

Checks, against one deployment, that the issuer publishes its discovery document and key
set, and that ``/auth/session`` refuses a request that carries no session. It prints one
JSON object of statuses and booleans only: never a cookie, token, credential or code. It
exits 0 when every check passes and 2 otherwise. The real browser sign-in of
``ARBORA-IDENTITY-R024`` is a separate qualification; this probe never replaces it.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import urllib.parse
from collections.abc import Sequence

TIMEOUT_S = 10.0


def _get(url: str) -> tuple[int, bytes]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return 0, b""
    connect = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        connection = connect(parts.hostname, parts.port, timeout=TIMEOUT_S)
        try:
            connection.request("GET", target or "/")
            reply = connection.getresponse()
            return reply.status, reply.read(1 << 20)
        finally:
            connection.close()
    except (http.client.HTTPException, TimeoutError, OSError):
        return 0, b""


def probe(base_url: str) -> dict[str, object]:
    base = base_url.rstrip("/")
    checks: dict[str, object] = {}
    status, body = _get(f"{base}/.well-known/openid-configuration")
    document: dict[str, object] = {}
    if status == 200:
        try:
            document = json.loads(body)
        except ValueError:
            document = {}
    checks["discovery"] = status == 200 and "issuer" in document
    status, body = _get(f"{base}/.well-known/jwks.json")
    keys: object = []
    if status == 200:
        try:
            keys = json.loads(body).get("keys", [])
        except (ValueError, AttributeError):
            keys = []
    checks["jwks"] = (
        isinstance(keys, list)
        and bool(keys)
        and all(isinstance(k, dict) and "d" not in k for k in keys)
    )
    status, _ = _get(f"{base}/auth/session")
    checks["session_requires_login"] = status in {401, 403}
    return checks


def _connect(parts: urllib.parse.SplitResult) -> http.client.HTTPConnection:
    connect = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    return connect(parts.hostname, parts.port, timeout=TIMEOUT_S)


def _login_cookie(
    connection: http.client.HTTPConnection, origin: str, username: str, password: str
) -> tuple[int, str]:
    """Post the credentials with the exact origin; the status and the cookie pair received."""
    connection.request(
        "POST",
        "/auth/login",
        json.dumps({"username": username, "password": password}),
        {"Content-Type": "application/json", "Origin": origin},
    )
    reply = connection.getresponse()
    reply.read(1 << 16)
    return reply.status, (reply.getheader("Set-Cookie") or "").split(";", 1)[0]


def _session_principal(
    connection: http.client.HTTPConnection, cookie: str
) -> tuple[int, str]:
    connection.request("GET", "/auth/session", headers={"Cookie": cookie})
    reply = connection.getresponse()
    payload = json.loads(reply.read(1 << 16) or b"{}")
    return reply.status, str(payload.get("principal") or payload.get("subject") or "")


def sign_in(base_url: str, username: str, password: str) -> dict[str, object]:
    """One committed sign-in against a deployment (``ARBORA-IDENTITY-R024``).

    The report holds a status and a digest of the session principal; the cookie, the
    password and the principal name never leave this function.
    """
    parts = urllib.parse.urlsplit(base_url.rstrip("/"))
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return {"signed_in": False}
    try:
        connection = _connect(parts)
        try:
            status, cookie = _login_cookie(
                connection, f"{parts.scheme}://{parts.netloc}", username, password
            )
            if status != 200 or not cookie:
                return {"signed_in": False, "login_status": status}
            code, principal = _session_principal(connection, cookie)
        finally:
            connection.close()
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        return {"signed_in": False}
    digest = hashlib.sha256(principal.encode()).hexdigest()[:16] if principal else ""
    return {"signed_in": code == 200 and bool(principal), "principal_digest": digest}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=("oidc", "saml"), default="oidc")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--realm", default="")
    parser.add_argument(
        "--sign-in",
        action="store_true",
        help="also sign in with ARBORA_PROBE_USER and ARBORA_PROBE_PASSWORD",
    )
    args = parser.parse_args(argv)
    if args.protocol != "oidc":
        print(
            json.dumps({"protocol": args.protocol, "ok": False, "reason": "unprobed"})
        )
        return 2
    report, ok = _report(args)
    print(json.dumps(report))
    return 0 if ok else 2


def _report(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    checks = probe(args.base_url)
    ok = all(value is True for value in checks.values())
    report: dict[str, object] = {"protocol": args.protocol, "checks": checks}
    if args.sign_in:
        outcome = sign_in(
            args.base_url,
            os.environ.get("ARBORA_PROBE_USER", ""),
            os.environ.get("ARBORA_PROBE_PASSWORD", ""),
        )
        report["sign_in"] = outcome
        ok = ok and outcome.get("signed_in") is True
    report["ok"] = ok
    return report, ok


if __name__ == "__main__":
    sys.exit(main())
