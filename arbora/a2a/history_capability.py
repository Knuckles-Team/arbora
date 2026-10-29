"""A2A completed-text profile negotiation, never caller execution authority."""

from __future__ import annotations

from inspect import signature
from typing import Any

from .routing import A2AAssemblyUnavailable

HISTORY_EXTENSION = (
    "https://knuckles-team.github.io/arbora/extensions/completed-text-history/v1"
)
EXTENSION_HEADER = "X-A2A-Extensions"


def requested_history(value: str | None) -> bool:
    """Validate bounded activation; unknown owned versions cannot downgrade."""
    if value is None:
        return False
    if len(value) > 4096:
        raise ValueError("extension header too large")
    values = [part.strip() for part in value.split(",")]
    if any(not part for part in values) or len(values) != len(set(values)):
        raise ValueError("invalid extension activation")
    prefix = HISTORY_EXTENSION.rsplit("/", 1)[0] + "/"
    if any(part.startswith(prefix) and part != HISTORY_EXTENSION for part in values):
        raise A2AAssemblyUnavailable("unsupported completed-text history version")
    return HISTORY_EXTENSION in values


def native_history_contract_available(host: Any) -> bool:
    """Inspect actual schema/registry wiring, not graph provisioning or health."""
    from arbora.mcp_server.harness_host import checkpoint_provider_active

    if host is None or not checkpoint_provider_active():
        return False
    try:
        from epistemic_graph.client import WorkItemClient
        from epistemic_graph.generated import coordination, models

        required = {
            "MethodGetRunWorkItemParams": {"completed_text_history", "selection"},
            "MethodClaimWorkItemParams": {"completed_text_history"},
            "RunWorkItemView": {"checkpoint_selection", "completed_checkpoint"},
            "ConversationTurnAdmission": {"history_profile"},
            "RunCompletionInput": {"completed_checkpoint"},
            "NativeClaimWorkItemResult": {"fresh_execution_grant"},
        }
        if any(
            not fields <= getattr(models, name).model_fields.keys()
            for name, fields in required.items()
        ):
            return False
        if any(
            "completed_text_history"
            not in signature(getattr(WorkItemClient, name)).parameters
            for name in ("claim", "get_run")
        ):
            return False
        if not _native_methods_available(coordination):
            return False
        registry = host.services.registry
        return all(
            registry.get(f"eg.coordination.{name}") is not None
            for name in (
                "GetConversationHead",
                "GetRunWorkItem",
                "ClaimWorkItem",
                "SubmitWorkItem",
                "CommitWorkItemResult",
            )
        )
    except (ImportError, AttributeError, TypeError, ValueError):
        return False


def _native_methods_available(coordination: Any) -> bool:
    """Require generated send/decode pairs, not a locally invented method name."""
    for method in (
        "get_conversation_head",
        "get_run_work_item",
        "claim_work_item",
        "submit_work_item",
        "commit_work_item_result",
    ):
        if not all(
            callable(getattr(coordination, f"{kind}_{method}", None))
            for kind in ("send", "decode")
        ):
            return False
    return True
