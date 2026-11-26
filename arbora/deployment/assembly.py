"""Assemble the deployment steps the engine chose (ARBORA-DEPLOY-R017.2, .5).

Where to run is the engine's answer (:mod:`arbora.deployment.placement`). The
ordered fleet-tool steps are an agent graph the engine assembles from the
tenant's published deployment template. Arbora sends the request and returns
the engine's graph and certificate unmodified; an abstention passes through
naming the engine's reason.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

from arbora.api.invoke.pipeline import OperationRefused

#: The template key of one target in the tenant Agent Library.
TEMPLATE_PREFIX = "deployment:"

Send = Callable[[dict[str, Any]], Awaitable[Any]]

UnavailableCause = Literal["decide", "placement_kind", "confirm"]

_UNAVAILABLE: dict[str, tuple[str, str]] = {
    "decide": (
        "ENGINE_DECIDE_UNAVAILABLE",
        "the engine is built without the decide feature",
    ),
    "placement_kind": (
        "PLACEMENT_KIND_UNAVAILABLE",
        "the engine does not serve the placement question kind",
    ),
    "confirm": (
        "CONFIRM_UNAVAILABLE",
        "the plan confirmation service is not bound",
    ),
}


def refuse_unavailable(cause: UnavailableCause) -> OperationRefused:
    """The refusal for one missing producer; each has no side effect."""
    try:
        code, reason = _UNAVAILABLE[cause]
    except KeyError:
        raise ValueError(f"unknown unavailable cause: {cause}") from None
    return OperationRefused("UNAVAILABLE", {"code": code, "reason": reason})


async def assemble_deployment(
    send: Send,
    *,
    tenant: str,
    target: str,
    components: Sequence[str],
) -> Mapping[str, Any]:
    """Send the chosen template and tool components to ``AgentAssemble``.

    ``send`` is the served ``eg.storage.AgentAssemble`` operation bound to the
    caller's engine client. The reply (graph and certificate, or an abstention
    record) is returned as the engine sent it; the request writes nothing.
    """
    request = {
        "tenant_id": tenant,
        "template_key": f"{TEMPLATE_PREFIX}{target}",
        "requirements": {"capabilities": list(components)},
    }
    result = await send({"request": request})
    if not isinstance(result, Mapping):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the engine returned a malformed assembly"}
        )
    return result
