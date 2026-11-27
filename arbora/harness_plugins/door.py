"""One served read over the door's HTTP surface, with the login credential.

Used by ``arbora plugin bundle`` for the layered configuration and the persona
load set (ARBORA-HARNESS-PLUGINS-R013, R014). A missing credential is not an
error here: the caller decides what an anonymous bundle may read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from arbora.core.harness_profiles import HarnessProfile
from arbora.harness_plugins.login import CredentialState, TokenStore, renew

TIMEOUT_S = 15.0
Post = Callable[[str, Mapping[str, Any], Mapping[str, str]], httpx.Response]


class DoorRefused(Exception):
    """The door answered with a refusal; ``code`` is its error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def mentions(self, engine_code: str) -> bool:
        """An engine code passes through in the error code or its detail."""
        return engine_code in (self.code, self.detail)


def _post(
    url: str, body: Mapping[str, Any], headers: Mapping[str, str]
) -> httpx.Response:
    return httpx.post(url, json=dict(body), headers=dict(headers), timeout=TIMEOUT_S)


def bearer(profile: HarnessProfile, store: TokenStore) -> str | None:
    """The usable access token for ``profile``, renewed when needed."""
    credential = store.load(profile.id)
    if credential is None:
        return None
    if credential.state is CredentialState.AUTHORIZED:
        credential = renew(credential, store=store)
    if credential.state is not CredentialState.AUTHORIZED:
        return None
    return credential.access_token


def call(
    base_url: str,
    op: str,
    params: Mapping[str, Any],
    token: str,
    *,
    post: Post = _post,
) -> Any:
    """``op`` as the signed-in caller; its result value or :class:`DoorRefused`."""
    response = post(
        f"{base_url.rstrip('/')}/api/v1/ops/{op}",
        dict(params),
        {"Authorization": f"Bearer {token}"},
    )
    try:
        payload = response.json()
    except ValueError:
        raise DoorRefused("UNAVAILABLE") from None
    if response.status_code != 200 or not isinstance(payload, Mapping):
        error = payload.get("error") if isinstance(payload, Mapping) else None
        code = error.get("code") if isinstance(error, Mapping) else None
        details = error.get("details") if isinstance(error, Mapping) else None
        reason = (
            details.get("reason") or details.get("code")
            if isinstance(details, Mapping)
            else details
        )
        raise DoorRefused(str(code or "UNAVAILABLE"), str(reason or ""))
    return (payload.get("result") or {}).get("value")


__all__ = ["DoorRefused", "bearer", "call"]
