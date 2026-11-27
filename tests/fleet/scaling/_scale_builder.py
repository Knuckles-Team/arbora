"""A structural double of ``medulla.decision_client.scale_intent_request`` (engine main).

Same body as the engine builder (MEDULLA-DECISION-ENGINE-R833), over the pinned
``decide_request`` helpers, so the tests run before the re-pin that carries it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest


def scale_intent_request(
    tenant_id: str,
    question_id: str,
    graph: str,
    intent: Mapping[str, Any],
    feature_schema: Mapping[str, Any],
    *,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from medulla.decision_client import DeclaredOption, decide_request, declared, param

    return decide_request(
        tenant_id,
        (question_id, "scale_intent", "ordinary"),
        declared([DeclaredOption("verdict")]),
        feature_schema,
        policy=policy,
        params=[
            param("graph", "text", graph),
            param("intent", "text", json.dumps(dict(intent), sort_keys=True)),
        ],
    )


def declare_scale_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned release as after the re-pin: the builder exists."""
    monkeypatch.setattr(
        "medulla.decision_client.scale_intent_request",
        scale_intent_request,
        raising=False,
    )
