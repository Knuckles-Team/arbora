"""The served reads behind the fleet acceptance checks (ARBORA-RELEASE-R003.9).

One adapter over the operation context. Every fact comes from the fleet
gateway, the engine ``ConnectorPack`` status read, the governed ``fleet.call``
pipeline or the onboarding digest comparison. A read that fails answers
``None``, which the checks report as ``GAP``; nothing is inferred or cached.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

PackStatus = Callable[[Any, Mapping[str, Any], Any], Awaitable[Mapping[str, Any]]]


class ContextAcceptancePorts:
    """``AcceptancePorts`` read through one operation context."""

    def __init__(
        self,
        context: Any,
        *,
        pack_status: PackStatus,
        pack_op: Any,
        probes: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._context = context
        self._pack_status = pack_status
        self._pack_op = pack_op
        self._probes = probes

    async def configured(self) -> tuple[str, ...]:
        """The connectors the deployment's MCP configuration runs as fleet children."""
        from arbora.fleet.onboarding import configured_fleet_endpoints

        return tuple(item.name for item in configured_fleet_endpoints())

    async def child(self, connector: str) -> Mapping[str, Any] | None:
        gateway = self._context.services.get("fleet_gateway")
        if gateway is None:
            return None
        snapshot = await gateway.status(self._context.caller, servers=(connector,))
        children = snapshot.get("children") if isinstance(snapshot, Mapping) else None
        row = children.get(connector) if isinstance(children, Mapping) else None
        return row if isinstance(row, Mapping) else None

    async def pack(self, connector: str) -> Mapping[str, Any] | None:
        from arbora.api.errors import EngineRefusal

        try:
            reply = await self._pack_status(
                self._context, {"connector": connector}, self._pack_op
            )
        except EngineRefusal:
            return None
        value = reply.get("value") if isinstance(reply, Mapping) else None
        head = value.get("head") if isinstance(value, Mapping) else None
        members = value.get("members") if isinstance(value, Mapping) else None
        if not isinstance(head, Mapping) or not isinstance(members, Mapping):
            return None
        return {
            "tool_count": members.get("published"),
            "pack_digest": head.get("pack_digest"),
        }

    async def read_call(self, connector: str) -> bool | None:
        from arbora.api.invoke.steps import OpError

        probe = self._probes.get(connector)
        invoke = self._context.services.get("plan_step_invoke")
        if not probe or invoke is None:
            return None
        arguments = dict(probe.get("arguments") or {})
        key = (
            "acceptance:"
            + hashlib.sha256(
                json.dumps(
                    [connector, probe.get("tool"), arguments], sort_keys=True
                ).encode()
            ).hexdigest()
        )
        outcome = await invoke(
            "fleet.call",
            {"server": connector, "tool": probe.get("tool"), "arguments": arguments},
            self._context.caller,
            key,
        )
        return not (
            isinstance(outcome, OpError) or getattr(outcome, "code", "OK") != "OK"
        )

    async def digest_matches(self, connector: str) -> bool | None:
        from arbora.fleet.onboarding import (
            capture_server_pack,
            configured_fleet_endpoints,
        )

        onboarding = self._context.services.get("fleet_onboarding")
        endpoint = next(
            (e for e in configured_fleet_endpoints() if e.name == connector), None
        )
        if onboarding is None or endpoint is None:
            return None
        pack = await capture_server_pack(endpoint)
        return bool(await onboarding.pack_unchanged(endpoint, pack))
