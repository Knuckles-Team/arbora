"""Actuate a capacity adjustment only on the engine's allow (ARBORA-DEPLOY-R017.17).

Arbora decides no feasibility. One typed ``Decide`` question of kind
``capacity_proposal`` asks the engine to evaluate the adjustment and the engine
answers ``allow`` or ``refuse:<CODE>``. The scale step runs only on ``allow``
for the same ``proposal_digest``, ``premise_revision`` and ``policy_revision``
as the request, as one governed ``fleet.call`` keyed ``capacity:<digest>``. The
receipt carries the capacity and headroom read back from the engine.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import OpError
from arbora.deployment.placement import check_receipt, single_record

#: The one Decide question this module asks.
CAPACITY_QUESTION_ID = "deployment.capacity"

#: The proposal fields the engine verdict is bound to.
BOUND_FIELDS: tuple[str, ...] = (
    "proposal_digest",
    "premise_revision",
    "policy_revision",
)

Decide = Callable[[dict[str, Any]], Awaitable[Any]]
StepInvoke = Callable[[str, Mapping[str, Any], Any, str], Awaitable[Any]]
ReadBack = Callable[[str], Awaitable[Mapping[str, Any]]]


#: The parameter the engine adds to the sealed record: the headroom and epoch it read.
OBSERVATION_PARAM = "observation"


def _answer(record: Mapping[str, Any]) -> str:
    """The verdict: the first scored option of the advisory outcome.

    The engine answers a ``capacity_proposal`` question as an advisory outcome
    whose one scored option id is ``allow`` or ``refuse:<CODE>``.
    """
    outcome = record.get("outcome")
    scores = outcome.get("scores") if isinstance(outcome, Mapping) else None
    first = scores[0] if isinstance(scores, list) and scores else None
    answer = first.get("option_id") if isinstance(first, Mapping) else None
    if not isinstance(answer, str) or not answer:
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "the record carries no verdict"}
        )
    return answer


def _without_observation(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """The record as the caller asked it: the engine-filled observation set aside."""
    inputs = record.get("inputs")
    params = inputs.get("params") if isinstance(inputs, Mapping) else None
    if not isinstance(params, list):
        return record
    kept = [
        p
        for p in params
        if not (isinstance(p, Mapping) and p.get("name") == OBSERVATION_PARAM)
    ]
    return {**record, "inputs": {**inputs, "params": kept}}


async def verdict(
    decide: Decide,
    *,
    tenant: str,
    principal: str,
    graph: str,
    feature_schema: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> str:
    """The engine's answer to one proposal: ``allow`` or ``refuse:<CODE>``.

    The record must echo exactly this request (``check_receipt``), so a verdict
    for other revisions or another digest is a ``RECEIPT_MISMATCH``.
    """
    from medulla.decision_client import DeclaredOption, decide_request, declared, param

    missing = [name for name in BOUND_FIELDS if not proposal.get(name)]
    if missing:
        raise OperationRefused("INVALID_ARGUMENT", {"missing": missing})
    request = decide_request(
        tenant,
        (CAPACITY_QUESTION_ID, "capacity_proposal", "ordinary"),
        declared([DeclaredOption("verdict")]),
        feature_schema,
        params=[
            param("graph", "text", graph),
            param("proposal", "text", json.dumps(dict(proposal), sort_keys=True)),
        ],
    )
    record = single_record(await decide(request))
    check_receipt(_without_observation(record), request, principal)
    return _answer(record)


async def actuate(
    decide: Decide,
    invoke: StepInvoke,
    caller: Any,
    *,
    tenant: str,
    principal: str,
    graph: str,
    feature_schema: Mapping[str, Any],
    proposal: Mapping[str, Any],
    scale_tool: Mapping[str, Any],
    read_back: ReadBack,
) -> dict[str, Any]:
    """Evaluate, actuate once on ``allow``, and return the applied-state receipt.

    A ``refuse`` verdict, a stale premise or a mismatch actuates nothing and
    returns the engine reason code.
    """
    answer = await verdict(
        decide,
        tenant=tenant,
        principal=principal,
        graph=graph,
        feature_schema=feature_schema,
        proposal=proposal,
    )
    digest = str(proposal["proposal_digest"])
    if answer != "allow":
        code = (
            answer.removeprefix("refuse:") if answer.startswith("refuse:") else answer
        )
        raise EngineRefusal(code, details={"proposal_digest": digest})
    outcome = await invoke(
        "fleet.call",
        {
            "server": scale_tool["server"],
            "tool": scale_tool["tool"],
            "arguments": {
                "resource_class_iri": proposal.get("resource_class_iri"),
                "proposed_capacity": proposal.get("proposed_capacity"),
            },
        },
        caller,
        f"capacity:{digest}",
    )
    if isinstance(outcome, OpError) or getattr(outcome, "code", "OK") != "OK":
        raise OperationRefused(
            "CAPACITY_ACTUATION_FAILED",
            {"proposal_digest": digest, "error": getattr(outcome, "code", "")},
        )
    observed = await read_back(str(proposal.get("resource_class_iri")))
    return {
        "proposal_digest": digest,
        "verdict": "allow",
        "observed_capacity": observed["capacity"],
        "headroom": observed["headroom"],
        "idempotency_key": f"capacity:{digest}",
    }
