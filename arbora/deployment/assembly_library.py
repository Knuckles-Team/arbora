"""Publish the deployment templates and their tool components (ARBORA-DEPLOY-R017.20).

``AgentAssemble`` resolves ``template_key = deployment:<target>`` to the newest
published agent graph of that id in the tenant's Agent Library. Arbora is the
producer of those graphs. For every placement target it publishes:

* the shared base of one deployment-step agent: a model profile, a system
  prompt and a placeholder tool component (the base must be a valid agent on
  its own);
* one ``AgentTemplate`` (``deployment-step``) whose one parameter replaces the
  placeholder tool, so every slot is the same role with a different tool;
* one tool component per slot and target, bound to the fleet tool named in
  ``~/.config/arbora/deploy.yaml`` under ``tools.<role>.<target>``;
* one ``AgentGraph`` ``deployment:<target>`` whose nodes ``render-artifact``,
  ``apply`` and ``verify`` instantiate the template with that target's tools.

The drafts are the engine's own wire types, validated before they are sent.
Nothing is invented: a slot with no configured tool is logged
``DEPLOY_TOOL_UNCONFIGURED`` and leaves its target without a graph, so the
engine refuses to assemble that target rather than guess a tool.

Publication is process lifecycle. Each write is idempotent: a current entry
with the same body is reused, and a changed body is published against the
revision that was read.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The placement targets the engine knows (``placement:<target>`` option ids).
TARGETS = ("in_process", "compose", "helm", "swarm")
#: The step slots of every deployment graph, in engine order.
ROLES = ("render-artifact", "apply", "verify")
TEMPLATE_PREFIX = "deployment:"
TEMPLATE_ID = "deployment-step"
PURPOSE_ID = "agent_library.deployment_publish"
VERSION = "1.0.0"

#: The fleet scope a deployment tool call needs, declared in its tool facts.
_TOOL_SCOPE = "fleet:control"
_BASE_PROMPT = "deployment:step-prompt"
_BASE_MODEL = "deployment:step-model"
_BASE_TOOL = "deployment:step-tool"
_BASE_SKILL = "deployment:step-skill"
_BASE_ONTOLOGY = "deployment:step-ontology"
_UNESTIMATED = 0

#: ``(method, params, idempotency_key)`` through the generated senders.
Send = Callable[[str, dict[str, Any], str | None], Awaitable[Any]]
#: One mutation context for one write: ``(tenant, idempotency_key)``.
ContextFor = Callable[[str, str], Awaitable[Mapping[str, Any]]]

_EFFECT = {"render-artifact": "read", "apply": "write", "verify": "read"}


@dataclass(frozen=True)
class ToolBinding:
    """The fleet tool one slot runs on one target."""

    server: str
    tool: str


def tool_bindings(document: Any) -> dict[tuple[str, str], ToolBinding]:
    """``(role, target) -> tool`` from the parsed ``deploy.yaml``.

    A malformed entry is skipped like a missing one; the caller logs it.
    """
    tools = document.get("tools") if isinstance(document, Mapping) else None
    bound: dict[tuple[str, str], ToolBinding] = {}
    if not isinstance(tools, Mapping):
        return bound
    for role in ROLES:
        row = tools.get(role)
        if not isinstance(row, Mapping):
            continue
        for target in TARGETS:
            entry = row.get(target)
            if not isinstance(entry, Mapping):
                continue
            server, tool = entry.get("server"), entry.get("tool")
            if isinstance(server, str) and server and isinstance(tool, str) and tool:
                bound[(role, target)] = ToolBinding(server, tool)
    return bound


def load_tool_bindings(path: Path | None = None) -> dict[tuple[str, str], ToolBinding]:
    """Read ``deploy.yaml`` from the Arbora config directory; absent means none."""
    import yaml

    from arbora.core.settings import config_dir

    source = path or Path(config_dir()) / "deploy.yaml"
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return tool_bindings(yaml.safe_load(text))


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def tool_component_id(role: str, target: str) -> str:
    return f"deployment:tool:{role}:{target}"


def _component(
    tenant: str,
    context: Mapping[str, Any],
    *,
    component_id: str,
    kind: str,
    facts: Mapping[str, Any],
    summary: str,
    attributes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One ``AgentComponentDraft``, validated against the engine's type."""
    from medulla.generated.agent_component import AgentComponentDraft

    body = {
        "id": component_id,
        "kind": kind,
        "facts": dict(facts),
        "attributes": dict(attributes or {}),
    }
    draft = {
        "component_id": component_id,
        "kind": kind,
        "version": VERSION,
        "content_digest": _digest(body),
        # The engine reads ``attributes`` as a map, never null (a null is
        # refused as an invalid request encoding), so an empty one is ``{}``.
        "attributes": dict(attributes or {}),
        "content_ref": None,
        "facts": dict(facts),
        "provenance": {"origin": "native"},
        "summary": summary,
        "classification": [],
        "requires": [],
        "declared_capabilities": [],
        "required_capabilities": [],
        "declared_required_capabilities": [],
        "tenant_id": tenant,
        "actor_scope": context["actor_scope"],
        "purpose_id": context["purpose_id"],
        "policy_digest": context["policy_digest"],
        "source_revision": f"arbora-{VERSION}",
        "source_revision_digest": _digest(body),
    }
    AgentComponentDraft.model_validate(draft)
    return draft


