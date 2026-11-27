"""Durable push notifications for A2A tasks (ARBORA-A2A-R013).

Arbora keeps no queue and no delivery state. A caller registers a webhook for a
task (``register_push_target``); the target is stored in the task WorkItem's
metadata. When the task reaches a terminal state the authority calls
``enqueue``, which submits one engine WorkItem of kind ``a2a_push`` per target.
``PushDeliveryService`` claims those items, posts the signed A2A task JSON and
commits the outcome. A failed delivery is deferred with exponential backoff on
the injected clock; after the last attempt the item is committed failed with
``PUSH_DEAD_LETTER``. No delivery goes to a private or loopback address unless
its host is allowlisted in ``A2A_PUSH_ALLOWED_PRIVATE_HOSTS``.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from arbora import work_items
from arbora.api.domain.a2a.models import A2APushTarget
from arbora.messaging.webhook_delivery import (
    canonical_body,
    clamp_setting,
    sign_payload,
)
from arbora.security.session import use_session

__all__ = [
    "PUSH_KIND",
    "PushDeliveryService",
    "PushDeliveryWorker",
    "PushRefused",
    "PushSettings",
    "TARGETS_KEY",
    "validate_push_url",
]

logger = logging.getLogger(__name__)

PUSH_KIND = "a2a_push"
TARGETS_KEY = "a2a_push_targets"
DEAD_LETTER = "PUSH_DEAD_LETTER"
MAX_TARGETS = 8
_LEASE_S = 60.0
_PAGE = 100
_CAS_TRIES = 3
DEFAULT_POLL_INTERVAL_S = 5.0

#: ``(default, (low, high))`` per configuration key.
MAX_ATTEMPTS = (5, (1, 20))
BACKOFF_BASE_S = (5, (1, 300))
BACKOFF_CAP_S = (600, (5, 3600))
TIMEOUT_S = (10, (1, 60))

Resolver = Callable[[str], Iterable[str]]
HttpPost = Callable[[str, str, dict[str, Any], dict[str, str]], int]


class PushRefused(ValueError):
    """A push target refusal with a closed code (JSON-RPC ``-32602``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _resolve(host: str) -> Iterable[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_private(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    return not (mapped or ip).is_global


def validate_push_url(
    url: str,
    *,
    allowed_private_hosts: Iterable[str] = (),
    resolve: Resolver = _resolve,
) -> str:
    """The host of an acceptable push URL, or ``PushRefused``.

    ``PUSH_URL_INVALID``: not https, longer than 2048 characters, carrying
    credentials, or unresolvable. ``PUSH_TARGET_PRIVATE``: any resolved address
    is private, loopback or link-local, and the host is not allowlisted.
    """
    if len(url) > 2048:
        raise PushRefused("PUSH_URL_INVALID")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise PushRefused("PUSH_URL_INVALID") from None
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise PushRefused("PUSH_URL_INVALID")
    allowed = {entry.strip().lower() for entry in allowed_private_hosts}
    try:
        addresses = [host] if _is_ip(host) else list(resolve(host))
    except OSError:
        raise PushRefused("PUSH_URL_INVALID") from None
    if not addresses:
        raise PushRefused("PUSH_URL_INVALID")
    if host not in allowed and any(_is_private(address) for address in addresses):
        raise PushRefused("PUSH_TARGET_PRIVATE")
    return host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class PushSettings:
    """The retry limits and the private-host allowlist, from configuration."""

    max_attempts: int = 5
    backoff_base_s: int = 5
    backoff_cap_s: int = 600
    timeout_s: int = 10
    allowed_private_hosts: tuple[str, ...] = ()

    @classmethod
    def from_config(cls) -> PushSettings:
        from arbora.core.environment import read_env

        def number(name: str, spec: tuple[Any, tuple[Any, Any]]) -> int:
            return int(clamp_setting(read_env(name), spec))

        raw = str(read_env("A2A_PUSH_ALLOWED_PRIVATE_HOSTS") or "").strip()
        if raw.startswith("["):
            try:
                hosts = [str(entry) for entry in json.loads(raw)]
            except ValueError:
                hosts = []
        else:
            hosts = [entry for entry in raw.split(",")]
        return cls(
            max_attempts=number("A2A_PUSH_MAX_ATTEMPTS", MAX_ATTEMPTS),
            backoff_base_s=number("A2A_PUSH_BACKOFF_BASE_S", BACKOFF_BASE_S),
            backoff_cap_s=number("A2A_PUSH_BACKOFF_CAP_S", BACKOFF_CAP_S),
            timeout_s=number("A2A_PUSH_TIMEOUT_S", TIMEOUT_S),
            allowed_private_hosts=tuple(
                entry.strip().lower() for entry in hosts if entry.strip()
            ),
        )


def _secret(reference: str) -> str:
    from arbora.messaging.transport import resolve_secret

    return resolve_secret(reference)


def _http_post(timeout_s: float) -> HttpPost:
    import httpx
    from scion_sdk.tls.resolve import resolve_tls_profile

    def post(
        method: str, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> int:
        trust = resolve_tls_profile("a2a-push")
        try:
            options = {
                **trust.httpx_kwargs(),
                "follow_redirects": False,
                "timeout": timeout_s,
            }
            with httpx.Client(**options) as client:
                return client.request(
                    method, url, content=canonical_body(body), headers=headers
                ).status_code
        finally:
            trust.cleanup()

    return post


class PushDeliveryWorker:
    """Registers push targets, enqueues terminal pushes and delivers them.

    It implements ``A2APushNotificationAuthority``: the card advertises push
    only while one is wired.
    """

    def __init__(
        self,
        authority: Any,
        *,
        settings: PushSettings | None = None,
        clock: Callable[[], float] = time.time,
        post: HttpPost | None = None,
        resolve: Resolver = _resolve,
        resolve_token: Callable[[str], str] = _secret,
        worker_ref: str = "a2a-push",
    ) -> None:
        self._authority = authority
        self.settings = settings or PushSettings.from_config()
        self._clock = clock
        self._post = post or _http_post(float(self.settings.timeout_s))
        self._resolve = resolve
        self._resolve_token = resolve_token
        self._worker_ref = worker_ref

    # -- registration (R013.1) ------------------------------------------------

    def register_push_target(
        self, task_id: str, target_url: str, token_ref: str | None = None
    ) -> A2APushTarget:
        """Validate and store one target on the caller's own task.

        A task the caller does not own answers like an absent task
        (``LookupError``). A terminal task is pushed to the new target at once.
        """
        try:
            target = A2APushTarget(task_id=task_id, url=target_url, token_ref=token_ref)
        except ValueError:
            raise PushRefused("PUSH_URL_INVALID") from None
        validate_push_url(
            target.url,
            allowed_private_hosts=self.settings.allowed_private_hosts,
            resolve=self._resolve,
        )
        engine = self._authority.work_engine()
        entry = {"url": target.url, "token_ref": target.token_ref}
        for _ in range(_CAS_TRIES):
            item = self._authority._authorized_item(task_id, "kg:write")
            if item is None:
                raise LookupError("task not found")
            metadata = dict(item.get("metadata") or {})
            targets = [
                t for t in metadata.get(TARGETS_KEY, ()) if t.get("url") != target.url
            ]
            if len(targets) >= MAX_TARGETS:
                raise PushRefused("PUSH_URL_INVALID")
            metadata[TARGETS_KEY] = [*targets, entry]
            if work_items.cas_work_item_metadata(engine, item, metadata=metadata):
                if item.get("status") in work_items.TERMINAL_WORK_ITEM_STATUSES:
                    self.enqueue(task_id, item, targets=[entry])
                return target
        raise PushRefused("PUSH_URL_INVALID")

    # -- enqueue (R013.2) -----------------------------------------------------

    def enqueue(
        self,
        task_id: str,
        item: Mapping[str, Any],
        *,
        targets: Iterable[Mapping[str, Any]] | None = None,
    ) -> int:
        """Submit one ``a2a_push`` WorkItem per target of a terminal task."""
        metadata = item.get("metadata") or {}
        wanted = list(targets if targets is not None else metadata.get(TARGETS_KEY, ()))
        if not wanted:
            return 0
        task = self._authority.project_held(dict(item), task_id=task_id)
        document = task.model_dump(mode="json", by_alias=True)
        state = str(document.get("status", {}).get("state", ""))
        engine = self._authority.work_engine()
        for target in wanted:
            key = hashlib.sha256(
                json.dumps(
                    [item.get("work_item_id"), state, target["url"]],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            work_items.submit_work_item_atomic(
                engine,
                kind=PUSH_KIND,
                idempotency_key=f"a2a-push:{key}",
                max_attempts=self.settings.max_attempts,
                description="a2a push notification",
                metadata={
                    "task_id": task_id,
                    "state": state,
                    "url": target["url"],
                    "token_ref": target.get("token_ref"),
                    "task": document,
                },
            )
        return len(wanted)

    # -- delivery (R013.2) ----------------------------------------------------

    def _backoff_s(self, failures: int) -> float:
        base, cap = self.settings.backoff_base_s, self.settings.backoff_cap_s
        return float(min(base * 2**failures, cap))

    async def _attempt(self, item: Mapping[str, Any], claim: Mapping[str, Any]) -> str:
        """One delivery attempt of a claimed item; returns its outcome."""
        engine = self._authority.work_engine()
        item_id = str(item["work_item_id"])
        metadata = item.get("metadata") or {}
        url = str(metadata.get("url", ""))
        try:
            host = validate_push_url(
                url,
                allowed_private_hosts=self.settings.allowed_private_hosts,
                resolve=self._resolve,
            )
            ok = await asyncio.to_thread(self._send, item_id, metadata, url)
        except Exception:
            logging.getLogger(__name__).debug(
                "a failed delivery is retried, never raised", exc_info=True
            )
            host, ok = urlsplit(url).hostname or "", False
        if ok:
            return await asyncio.to_thread(
                work_items.commit_result, engine, item_id, claim, outcome="succeeded"
            )
        attempt = int(claim.get("attempt") or 1)
        if attempt >= self.settings.max_attempts:
            logger.warning("a2a push dead letter for host %s", host)
            return await asyncio.to_thread(
                work_items.commit_result,
                engine,
                item_id,
                claim,
                outcome="failed",
                error_ref=DEAD_LETTER,
            )
        await asyncio.to_thread(
            work_items.defer_work_item,
            engine,
            item_id,
            claim,
            next_retry_at=self._clock() + self._backoff_s(attempt - 1),
            reason_ref="PUSH_DELIVERY_FAILED",
        )
        return "deferred"

    def _send(self, item_id: str, metadata: Mapping[str, Any], url: str) -> bool:
        token_ref = metadata.get("token_ref")
        token = self._resolve_token(str(token_ref)) if token_ref else ""
        if token_ref and not token:
            return False
        stamp = str(int(self._clock()))
        document = dict(metadata.get("task") or {})
        headers = {
            "Content-Type": "application/json",
            "X-Arbora-Delivery": item_id,
            "X-Arbora-Timestamp": stamp,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Arbora-Signature"] = sign_payload(
                token, stamp, canonical_body(document)
            )
        return 200 <= self._post("POST", url, document, headers) < 300

    async def run_once(self, sessions: Iterable[Any]) -> list[tuple[str, str]]:
        """Claim and attempt every due push item of each tenant session."""
        outcomes: list[tuple[str, str]] = []
        engine = self._authority.work_engine()
        for session in sessions:
            with use_session(session):
                cursor: str | None = None
                while True:
                    page = await asyncio.to_thread(
                        work_items.list_work_items,
                        engine,
                        kind=PUSH_KIND,
                        cursor=cursor,
                        limit=_PAGE,
                    )
                    for item in page.get("items", ()):
                        if item.get("status") in work_items.TERMINAL_WORK_ITEM_STATUSES:
                            continue
                        claim = await asyncio.to_thread(
                            work_items.claim_specific,
                            engine,
                            str(item["work_item_id"]),
                            token=self._worker_ref,
                            lease_ttl_s=_LEASE_S,
                            now=self._clock(),
                        )
                        if claim is None:
                            continue
                        outcome = await self._attempt(item, claim)
                        outcomes.append((str(item["work_item_id"]), outcome))
                    cursor = page.get("next_cursor")
                    if not cursor:
                        break
        return outcomes


class PushDeliveryService:
    """The host background service: poll the engine's push items until stopped."""

    def __init__(
        self,
        worker: PushDeliveryWorker,
        process_session: Callable[[], Any],
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        emit: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.worker = worker
        self._session = process_session
        self._sleep = sleep
        self._poll = poll_interval_s
        self._emit = emit or (lambda _event, _fields: None)

    async def run(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            try:
                await self.worker.run_once((self._session(),))
            except Exception:
                logging.getLogger(__name__).debug(
                    "the next poll retries", exc_info=True
                )
                self._emit("a2a_push_poll_failed", {})
            await self._sleep(self._poll)
