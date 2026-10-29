"""The task-plan feature-schema pin for ``agents.plan_run`` (ARBORA-RELEASE-R003.4.4).

``agents.plan_run`` sends the engine a ``TaskPlan`` decision request. That
request names one feature-schema component in the Agent Library. Arbora gets
that component from the engine package in two steps and never makes one up.

1. **Generation time.** ``scripts/gen_api.py`` reads the engine's exported task-plan
   feature schema (``TASK_PLAN_FEATURE_SCHEMA``: body, ``schema_version`` and
   content digest). It writes the body and its digest to
   ``arbora/api/generated/task_plan_schema.json``.
2. **Serving time.** At start-up Arbora compares the committed digest with the
   installed engine's export. If they match, Arbora publishes the body to the
   Agent Library once per tenant. The publish is idempotent. Arbora then uses
   the ``definition_digest`` that the engine returns as the pin. If they differ,
   ``agents.plan_run`` stays ``UNAVAILABLE`` and the reason is logged.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COMPONENT_ID = "arbora.agents.plan_run.feature_schema"
KIND = "feature_schema"
COMPONENT_VERSION = "1.0.0"
PURPOSE_ID = "agent_library.task_plan_schema_publish"
PIN_PATH = Path(__file__).resolve().parent / "generated" / "task_plan_schema.json"
EXPORT_NAME = "TASK_PLAN_FEATURE_SCHEMA"
#: Where the engine exports the schema, searched in order. Confirm the exact
#: module when epistemic-graph 2.27.5 publishes, then keep only that path.
EXPORT_MODULES = (
    "epistemic_graph.decision_client",
    "epistemic_graph.decision_stat",
    "epistemic_graph",
)
#: The fields whose drift keeps the op unavailable. ``engine_version`` is
#: informational: a newer engine that exports the same body keeps the pin.
VERIFIED_FIELDS = ("component_id", "kind", "schema_version", "content_digest")

Publish = Callable[[str, Mapping[str, Any]], Coroutine[Any, Any, str]]
BindClient = Callable[[str], AbstractAsyncContextManager[Any]]


def _stat() -> Any:
    return importlib.import_module("epistemic_graph.decision_stat")


def installed_export(
    modules: Sequence[str] = EXPORT_MODULES, stat: Any = None
) -> tuple[str, dict[str, Any]] | None:
    """``(module path, export)`` from the installed engine, or ``None``.

    An export whose body does not hash to its own content digest is refused.
    """
    for name in modules:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        export = getattr(module, EXPORT_NAME, None)
        if export is None:
            continue
        if not isinstance(export, Mapping) or not {
            "body",
            "schema_version",
            "content_digest",
        } <= set(export):
            raise ValueError(f"{name}.{EXPORT_NAME} is not body/schema_version/digest")
        digest, _attributes = (stat or _stat()).encode_body(export["body"])
        if digest != export["content_digest"]:
            raise ValueError(f"{name}.{EXPORT_NAME} body does not match its digest")
        return f"{name}.{EXPORT_NAME}", dict(export)
    return None


def generate_pin(
    *,
    modules: Sequence[str] = EXPORT_MODULES,
    stat: Any = None,
    engine_version: str | None = None,
) -> dict[str, Any]:
    """The pin from the installed engine (or from the given fixture modules)."""
    if engine_version is None:
        from importlib.metadata import version

        engine_version = version("medulla")
    found = installed_export(modules, stat)
    pin: dict[str, Any] = {
        "component_id": COMPONENT_ID,
        "kind": KIND,
        "engine_version": engine_version,
        "available": found is not None,
        "export": None,
        "schema_version": None,
        "content_digest": None,
        "body": None,
    }
    if found is not None:
        path, export = found
        pin.update(
            export=path,
            schema_version=export["schema_version"],
            content_digest=export["content_digest"],
            body=export["body"],
        )
    return pin


def pin_bytes(pin: Mapping[str, Any]) -> bytes:
    # The engine codec hashes body insertion order. Sort only pin metadata;
    # recursively sorting the exported body changes its content digest.
    ordered = {key: pin[key] for key in sorted(pin)}
    return (json.dumps(ordered, indent=2) + "\n").encode()


def load_pin(path: Path = PIN_PATH) -> dict[str, Any]:
    return dict(json.loads(path.read_bytes()))


def drift(committed: Mapping[str, Any], installed: Mapping[str, Any]) -> list[str]:
    """The verified fields where the committed pin and the installed engine differ."""
    return [
        name for name in VERIFIED_FIELDS if committed.get(name) != installed.get(name)
    ]


def verify(committed: Mapping[str, Any], installed: Mapping[str, Any]) -> str | None:
    """``None`` when the committed pin may be published, else the reason."""
    if not committed.get("available"):
        return "the committed task-plan schema pin has no engine export"
    if not installed.get("available"):
        return f"the installed engine does not export {EXPORT_NAME}"
    body = committed.get("body")
    if not isinstance(body, Mapping):
        return "the committed task-plan schema body is missing or not an object"
    stat = _stat()
    try:
        digest, _attributes = stat.encode_body(body)
    except (TypeError, ValueError):
        return "the committed task-plan schema body cannot be encoded by the engine"
    if digest != committed.get("content_digest"):
        return "the committed task-plan schema body does not match its digest"
    changed = drift(committed, installed)
    if changed:
        return "task-plan schema pin drift: " + ", ".join(changed)
    return None


def publication(
    pin: Mapping[str, Any], tenant: str, context: Mapping[str, Any], stat: Any = None
) -> dict[str, Any]:
    """The ``AgentComponent`` publish params for the pinned body in ``tenant``."""
    body = (stat or _stat()).body_attributes_for_publish(pin["body"])
    if body["content_digest"] != pin["content_digest"]:
        raise ValueError("task-plan schema body does not match the pinned digest")
    component = {
        "component_id": pin["component_id"],
        "kind": pin["kind"],
        "version": COMPONENT_VERSION,
        "content_digest": body["content_digest"],
        "attributes": body["attributes"],
        "content_ref": None,
        "facts": {"facts": "opaque"},
        "provenance": {"origin": "native"},
        "summary": "Feature schema for engine-decided task plans (agents.plan_run)",
        "classification": [],
        "requires": [],
        "declared_capabilities": [],
        "required_capabilities": [],
        "declared_required_capabilities": [],
        "tenant_id": tenant,
        "actor_scope": context["actor_scope"],
        "purpose_id": context["purpose_id"],
        "policy_digest": context["policy_digest"],
        "source_revision": f"epistemic-graph-{pin['engine_version']}",
        "source_revision_digest": pin["content_digest"],
    }
    return {
        "op": {
            "op": "publish",
            "request": {
                "context": dict(context),
                "component": component,
                "evaluation_receipt_digest": None,
            },
        }
    }


def idempotency_key(pin: Mapping[str, Any], tenant: str) -> str:
    return f"task-plan-schema:{tenant}:{pin['content_digest']}"


class TaskPlanSchemaPins:
    """The verified pin, published once per tenant and cached process-wide."""

    def __init__(self, pin: Mapping[str, Any]) -> None:
        self.pin = dict(pin)
        self._published: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def for_tenant(self, tenant: str, publish: Publish) -> dict[str, Any]:
        """The ``ComponentDependency`` pin for ``tenant``; publishes on first use."""
        if tenant not in self._published:
            lock = self._locks.setdefault(tenant, asyncio.Lock())
            async with lock:
                if tenant not in self._published:
                    digest = await publish(tenant, self.pin)
                    if not isinstance(digest, str) or not digest:
                        raise ValueError("the engine returned no definition digest")
                    self._published[tenant] = digest
        return {
            "component_id": self.pin["component_id"],
            "kind": self.pin["kind"],
            "definition_digest": self._published[tenant],
        }


def startup_pins(path: Path = PIN_PATH) -> TaskPlanSchemaPins | None:
    """Verify the committed pin against the installed engine at start-up.

    Returns the publisher for the verified pin, or ``None`` after logging why
    ``agents.plan_run`` stays unavailable.
    """
    try:
        committed = load_pin(path)
        reason = verify(committed, generate_pin())
    except Exception as exc:
        reason = f"task-plan schema pin unreadable: {exc}"
    if reason is not None:
        logger.error(
            "task-plan schema pin not bound (%s); agents.plan_run refuses UNAVAILABLE",
            reason,
        )
        return None
    return TaskPlanSchemaPins(committed)


def engine_publish(
    client_for: Callable[[str], Awaitable[Any]],
    context_for: Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    graph: str | None = None,
    *,
    bind_client: BindClient | None = None,
) -> Publish:
    """Publish through the engine; reuse a current entry with the same body.

    A restart reads the current entry first, so a re-publish never collides
    with the earlier publish under a fresh mutation context.
    Only a new write resolves the async authority provider for this exact pin.
    """

    async def _publish(client: Any, tenant: str, pin: Mapping[str, Any]) -> str:
        from epistemic_graph.generated.agent_component import AgentComponentOpCurrent
        from epistemic_graph.generated.storage import (
            send_agent_component,
            send_agent_component_current,
        )

        current = await send_agent_component_current(
            client,
            AgentComponentOpCurrent(
                op="current", component_id=pin["component_id"], tenant_id=tenant
            ),
            graph,
        )
        if current is not None and current.content_digest == pin["content_digest"]:
            return str(current.definition_digest)
        # Agent Library publishes are compare-and-swapped by the native
        # ``expected_revision`` in the mutation context.  The current read is
        # authoritative for this tenant: a missing head names revision zero,
        # while a mismatching head must be published against the exact
        # revision we observed.  The native write then refuses a concurrent
        # change instead of allowing this bootstrap to overwrite it.
        expected_revision = 0 if current is None else int(current.entry_revision)
        if expected_revision < 0:
            raise ValueError("AgentComponent current entry revision cannot be negative")
        context = dict(await context_for(tenant, pin))
        context["expected_revision"] = expected_revision
        result = await send_agent_component(
            client,
            publication(pin, tenant, context),
            graph,
            idempotency_key=idempotency_key(pin, tenant),
        )
        payload: Any = getattr(result, "payload", result)
        return str(payload["component"]["definition_digest"])

    async def publish(tenant: str, pin: Mapping[str, Any]) -> str:
        if bind_client is None:
            return await _publish(await client_for(tenant), tenant, pin)
        async with bind_client(tenant) as client:
            return await _publish(client, tenant, pin)

    return publish


__all__ = [
    "COMPONENT_ID",
    "EXPORT_MODULES",
    "EXPORT_NAME",
    "KIND",
    "PIN_PATH",
    "TaskPlanSchemaPins",
    "drift",
    "engine_publish",
    "generate_pin",
    "idempotency_key",
    "installed_export",
    "load_pin",
    "pin_bytes",
    "publication",
    "startup_pins",
    "verify",
]