#: The engine refuses a zero context window; one token is the least it admits.
_NO_WINDOW = 1


def base_components(tenant: str, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The model profile, prompt, skill, ontology and placeholder tool of the step agent.

    A deployment step runs a tool, not a model, so the profile states that: no
    provider, no capability and the smallest window the engine admits (it
    refuses a zero window).
    """
    return [
        _component(
            tenant,
            context,
            component_id=_BASE_MODEL,
            kind="model_profile",
            facts={
                "facts": "model_profile",
                "provider": "none",
                "model_identity": "none",
                "context_window_tokens": _NO_WINDOW,
                "max_output_tokens": _NO_WINDOW,
                "supports_structured_output": False,
                "supports_tools": False,
                "supports_vision": False,
            },
            summary="No model: a deployment step runs a governed fleet tool",
        ),
        _component(
            tenant,
            context,
            component_id=_BASE_PROMPT,
            kind="system_prompt",
            facts={
                "facts": "system_prompt",
                "prompt_mode": "static",
                "token_estimate": _UNESTIMATED,
                "variables": [],
            },
            summary="Placeholder prompt of a deployment step",
        ),
        _component(
            tenant,
            context,
            component_id=_BASE_TOOL,
            kind="tool",
            facts={
                "facts": "tool",
                "effect": "read",
                "required_scopes": [_TOOL_SCOPE],
            },
            summary="Placeholder tool every deployment slot replaces",
        ),
        _component(
            tenant,
            context,
            component_id=_BASE_SKILL,
            kind="skill",
            facts={"facts": "opaque"},
            summary="The engine requires an agent to name a skill; a step runs one tool",
        ),
        _component(
            tenant,
            context,
            component_id=_BASE_ONTOLOGY,
            kind="ontology",
            facts={"facts": "opaque"},
            summary="The engine requires an agent to name an ontology; a step uses none",
        ),
    ]


def tool_components(
    tenant: str,
    context: Mapping[str, Any],
    bindings: Mapping[tuple[str, str], ToolBinding],
) -> list[dict[str, Any]]:
    """One tool component per configured slot and target."""
    out = []
    for (role, target), bound in sorted(bindings.items()):
        out.append(
            _component(
                tenant,
                context,
                component_id=tool_component_id(role, target),
                kind="tool",
                facts={
                    "facts": "tool",
                    "effect": _EFFECT[role],
                    "required_scopes": [_TOOL_SCOPE],
                },
                summary=f"Fleet tool for the {role} step on {target}",
                attributes={"server": bound.server, "tool": bound.tool},
            )
        )
    return out


def template_draft(
    tenant: str, context: Mapping[str, Any], pins: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    """The ``AgentTemplateDraft``: the step agent and its one tool parameter."""
    from medulla.generated.models import AgentTemplateDraft

    draft = {
        "template_id": TEMPLATE_ID,
        "version": VERSION,
        "tenant_id": tenant,
        "actor_scope": context["actor_scope"],
        "purpose_id": context["purpose_id"],
        "policy_digest": context["policy_digest"],
        "base": {
            "agent_id": f"{TEMPLATE_ID}-base",
            "package_id": "arbora",
            "version": VERSION,
            "role": "deployment step",
            "role_digest": _digest("deployment step"),
            "system_prompt": dict(pins[_BASE_PROMPT]),
            "tools": [dict(pins[_BASE_TOOL])],
            "skills": [dict(pins[_BASE_SKILL])],
            "model_profile": dict(pins[_BASE_MODEL]),
            "model_identity": "none",
            "ontologies": [dict(pins[_BASE_ONTOLOGY])],
            "tenant_id": tenant,
            "actor_scope": context["actor_scope"],
            "purpose_id": context["purpose_id"],
            "policy_digest": context["policy_digest"],
            "source_revision": f"arbora-{VERSION}",
            "source_revision_digest": _digest(TEMPLATE_ID),
            "instantiated_from": None,
        },
        "params": [
            {
                "name": "tool",
                "replaces": _BASE_TOOL,
                "kind": "tool",
                "required": True,
                "summary": "The fleet tool this deployment step runs",
            }
        ],
    }
    AgentTemplateDraft.model_validate(draft)
    return draft


def graph_draft(
    tenant: str,
    context: Mapping[str, Any],
    target: str,
    template_digest: str,
    pins: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """The ``AgentGraphDraft`` ``deployment:<target>``: three steps, then end."""
    from medulla.generated.decision import AgentGraphDraft

    nodes: list[dict[str, Any]] = []
    for role in ROLES:
        nodes.append(
            {
                "node_id": role,
                "kind": {
                    "template": {
                        "template_id": TEMPLATE_ID,
                        "definition_digest": template_digest,
                        "bindings": {
                            "tool": dict(pins[tool_component_id(role, target)])
                        },
                    }
                },
                "deps_contract": None,
                "output_contract": None,
            }
        )
    nodes.append(
        {
            "node_id": "done",
            "kind": "end",
            "deps_contract": None,
            "output_contract": None,
        }
    )
    order = [*ROLES, "done"]
    draft = {
        "graph_id": f"{TEMPLATE_PREFIX}{target}",
        "version": VERSION,
        "tenant_id": tenant,
        "actor_scope": context["actor_scope"],
        "purpose_id": context["purpose_id"],
        "policy_digest": context["policy_digest"],
        "synthesis_evidence": None,
        "topology": None,
        "shape": {
            "entry_node": ROLES[0],
            "nodes": nodes,
            "edges": [
                {"from": a, "to": b, "condition": None}
                for a, b in zip(order, order[1:], strict=False)
            ],
            "max_iterations": 10,
        },
    }
    AgentGraphDraft.model_validate(draft)
    return draft


def _payload(result: Any) -> Any:
    return getattr(result, "payload", result)


def _dependency(kind: str, component_id: str, digest: str) -> dict[str, str]:
    return {"component_id": component_id, "kind": kind, "definition_digest": digest}


_VOLATILE = frozenset({"actor_scope", "purpose_id", "policy_digest"})


def _same_body(entry: Any, *, draft: Mapping[str, Any]) -> bool:
    """The current entry already carries every body field of ``draft``."""
    if not isinstance(entry, Mapping):
        return False
    return all(
        entry.get(key) == value for key, value in draft.items() if key not in _VOLATILE
    )


class _Writer:
    """Idempotent publishes of one tenant: read the head, reuse or CAS-write."""

    def __init__(self, send: Send, context_for: ContextFor, tenant: str) -> None:
        self._send = send
        self._context_for = context_for
        self._tenant = tenant

    async def publish(
        self,
        method: str,
        head: dict[str, Any],
        key: str,
        build: Callable[[Mapping[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
        result_field: str,
        same: Callable[[Any], bool],
    ) -> str:
        """Publish through ``build(context) -> (draft, op request)``; return the digest."""
        current = _payload(await self._send(method, {"op": head}, None))
        if current is not None and same(current):
            return str(current["definition_digest"])
        expected = 0 if current is None else int(current["entry_revision"])
        idem = f"deploy-library:{self._tenant}:{key}"
        context = dict(await self._context_for(self._tenant, idem))
        context["expected_revision"] = expected
        context["idempotency_key"] = idem
        _draft, request = build(context)
        request["context"] = context
        result = _payload(
            await self._send(
                method, {"op": {"op": "publish", "request": request}}, idem
            )
        )
        digest = result[result_field]["definition_digest"]
        if not isinstance(digest, str) or not digest:
            raise ValueError("the engine returned no definition digest")
        return digest


async def publish_deployment_library(
    send: Send,
    context_for: ContextFor,
    tenant: str,
    bindings: Mapping[tuple[str, str], ToolBinding],
) -> dict[str, bool]:
    """Publish the base, the template and one graph per fully configured target.

    Returns ``{target: published}``. A target with an unconfigured slot is not
    published and is logged ``DEPLOY_TOOL_UNCONFIGURED`` per missing slot.
    """
    from medulla.generated.agent_component import AgentComponentDraft  # noqa: F401

    writer = _Writer(send, context_for, tenant)
    probe = dict(await context_for(tenant, "deploy-library:probe"))
    pins: dict[str, dict[str, str]] = {}

    async def component(draft: dict[str, Any]) -> None:
        cid, kind = draft["component_id"], draft["kind"]

        def same(entry: Any) -> bool:
            return (
                isinstance(entry, Mapping)
                and entry.get("content_digest") == draft["content_digest"]
            )

        def build(ctx: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            fresh = dict(draft)
            fresh.update(
                actor_scope=ctx["actor_scope"],
                purpose_id=ctx["purpose_id"],
                policy_digest=ctx["policy_digest"],
            )
            return fresh, {"component": fresh, "evaluation_receipt_digest": None}

        digest = await writer.publish(
            "AgentComponent",
            {"op": "current", "component_id": cid, "tenant_id": tenant},
            f"component:{cid}:{draft['content_digest']}",
            build,
            "component",
            same,
        )
        pins[cid] = _dependency(kind, cid, digest)

    configured = {
        target: [role for role in ROLES if (role, target) in bindings]
        for target in TARGETS
    }
    for target, roles in configured.items():
        for role in ROLES:
            if role not in roles:
                logger.warning(
                    "DEPLOY_TOOL_UNCONFIGURED: no tool for %s on %s; the target "
                    "stays unassemblable",
                    role,
                    target,
                )
    published = {target: False for target in TARGETS}
    ready = [t for t, roles in configured.items() if len(roles) == len(ROLES)]
    if not ready:
        return published
    for draft in base_components(tenant, probe):
        await component(draft)
    usable = {k: v for k, v in bindings.items() if k[1] in ready}
    for draft in tool_components(tenant, probe, usable):
        await component(draft)

    def build_template(ctx: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        draft = template_draft(tenant, ctx, pins)
        return draft, {"template": draft}

    template_probe = template_draft(tenant, probe, pins)
    template_digest = await writer.publish(
        "AgentTemplate",
        {"op": "current", "template_id": TEMPLATE_ID, "tenant_id": tenant},
        f"template:{_digest(template_probe['base'])}",
        build_template,
        "template",
        functools.partial(_same_body, draft=template_probe),
    )
    for target in ready:
        graph_probe = graph_draft(tenant, probe, target, template_digest, pins)

        def build_graph(
            ctx: Mapping[str, Any], target: str = target
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            draft = graph_draft(tenant, ctx, target, template_digest, pins)
            return draft, {"graph": draft}

        await writer.publish(
            "AgentGraph",
            {"op": "current", "graph_id": graph_probe["graph_id"], "tenant_id": tenant},
            f"graph:{target}:{_digest(graph_probe['shape'])}",
            build_graph,
            "graph",
            functools.partial(_same_body, draft=graph_probe),
        )
        published[target] = True
    return published


async def publish_at_startup(
    send: Send,
    context_for: ContextFor,
    tenants: Sequence[str],
    bindings: Mapping[tuple[str, str], ToolBinding],
    *,
    attempts: int = 4,
    initial_delay_s: float = 0.5,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, dict[str, bool]]:
    """Publish every served tenant's library with bounded retry and backoff.

    A tenant that still fails is logged and start-up continues; its targets
    stay unassemblable.
    """
    results: dict[str, dict[str, bool]] = {}
    for tenant in dict.fromkeys(tenants):
        results[tenant] = {target: False for target in TARGETS}
        for attempt in range(1, attempts + 1):
            try:
                results[tenant] = await publish_deployment_library(
                    send, context_for, tenant, bindings
                )
            except Exception as exc:  # noqa: BLE001 - logged; the targets stay unpublished
                if attempt == attempts:
                    logger.error(
                        "deployment library not published for %s after %d attempts "
                        "(%s: %s)",
                        tenant,
                        attempts,
                        type(exc).__name__,
                        exc,
                    )
                    break
                await sleep(initial_delay_s * 2 ** (attempt - 1))
            else:
                break
    return results


__all__ = [
    "ROLES",
    "TARGETS",
    "TEMPLATE_ID",
    "ToolBinding",
    "base_components",
    "graph_draft",
    "load_tool_bindings",
    "publish_at_startup",
    "publish_deployment_library",
    "template_draft",
    "tool_bindings",
    "tool_components",
]
