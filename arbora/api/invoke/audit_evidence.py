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
CallerBuilder = Callable[[Mapping[str, Any], Any, Any], dict[str, str]]

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


def _retrieval_space(arguments: Mapping[str, Any], result: Any) -> dict[str, str]:
    """Counts only: the queries and the passages never enter the audit record."""
    value = _value(result)
    counts = value.get("hit_counts", [])
    return {
        "query_count": str(value.get("query_count", 0)),
        "boundary_size": str(value.get("boundary_size", 0)),
        "hit_counts": ",".join(str(n) for n in counts)
        if isinstance(counts, list)
        else "",
        "context_budget": str(value.get("context_budget", 0)),
    }


def _research_web(arguments: Mapping[str, Any], result: Any) -> dict[str, str]:
    """Identifiers and counts only: never page text, titles or snippets (R067.6)."""
    value = _value(result)
    documents = [d for d in value.get("documents", []) if isinstance(d, Mapping)]
    subject = str(arguments.get("query") or " ".join(arguments.get("urls") or ()))
    return {
        "query_digest": hashlib.sha256(subject.encode()).hexdigest(),
        "decision_id": str(value.get("decision_id") or ""),
        "work_item_id": str(value.get("work_item_id") or ""),
        "backends": ",".join(str(d.get("backend", "")) for d in documents),
        "fallback_count": str(value.get("fallbacks", 0)),
        "document_ids": ",".join(str(d.get("document_id", "")) for d in documents),
    }


def _digest_of(values: Any) -> str:
    return hashlib.sha256(json.dumps(sorted(values)).encode()).hexdigest()


def _projections(
    arguments: Mapping[str, Any], result: Any, caller: Any
) -> dict[str, str]:
    """The caller's scope and the returned projections, as digests only (R052.5)."""
    listed = _value(result).get("projections", [])
    references = [
        str(p.get("mapping_ref", "")) for p in listed if isinstance(p, Mapping)
    ]
    return {
        "caller_scope_digest": _digest_of(
            getattr(caller, "effective_scopes", ()) or ()
        ),
        "projection_refs_digest": _digest_of(references),
        "projection_count": str(len(references)),
    }


def _plain(builder: Builder) -> CallerBuilder:
    """A builder that reads no caller, in the one dispatch shape."""
    return lambda arguments, result, caller: builder(arguments, result)


_BUILDERS: dict[str, CallerBuilder] = {
    **dict.fromkeys(_POLICY_OPS, _plain(_policy_change)),
    "plans.run": _plain(_refused_plan),
    "security.evaluate": _plain(_security_evaluate),
    "retrieval.space": _plain(_retrieval_space),
    "research.web": _plain(_research_web),
    "query.projections": _projections,
}


def result_evidence(
    op_id: str, arguments: Mapping[str, Any], result: Any, caller: Any = None
) -> dict[str, str]:
    """The audit fields ``op_id`` names for this outcome; empty for other operations."""
    builder = _BUILDERS.get(op_id)
    return builder(arguments, result, caller) if builder is not None else {}
