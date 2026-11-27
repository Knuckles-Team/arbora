"""Code-host events reach the engine as delivery feedback (ARBORA-OPS-R896.8, R896.9).

``POST /api/v1/development/scm-events/{connector}`` is admitted by the gateway identity
middleware's signed-ingress method, then served through the one invoke pipeline as
``development.scm.event``. The handler passes the verified delivery to the connector's
normalizer through the fleet, maps each ``ScmFeedbackEvent`` of the scion-sdk contract to the
engine's, stamps ``admitted_principal`` on a person's event only when the linked arbora
principal holds ``source:write``, and sends it to ``DeliveryFeedback``. A polling fallback
(``development.scm.poll``, ticked by the host's background service) reads each followed
proposal's events through the connector's list tool with a stored cursor when no webhook
arrived within ``ARBORA_DEVELOPMENT_SCM_POLL_INTERVAL_S``. Arbora stores only the cursor.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_write_op
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import OpSpec, Surface
from arbora.core.environment import read_env
from arbora.security.signed_ingress import SIGNED_CREDENTIAL_KIND

WRITE_SCOPE = "lifecycle:write"
SOURCE_WRITE = "source:write"
CURSOR_NAMESPACE = "development.scm.cursor"
DEFAULT_POLL_INTERVAL_S = 120
_PERSON = "person"
_FORWARDED_HEADERS = ("content-type", "x-github-event", "x-gitlab-event", "x-event-key")


class IdentityLinks(Protocol):
    """The access registry's identity links: a code-host actor to an arbora principal."""

    async def principal_for(self, provider: str, actor: str) -> str | None: ...


class SourceAccess(Protocol):
    """Whether a principal holds a scope on a repository, answered by the engine."""

    async def holds(self, principal: str, scope: str, repository: str) -> bool: ...


class ScmEventParams(Params):
    connector: str = Field(min_length=1, max_length=128)
    delivery_id: str = Field(min_length=1, max_length=256)
    headers: dict[str, str]
    body_b64: str = Field(max_length=2_000_000)


class ScmEventResult(Params):
    delivered: int
    duplicates: int


class ScmPollParams(Params):
    pass


class ScmPollResult(Params):
    polled: int
    delivered: int


def millis(value: Any) -> int:
    """Epoch milliseconds of an ISO instant (the SDK event's ``at``)."""
    if isinstance(value, (int, float)):
        return int(value)
    return int(
        datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000
    )


def proposal_id(proposal: Mapping[str, Any]) -> str:
    """The engine's proposal id of a change-proposal reference."""
    return f"{proposal['provider']}:{proposal['repository']}#{proposal['number']}"


def engine_event(event: Mapping[str, Any], connector: str) -> dict[str, Any]:
    """The SDK ``ScmFeedbackEvent`` as the engine's; a malformed event refuses."""
    try:
        kind = str(event["kind"])
        return {
            "event_id": str(event["event_id"]),
            "connector": connector,
            "kind": kind,
            "source": _PERSON if event.get("actor_kind") == _PERSON else "automation",
            "proposal_id": proposal_id(event["proposal"]),
            "failed": bool(event.get("failed", False)),
            "summary": str(event.get("summary") or kind)[:500],
            "observed_at": millis(event["at"]),
        }
    except (KeyError, TypeError, ValueError):
        raise OperationRefused("FEEDBACK_EVENT_INVALID", {}) from None


async def admit_actor(
    event: Mapping[str, Any],
    repository: str,
    *,
    links: IdentityLinks | None,
    access: SourceAccess | None,
) -> str | None:
    """The arbora principal to stamp on a person's event, else ``None`` (context only).

    A bot, an automated reviewer or a check event never carries a principal, and neither does a
    person whose code-host actor has no identity link or whose principal lacks ``source:write``.
    Quoted text in an event is never read here, so it cannot change a scope or a policy.
    """
    if event.get("actor_kind") != _PERSON or links is None or access is None:
        return None
    principal = await links.principal_for(
        str(event["proposal"]["provider"]), str(event["actor"])
    )
    if principal and await access.holds(principal, SOURCE_WRITE, repository):
        return principal
    return None


async def _call_connector(
    context: Any, server: str, tool: str, arguments: dict[str, Any]
) -> Any:
    from arbora.api.ops.research import FleetCaller

    return await FleetCaller(context).call(server, tool, arguments)


