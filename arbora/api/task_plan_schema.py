"""The published task-plan feature-schema pin (ARBORA-RELEASE-R003.4.4).

``agents.plan_run`` sends the engine a ``TaskPlan`` decision request that names
one published feature-schema component. Arbora does not guess that pin at run
time. ``scripts/gen_api.py`` derives it from the installed engine package and
commits it to ``arbora/api/generated/task_plan_schema.json``. At server
start-up, Arbora derives the pin again from the installed engine and binds it
only when it matches the committed pin and the pin is final. A drifted or
unfinished pin leaves ``agents.plan_run`` refusing with ``UNAVAILABLE``.

The pin is final when it carries the engine's ``definition_digest`` for the
schema component. The engine computes that digest when it publishes the
component; the installed engine exposes it through
``epistemic_graph.decision_client.TASK_PLAN_FEATURE_SCHEMA``. An engine
release without that export yields an unfinished pin.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COMPONENT_ID = "arbora.agents.plan_run.feature_schema"
KIND = "feature_schema"
PIN_PATH = Path(__file__).resolve().parent / "generated" / "task_plan_schema.json"
#: The fields whose drift unbinds the pin. ``engine_version`` is informational:
#: a newer engine with the same schema body and definition keeps the pin valid.
VERIFIED_FIELDS = (
    "component_id",
    "kind",
    "schema_version",
    "content_digest",
    "definition_digest",
)


def schema_body(stat: Any) -> dict[str, Any]:
    """The task-plan feature schema, built with the engine's own helpers."""
    return stat.feature_schema_body(
        [stat.feature("prompt", stat.text_bm25("summary", "query"))]
    )


def _published_definition(facade: Any, content_digest: str) -> str | None:
    """The engine's definition digest for this schema, when it publishes one."""
    published = getattr(facade, "TASK_PLAN_FEATURE_SCHEMA", None)
    if not isinstance(published, Mapping):
        return None
    if published.get("content_digest") not in (None, content_digest):
        return None
    digest = published.get("definition_digest")
    return digest if isinstance(digest, str) and digest else None


def generate_pin(
    *,
    stat: Any = None,
    facade: Any = None,
    engine_version: str | None = None,
) -> dict[str, Any]:
    """Derive the pin from the installed engine (or the given fixture modules)."""
    if stat is None:
        stat = importlib.import_module("epistemic_graph.decision_stat")
    if facade is None:
        facade = importlib.import_module("epistemic_graph.decision_client")
    if engine_version is None:
        from importlib.metadata import version

        engine_version = version("epistemic-graph")
    body = schema_body(stat)
    content_digest, _attributes = stat.encode_body(body)
    definition = _published_definition(facade, content_digest)
    return {
        "component_id": COMPONENT_ID,
        "kind": KIND,
        "schema_version": body["schema_version"],
        "content_digest": content_digest,
        "definition_digest": definition,
        "engine_version": engine_version,
        "final": definition is not None,
    }


def pin_bytes(pin: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(pin), indent=2, sort_keys=True) + "\n").encode()


def load_pin(path: Path = PIN_PATH) -> dict[str, Any]:
    return dict(json.loads(path.read_bytes()))


def drift(committed: Mapping[str, Any], installed: Mapping[str, Any]) -> list[str]:
    """The verified fields where the committed pin and the installed engine differ."""
    return [
        name for name in VERIFIED_FIELDS if committed.get(name) != installed.get(name)
    ]


def bound_feature_schema(
    committed: Mapping[str, Any], installed: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """``(pin, None)`` when the pin may bind, else ``(None, reason)``."""
    changed = drift(committed, installed)
    if changed:
        return None, "task-plan schema pin drift: " + ", ".join(changed)
    if not committed.get("final") or not committed.get("definition_digest"):
        return None, "task-plan schema pin is not final"
    return {
        "component_id": committed["component_id"],
        "kind": committed["kind"],
        "definition_digest": committed["definition_digest"],
    }, None


def startup_feature_schema(path: Path = PIN_PATH) -> dict[str, Any] | None:
    """Verify the committed pin against the installed engine at start-up.

    Returns the pin to bind, or ``None`` after logging why it is not bound.
    """
    try:
        committed = load_pin(path)
        installed = generate_pin()
    except Exception as exc:
        logger.error("task-plan schema pin not bound: %s", exc)
        return None
    pin, reason = bound_feature_schema(committed, installed)
    if pin is None:
        logger.error(
            "task-plan schema pin not bound (%s); agents.plan_run refuses UNAVAILABLE",
            reason,
        )
    return pin


__all__ = [
    "COMPONENT_ID",
    "KIND",
    "PIN_PATH",
    "bound_feature_schema",
    "drift",
    "generate_pin",
    "load_pin",
    "pin_bytes",
    "schema_body",
    "startup_feature_schema",
]
