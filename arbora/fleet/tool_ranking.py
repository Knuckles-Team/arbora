"""The engine tool ranking behind tool search (ARBORA-OPS-R866).

For a ``query`` search the multiplexer asks the engine ``Decide`` with the
``tool_relevance`` question over the tenant's published tools. The typed
``tool_assignment`` parameter carries the caller's task text, role IRI and context
budget (``agent_role_iri`` and ``context_budget_tokens`` are required by the engine). A
search that names no budget is asked with the configured budget, never refused. The
sealed answer is an advisory outcome whose scored options are the selected tools, best
first; an abstention (an empty catalog, a budget below the cheapest tool) ranks nothing
and the catalog order stands. Arbora ranks nothing locally; a failed call reaches the
caller as ``UNAVAILABLE`` from ``MultiplexerOps``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

#: ``send(method, params, tenant)`` reaches the engine as the host service identity.
RankSend = Callable[[str, dict[str, Any], str], Awaitable[Any]]

#: The most tools one ranking request keeps (the engine's ``top_k`` bound).
MAX_RANKED = 50

#: The setting naming the context budget of a search that supplies none.
BUDGET_SETTING = "ARBORA_TOOL_SEARCH_CONTEXT_BUDGET_TOKENS"
DEFAULT_BUDGET_TOKENS = 4000


def configured_budget() -> int:
    """The context budget, in tokens, asked for a search that names none."""
    from arbora.core.settings import setting

    return int(setting(BUDGET_SETTING, DEFAULT_BUDGET_TOKENS))


def order_request(
    tenant: str,
    feature_schema: Mapping[str, Any],
    *,
    query: str,
    role: str | None,
    context_budget_tokens: int | None,
) -> dict[str, Any]:
    """The engine ``DecideRequest`` of one ``tool_relevance`` question."""
    from medulla.decision_client import tool_assignment_request

    budget = (
        configured_budget() if context_budget_tokens is None else context_budget_tokens
    )
    anchor = hashlib.sha256(f"{tenant}\0{role or ''}\0{query}".encode()).hexdigest()
    return tool_assignment_request(
        tenant,
        f"tool-relevance:{anchor[:32]}",
        feature_schema,
        task_text=query,
        agent_role_iri=role or "",
        top_k=MAX_RANKED,
        context_budget_tokens=budget,
    )


def ordered_tool_ids(record: Mapping[str, Any]) -> list[str]:
    """The selected tools of a sealed record, best first; an abstention ranks none."""
    outcome = record.get("outcome")
    if not isinstance(outcome, Mapping):
        raise ValueError("the engine answered no tool ranking")
    if outcome.get("outcome") == "abstained":
        return []
    scores = outcome.get("scores")
    if outcome.get("outcome") != "advisory" or not isinstance(scores, Sequence):
        raise ValueError("the engine answered no tool ranking")
    return [str(row["option_id"]) for row in scores]


def engine_tool_order(
    send: RankSend,
    *,
    feature_schema: Callable[[str], Mapping[str, Any]],
    principal: Callable[[], str],
) -> Callable[..., Awaitable[Sequence[str]]]:
    """The production ``tool_ranker`` of ``MultiplexerOps`` over engine ``Decide``."""
    from arbora.deployment.placement import check_receipt, send_decide, single_record

    async def order(
        caller: Any,
        *,
        query: str,
        role: str | None = None,
        context_budget_tokens: int | None = None,
    ) -> Sequence[str]:
        tenant = str(caller.tenant)
        request = order_request(
            tenant,
            feature_schema(tenant),
            query=query,
            role=role,
            context_budget_tokens=context_budget_tokens,
        )
        record = single_record(await send_decide(send, request, tenant))
        check_receipt(record, request, principal())
        return ordered_tool_ids(record)

    return order


__all__ = [
    "BUDGET_SETTING",
    "DEFAULT_BUDGET_TOKENS",
    "MAX_RANKED",
    "RankSend",
    "configured_budget",
    "engine_tool_order",
    "ordered_tool_ids",
    "order_request",
]