def _source(context: Any, connector: str) -> Any:
    from arbora.identity.signed_ingress import configured_sources

    source = configured_sources().get(connector)
    if source is None:
        raise OperationRefused("SCM_SIGNATURE_INVALID", {})
    return source


def _tool_result(answer: Any) -> Mapping[str, Any]:
    value = answer.get("value", answer) if isinstance(answer, Mapping) else None
    if not isinstance(value, Mapping):
        raise unavailable("connector result malformed")
    return value


async def _deliver(
    context: Any, connector: str, event: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Stamp and send one normalized event; the engine dedups by ``event_id``."""
    from arbora.api.ops.delivery_ports import delivery_port

    wire = engine_event(event, connector)
    stamp = await admit_actor(
        event,
        str(event["proposal"]["repository"]),
        links=delivery_port(context, "identity_links"),
        access=delivery_port(context, "source_access"),
    )
    if stamp is not None:
        wire["admitted_principal"] = stamp
    try:
        receipt = await engine_send(
            context, "DeliveryFeedback", {"event": wire}, idempotent=True
        )
    except EngineRefusal as refused:
        if refused.code == "FEEDBACK_EVENT_INVALID":
            raise OperationRefused("FEEDBACK_EVENT_INVALID", {}) from None
        raise
    proposal = event["proposal"]
    await _remember(
        context,
        connector,
        f"{proposal['repository']}#{proposal['number']}",
        None,
        context_now_ms(context),
    )
    return receipt if isinstance(receipt, Mapping) else {}


def _system_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def context_now_ms(context: Any) -> int:
    """The composed clock (a test injects its own); the host defaults to the system clock."""
    return int((context.services.get("clock_ms") or _system_clock_ms)())


async def _remember(
    context: Any, connector: str, proposal: str, cursor: str | None, now_ms: int
) -> None:
    """Store the cursor and the time of the last event: the only state arbora holds.

    ``proposal`` is ``<repository>#<number>``.
    """
    key = f"{connector}/{proposal}"
    current = await _cursor(context, key)
    value = {
        "cursor": cursor if cursor is not None else current.get("cursor"),
        "at_ms": now_ms,
    }
    await engine_send(
        context,
        "KvPut",
        {
            "namespace": CURSOR_NAMESPACE,
            "key": key,
            "value": json.dumps(value).encode(),
        },
    )


async def _cursor(context: Any, key: str) -> dict[str, Any]:
    entry = await engine_send(
        context, "KvGet", {"namespace": CURSOR_NAMESPACE, "key": key}
    )
    raw = entry.get("value") if isinstance(entry, Mapping) else None
    if isinstance(raw, (bytes, bytearray)):
        raw = json.loads(bytes(raw))
    return dict(raw) if isinstance(raw, Mapping) else {}


async def handle_scm_event(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Normalize one admitted delivery and forward its event; refuses unless admitted by signature."""
    caller = context.caller
    if getattr(caller, "credential_kind", "") != SIGNED_CREDENTIAL_KIND:
        raise OperationRefused("SCM_SIGNATURE_INVALID", {})
    source = _source(context, params["connector"])
    if caller.principal != source.principal or caller.tenant != source.tenant:
        raise OperationRefused("SCM_SIGNATURE_INVALID", {})
    answer = _tool_result(
        await _call_connector(
            context,
            source.server,
            source.normalize_tool,
            {"headers": params["headers"], "body": params["body_b64"]},
        )
    )
    if answer.get("refused"):
        code = str(answer.get("code"))
        raise OperationRefused(
            code
            if code in ("SCM_EVENT_UNSUPPORTED", "SCM_SIGNATURE_INVALID")
            else "SCM_SIGNATURE_INVALID",
            {},
        )
    receipt = await _deliver(context, source.id, answer)
    duplicate = bool(receipt.get("duplicate"))
    return {"delivered": 0 if duplicate else 1, "duplicates": 1 if duplicate else 0}


def _proposal_ref(proposal: Any) -> tuple[str, str, str] | None:
    """``(connector, repository, number)`` of one open proposal, else ``None``."""
    if not isinstance(proposal, Mapping) or proposal.get("state", "open") != "open":
        return None
    provider, _, rest = str(proposal.get("id", "")).partition(":")
    repository, _, number = rest.rpartition("#")
    if not (provider and repository and number):
        return None
    return str(proposal.get("connector") or provider), repository, number


def _open_proposals(row: Any) -> list[tuple[str, str, str]]:
    """The open proposals one live delivery follows."""
    if not isinstance(row, Mapping) or row.get("ended"):
        return []
    refs = (_proposal_ref(proposal) for proposal in row.get("proposals") or ())
    return [ref for ref in refs if ref is not None]


async def followed_proposals(context: Any) -> list[tuple[str, str, str]]:
    """Every open proposal a live delivery follows."""
    rows = await engine_send(context, "DeliveryList", {"request": {}})
    return [
        found
        for row in rows
        if isinstance(rows, list)
        for found in _open_proposals(row)
    ]


async def handle_scm_poll(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read each quiet followed proposal's events through the connector list tool."""
    from arbora.pin_lag.scion_sdk import require_scm_event_listing

    require_scm_event_listing()
    interval_ms = 1000 * int(
        str(read_env("ARBORA_DEVELOPMENT_SCM_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S))
    )
    now = context_now_ms(context)
    polled = delivered = 0
    for connector, repository, number in await followed_proposals(context):
        key = f"{connector}/{repository}#{number}"
        state = await _cursor(context, key)
        if now - int(state.get("at_ms", 0)) < interval_ms:
            continue
        source = _source(context, connector)
        page = _tool_result(
            await _call_connector(
                context,
                source.server,
                source.list_tool,
                {
                    "repository": repository,
                    "number": int(number),
                    "cursor": state.get("cursor"),
                    "limit": 100,
                },
            )
        )
        polled += 1
        for event in page.get("events") or ():
            delivered += (
                0 if (await _deliver(context, source.id, event)).get("duplicate") else 1
            )
        await _remember(
            context, connector, f"{repository}#{number}", page.get("next_cursor"), now
        )
    return {"polled": polled, "delivered": delivered}


NATIVE_IDEMPOTENCY: dict[str, str] = {
    # The engine dedups a feedback event by its provider event id.
    "development.scm.event": "medulla:DeliveryFeedback",
    "development.scm.poll": "medulla:DeliveryFeedback",
}


def body_params(
    connector: str, delivery_id: str, headers: Mapping[str, str], body: bytes
) -> dict[str, Any]:
    """The operation params of one admitted delivery: the raw body travels base64-encoded."""
    kept = {
        k: v
        for k, v in headers.items()
        if k.lower() in _FORWARDED_HEADERS
        or k.lower().startswith(("x-hub-", "x-github-", "x-gitlab-"))
    }
    return {
        "connector": connector,
        "delivery_id": delivery_id,
        "headers": kept,
        "body_b64": base64.b64encode(body).decode("ascii"),
    }


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.development_scm"
    return (
        build_write_op(
            op_id="development.scm.event",
            summary="Forward one signed code-host delivery to the delivery feedback of the engine",
            examples=("a code host sent a signed event",),
            params=ScmEventParams,
            result=ScmEventResult,
            handler=f"{module}.handle_scm_event",
            scope=WRITE_SCOPE,
            surfaces=frozenset({Surface.HTTP}),
        ),
        build_write_op(
            op_id="development.scm.poll",
            summary="Read the events of followed proposals that no webhook reported",
            examples=("poll the code host for quiet proposals",),
            params=ScmPollParams,
            result=ScmPollResult,
            handler=f"{module}.handle_scm_poll",
            scope=WRITE_SCOPE,
            surfaces=frozenset({Surface.MCP}),
        ),
    )


specs = operations


class ScmPollService:
    """Tick ``development.scm.poll`` as the tenant's service caller on an injected clock."""

    def __init__(
        self,
        callers: Any,
        clock: Any,
        tick_s: float,
        *,
        sink: Callable[[Exception], None] | None = None,
    ) -> None:
        self._callers, self._clock, self._tick_s = callers, clock, tick_s
        self._sink = sink

    async def tick(self) -> None:
        for tenant in await self._callers.tenants():
            try:
                await self._callers.invoke(
                    tenant,
                    "development.scm.poll",
                    {},
                    f"scm-poll:{tenant}:{int(self._clock.now() // self._tick_s)}",
                )
            except Exception as exc:  # noqa: BLE001 - one tenant never stops the rest
                if self._sink is not None:
                    self._sink(exc)

    async def run(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            await self.tick()
            await self._clock.sleep(self._tick_s)


__all__ = [
    "NATIVE_IDEMPOTENCY",
    "ScmPollService",
    "admit_actor",
    "body_params",
    "engine_event",
    "handle_scm_event",
    "handle_scm_poll",
    "operations",
    "specs",
]
