"""The engine tool ranking behind tool search (ARBORA-OPS-R866).

For a ``query`` search the multiplexer asks the engine ``Decide`` with the
``ToolRelevance`` question: the caller's task text, role IRI and context budget pass
through unchanged. The sealed answer lists every ranked tool, best first. Arbora ranks
nothing locally; a failed call reaches the caller as ``UNAVAILABLE`` from
``MultiplexerOps``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

#: ``send(method, params, tenant)`` reaches the engine as the host service identity.
RankSend = Callable[[str, dict[str, Any], str], Awaitable[Any]]

#: The most tools one ranking request keeps (the engine's ``top_k`` bound).
MAX_RANKED = 50


def engine_tool_order(
    send: RankSend,
) -> Callable[..., Awaitable[Sequence[str]]]:
    """The production ``tool_ranker`` of ``MultiplexerOps`` over engine ``Decide``."""
    from arbora.api.ops._decide import record_of

    async def order(
        caller: Any,
        *,
        query: str,
        role: str | None = None,
        context_budget_tokens: int | None = None,
    ) -> Sequence[str]:
        question: dict[str, Any] = {
            "kind": "tool_relevance",
            "task_text": query,
            "agent_role_iri": role or "",
            "top_k": MAX_RANKED,
        }
        if context_budget_tokens is not None:
            question["context_budget_tokens"] = context_budget_tokens
        tenant = str(caller.tenant)
        record = record_of(
            await send(
                "Decide",
                {"request": {"tenant_id": tenant, "question": question}},
                tenant,
            )
        )
        ranked = record.get("ranked")
        if not isinstance(ranked, Sequence) or isinstance(ranked, (str, bytes)):
            raise ValueError("the engine answered no tool ranking")
        return [str(row["tool_id"]) for row in ranked]

    return order


__all__ = ["MAX_RANKED", "RankSend", "engine_tool_order"]
