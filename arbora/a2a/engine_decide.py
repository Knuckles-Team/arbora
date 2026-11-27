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

from .engine_deciders import (
    CREDENTIAL_REUSE_KIND,
    HELD_ITEM_KIND,
    TOOL_ACTION_KIND,
    AskDecide,
)

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
    CREDENTIAL_REUSE_KIND: (
        "owner",
        "agent_id",
        "connector",
        "provider",
        "scopes",
        "purpose",
    ),
}

Send = Callable[[str, dict[str, Any], str], Awaitable[Any]]


def _anchor(params: Mapping[str, str]) -> str:
    """The reference one question governs: the pending call, the held item or the agent."""
    for name in ("pending_call_id", "item_ref", "agent_id"):
        if params.get(name):
            return str(params[name])
    return ""


def question_id(kind: str, params: Mapping[str, str]) -> str:
    """A stable id of one question: the kind and the reference it governs."""
    anchor = _anchor(params)
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
    from arbora.deployment.placement import check_receipt, send_decide, single_record

    async def ask(
        kind: str, params: Mapping[str, str], tenant: str
    ) -> Mapping[str, Any] | None:
        request = decide_request_for(
            kind, params, tenant, graph=graph, feature_schema=feature_schema(tenant)
        )
        record = single_record(await send_decide(send, request, tenant))
        check_receipt(record, request, principal())
        return record

    return ask


class HostDecide:
    """The A2A door's engine ``Decide`` port, bound once the host invocation exists.

    The host builds its A2A service before the served invocation (which owns the
    engine sender) is composed, so the port starts unbound and refuses closed: an
    unbound call raises, and the router and harness decider read a raise as
    unavailable. ``bind`` hands it the same ``Decide`` sender, tenant feature-schema
    pin and service principal the confirm deciders use (ARBORA-A2A-R005.4).
    """

    def __init__(self) -> None:
        self._send: Send | None = None
        self._feature_schema: Callable[[str], Mapping[str, Any]] | None = None
        self._principal: Callable[[], str] | None = None

    @property
    def bound(self) -> bool:
        return self._send is not None

    def bind(
        self,
        send: Send,
        *,
        feature_schema: Callable[[str], Mapping[str, Any]],
        principal: Callable[[], str],
    ) -> None:
        if self._send is not None:
            raise RuntimeError("the A2A decision port is already bound")
        self._send, self._feature_schema, self._principal = (
            send,
            feature_schema,
            principal,
        )

    async def __call__(self, request: dict[str, Any]) -> Any:
        if self._send is None:
            raise RuntimeError("the A2A decision port is not bound")
        from arbora.deployment.placement import send_decide

        return await send_decide(self._send, request, str(request["tenant_id"]))

    def feature_schema(self, tenant: str) -> Mapping[str, Any]:
        if self._feature_schema is None:
            raise RuntimeError("the A2A decision port is not bound")
        return self._feature_schema(tenant)

    def principal(self) -> str:
        if self._principal is None:
            raise RuntimeError("the A2A decision port is not bound")
        return self._principal()
