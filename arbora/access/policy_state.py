"""The host's policy state: the ``policy`` field of ``HostContext`` (ARBORA-HOST-R039.3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from arbora.access.action_policy import ActionPolicies
from arbora.access.approval import ApprovalDecisions

__all__ = ["PolicyState"]


@dataclass(frozen=True, slots=True)
class PolicyState:
    """Every policy object one host owns, reached by argument."""

    action_policies: ActionPolicies = field(default_factory=ActionPolicies)
    approvals: ApprovalDecisions = field(default_factory=ApprovalDecisions)
