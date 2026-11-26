"""Operation-specific audit evidence: references, digests and counts, never content.

``result_evidence`` is the one place that turns an operation's arguments and
outcome into the extra audit fields of the operations that name them
(ARBORA-OPS-R050.5, ARBORA-OPS-R051.5). It never reads clause text, probe
payloads or any other body: every value is an identifier, a digest or a count.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

Builder = Callable[[Mapping[str, Any], Any], dict[str, str]]

_POLICY_OPS = frozenset({"policy.create", "policy.update", "policy.retire"})


def _value(result: Any) -> Mapping[str, Any]:
    value = getattr(result, "value", None)
    return value if isinstance(value, Mapping) else {}


def _policy_change(arguments: Mapping[str, Any], result: Any) -> dict[str, str]:
    value = _value(result)
    return {
        "policy_ref": str(value.get("policy_ref", "")),
        "version_digest": str(value.get("version_digest", "")),
        "approval_ref": str(arguments.get("receipt_digest", "")),
        "outcome": str(result.code),
    }


def _refused_plan(arguments: Mapping[str, Any], result: Any) -> dict[str, str]:
    if getattr(result, "code", "") != "POLICY_DENIED":
        return {}
    details = getattr(result, "details", {}) or {}
    return {
        "policy_ref": str(details.get("policy_ref", "")),
        "version_digest": str(details.get("version_digest", "")),
        "approval_ref": "",
        "outcome": "POLICY_DENIED",
    }


def _security_evaluate(arguments: Mapping[str, Any], result: Any) -> dict[str, str]:
    value = _value(result)
    goal = json.dumps(str(arguments.get("goal", ""))).encode()
    findings = value.get("findings", [])
    return {
        "goal_digest": hashlib.sha256(goal).hexdigest(),
        "entry_ids": ",".join(str(i) for i in value.get("entry_ids", [])),
        "scope_refs": ",".join(str(t) for t in arguments.get("target_ids", [])),
        "finding_count": str(len(findings) if isinstance(findings, list) else 0),
    }


_BUILDERS: dict[str, Builder] = {
    **dict.fromkeys(_POLICY_OPS, _policy_change),
    "plans.run": _refused_plan,
    "security.evaluate": _security_evaluate,
}


def result_evidence(
    op_id: str, arguments: Mapping[str, Any], result: Any
) -> dict[str, str]:
    """The audit fields ``op_id`` names for this outcome; empty for other operations."""
    builder = _BUILDERS.get(op_id)
    return builder(arguments, result) if builder is not None else {}
