"""Shared gate of the live identity qualification tests (level L6, run by the owner).

Each test needs a deployed provider or an installed host, so it runs only when its
``ARBORA_QUAL_*`` variables name one. Credentials arrive through the environment of the
owner's run and never appear in a report: a test asserts digests and statuses only.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.parse
from typing import Any

import pytest


def require(*names: str) -> dict[str, str]:
    """The values of the named variables, or skip naming the first one that is unset."""
    values: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name, "").strip()
        if not value:
            pytest.skip(f"requires the live qualification environment ({name})")
        values[name] = value
    return values


def request(
    base_url: str,
    method: str,
    path: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = 15.0,
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP request; ``(status, headers, body)``, or status 0 when unreachable."""
    parts = urllib.parse.urlsplit(base_url)
    connect = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    payload = None if body is None else json.dumps(body)
    sent = {"Content-Type": "application/json", **(headers or {})}
    try:
        connection = connect(parts.hostname, parts.port, timeout=timeout)
        try:
            connection.request(method, path, payload, sent)
            reply = connection.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read(1 << 20)
        finally:
            connection.close()
    except (http.client.HTTPException, OSError):
        return 0, {}, b""
