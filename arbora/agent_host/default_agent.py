"""The default fleet agent's definition in the engine ``AgentLibrary`` (ARBORA-HOST-R008.1001).

A run that names no agent runs on the default fleet agent. Its skills are gated like any
agent's, so its definition must be a graph record: without a library entry the run gate
refuses ``attestation_source_unavailable``. The host composes one
:class:`DefaultAgentRegistrar`; it registers the definition for the verified caller's
tenant through the engine, once, and a repeat with the same skills writes nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused

#: The scope and purpose the default agent's library entry is published under.
ACTOR_SCOPE = "arbora:fleet"
PURPOSE_ID = "fleet.default"
ROLE = "fleet-agent"


def _digest(*parts: Any) -> str:
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dependency(component_id: str, kind: str, *content: Any) -> dict[str, str]:
    return {
        "component_id": component_id,
        "definition_digest": _digest(component_id, kind, *content),
        "kind": kind,
    }


@dataclass(frozen=True, slots=True)
class DefaultAgentDefinition:
    """What the host stands behind for the default agent: its name, model and skill refs."""

    agent_id: str
    model_identity: str = "fleet-default"
    skills: tuple[Mapping[str, str], ...] = ()
    version: str = "1"

    def item(self) -> dict[str, Any]:
        """The library entry draft body, minus the tenant the engine write stamps."""
        content = (self.agent_id, self.model_identity, self.version)
        return {
            "actor_scope": ACTOR_SCOPE,
            "agent_id": self.agent_id,
            "model_identity": self.model_identity,
            "model_profile": _dependency(
                f"model-profile:{self.model_identity}", "model_profile", *content
            ),
            "ontologies": [],
            "package_id": f"arbora:{self.agent_id}",
            "policy_digest": _digest("policy", *content),
            "purpose_id": PURPOSE_ID,
            "role": ROLE,
            "role_digest": _digest("role", ROLE),
            "skills": [dict(s) for s in self.skills],
            "source_revision": self.version,
            "source_revision_digest": _digest("source", *content),
            "system_prompt": _dependency(
                f"prompt:{self.agent_id}", "system_prompt", *content
            ),
            "tools": [],
            "version": self.version,
        }


class DefaultAgentRegistrar:
    """Registers the default agent's definition for each tenant that runs on it."""

    def __init__(self, definition: DefaultAgentDefinition) -> None:
        self._definition = definition

    async def ensure(self, context: Any) -> str:
        """``created`` or ``unchanged``; an existing entry is the operator's and is never rewritten."""
        from arbora.api.ops.agent_library import current_entry, publish_entry

        try:
            await current_entry(context, self._definition.agent_id)
        except OperationRefused as refused:
            if refused.code != "NOT_FOUND":
                raise
            await publish_entry(context, self._definition.item())
            return "created"
        return "unchanged"


async def run_agent_name(context: Any, message: Mapping[str, Any]) -> str:
    """The agent a run is gated on: the named one, else the default fleet agent.

    The default agent is registered first when the host composed a registrar, so a host
    without one leaves it unregistered and the gate refuses ``attestation_source_unavailable``.
    """
    from arbora.host_bindings import FLEET_AGENT_NAME

    named = str((message.get("metadata") or {}).get("agent_name") or "").strip()
    registrar = (getattr(context, "services", None) or {}).get("default_agent")
    if not named and registrar is not None:
        await registrar.ensure(context)
    return named or FLEET_AGENT_NAME


__all__ = ["DefaultAgentDefinition", "DefaultAgentRegistrar", "run_agent_name"]
