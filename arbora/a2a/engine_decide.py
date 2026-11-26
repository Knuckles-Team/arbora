"""The production ``ask_decide`` seam of the confirm chain (ARBORA-A2A-R005.4, R006.4).

``compose_approval_chain(ask_decide=...)`` reads engine verdicts through one seam of
``(question kind, params, tenant)``. This module builds that seam over the engine
``Decide`` method: the typed question, its one declared ``admission`` option, the
tenant's published feature-schema pin, the default decision policy and the typed text
params the two question families require (``graph`` included). The record is checked
against the request and the verified principal before it is returned, so a record that
answers another question, tenant or caller is never read as a verdict.

Arbora decides nothing here. A missing ``Decide`` method, a missing feature-schema pin or
a record that does not answer this request raises, and the admitters read a raise as
``ENGINE_UNAVAILABLE``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .engine_deciders import HELD_ITEM_KIND, TOOL_ACTION_KIND, AskDecide

#: The one option both admission families declare.
ADMISSION_OPTION = "admission"

#: The text params each family requires besides ``graph`` (engine ``constraints``).
FAMILY_PARAMS: Mapping[str, tuple[str, ...]] = {
    TOOL_ACTION_KIND: (
        "pending_call_id",
        "params_digest",
        "trust_mark_ref",
        "action_class",
    ),
    HELD_ITEM_KIND: ("item_kind", "item_ref", "content_digest"),
}

Send = Callable[[str, dict[str, Any], str], Awaitable[Any]]


def question_id(kind: str, params: Mapping[str, str]) -> str:
    """A stable id of one question: the kind and the reference it governs."""
    anchor = params.get("pending_call_id") or params.get("item_ref") or ""
    return f"a2a.{kind}:{anchor}"


def decide_request_for(
    kind: str,
    params: Mapping[str, str],
    tenant: str,
    *,
    graph: str,
    feature_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """The engine ``DecideRequest`` of one admission question."""
    from medulla.decision_client import DeclaredOption, decide_request, declared, param

    names = FAMILY_PARAMS.get(kind)
    if names is None:
        raise ValueError(f"unknown admission question kind {kind!r}")
    missing = [name for name in names if not params.get(name)]
    if missing:
        raise ValueError(f"the {kind} question lacks {', '.join(missing)}")
    typed = [param("graph", "text", graph)]
    typed += [param(name, "text", str(params[name])) for name in names]
    return decide_request(
        tenant,
        (question_id(kind, params), kind, "ordinary"),
        declared([DeclaredOption(ADMISSION_OPTION)]),
        feature_schema,
        params=typed,
    )


def build_ask_decide(
    send: Send,
    *,
    graph: str,
    feature_schema: Callable[[str], Mapping[str, Any]],
    principal: Callable[[], str],
) -> AskDecide:
    """``AskDecide`` over the engine ``Decide`` method.

    ``send(method, params, tenant)`` returns the decoded engine batch; ``feature_schema``
    is the tenant's published pin and raises while none is published; ``principal`` is
    the identity the engine saw, which the record's receipt must name.
    """
    from arbora.deployment.placement import check_receipt, single_record

    async def ask(
        kind: str, params: Mapping[str, str], tenant: str
    ) -> Mapping[str, Any] | None:
        request = decide_request_for(
            kind, params, tenant, graph=graph, feature_schema=feature_schema(tenant)
        )
        record = single_record(await send("Decide", request, tenant))
        check_receipt(record, request, principal())
        return record

    return ask
