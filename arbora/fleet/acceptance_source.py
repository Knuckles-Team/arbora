"""The fleet side of connector acceptance, behind the ``FleetAcceptance`` port (ARBORA-RELEASE-R003.9)."""

from __future__ import annotations

from typing import Any

from arbora.fleet import onboarding

__all__ = ["OnboardingAcceptance"]


class OnboardingAcceptance:
    """What the deployment configures as fleet children and whether a pack is unchanged."""

    def configured(self) -> tuple[str, ...]:
        """The connectors the deployment's MCP configuration runs as fleet children."""
        return tuple(item.name for item in onboarding.configured_fleet_endpoints())

    async def digest_matches(self, context: Any, connector: str) -> bool | None:
        """Whether the connector's captured pack equals the onboarded one; ``None`` unread."""
        recorded = context.services.get("fleet_onboarding")
        endpoint = next(
            (e for e in onboarding.configured_fleet_endpoints() if e.name == connector),
            None,
        )
        if recorded is None or endpoint is None:
            return None
        pack = await onboarding.capture_server_pack(endpoint)
        return bool(await recorded.pack_unchanged(endpoint, pack))
