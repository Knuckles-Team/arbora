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
import http.client
import json
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=("oidc", "saml"), default="oidc")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--realm", default="")
    args = parser.parse_args(argv)
    if args.protocol != "oidc":
        print(
            json.dumps({"protocol": args.protocol, "ok": False, "reason": "unprobed"})
        )
        return 2
    checks = probe(args.base_url)
    ok = all(value is True for value in checks.values())
    print(json.dumps({"protocol": args.protocol, "ok": ok, "checks": checks}))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
