"""Default skill-audit and workflow-admission policies (ARBORA-FLEET-R034.2).

The skill policy scans the skill text and its declared tools. It fails a
skill whose ``high`` findings exceed ``admission.skill_max_high_findings``
(default 0, bounds [0, 50], unit findings). The workflow policy requires an
owner, an inputs schema, data flows and a tool inventory. Each default is
replaceable by name through ``admission.skill_policy`` and
``admission.workflow_policy``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from arbora.fleet.admission_models import (
    AdmissionRefused,
    AuditFinding,
    SkillAuditReport,
    WorkflowAdmissionRecord,
    parse_workflow_record,
)

__all__ = [
    "AdmissionConfig",
    "SKILL_POLICIES",
    "WORKFLOW_POLICIES",
    "default_skill_audit_policy",
    "default_workflow_admission_policy",
]

SKILL_MAX_HIGH_KEY = "admission.skill_max_high_findings"
SKILL_POLICY_KEY = "admission.skill_policy"
WORKFLOW_POLICY_KEY = "admission.workflow_policy"
DEFAULT_SKILL_POLICY = "default_skill_audit_policy"
DEFAULT_WORKFLOW_POLICY = "default_workflow_admission_policy"

_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "prompt-override",
        "high",
        re.compile(r"ignore (all )?(previous|prior) instructions", re.I),
    ),
    ("remote-shell-pipe", "high", re.compile(r"(curl|wget)[^\n|]*\|\s*(ba)?sh", re.I)),
    ("recursive-delete", "high", re.compile(r"\brm\s+-rf\s+/", re.I)),
    (
        "credential-exfiltration",
        "high",
        re.compile(r"(exfiltrate|send)[^\n]{0,40}(token|password|secret)", re.I),
    ),
    ("privilege-escalation", "medium", re.compile(r"\bsudo\b", re.I)),
    ("unpinned-download", "low", re.compile(r"https?://\S+\.(sh|exe)\b", re.I)),
)

SkillPolicy = Callable[[str, str, tuple[str, ...], int], SkillAuditReport]
WorkflowPolicy = Callable[[Mapping[str, Any]], WorkflowAdmissionRecord]


def default_skill_audit_policy(
    skill_id: str, text: str, tools: tuple[str, ...], limit: int
) -> SkillAuditReport:
    """Scan the skill text and declared tools; the caller compares the limit."""
    findings: list[AuditFinding] = []
    for rule, severity, pattern in _RULES:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(
                AuditFinding(rule=rule, severity=severity, location=f"text:{line}")
            )
    for tool in tools:
        if tool.strip() in {"*", ""}:
            findings.append(
                AuditFinding(
                    rule="wildcard-tool", severity="high", location=f"tool:{tool!r}"
                )
            )
    return SkillAuditReport(
        skill_id=skill_id,
        policy=DEFAULT_SKILL_POLICY,
        limit=limit,
        findings=tuple(findings),
    )


def default_workflow_admission_policy(
    declaration: Mapping[str, Any],
) -> WorkflowAdmissionRecord:
    """Require owner, inputs schema, data flows and tool inventory."""
    return parse_workflow_record(declaration)


SKILL_POLICIES: dict[str, SkillPolicy] = {
    DEFAULT_SKILL_POLICY: default_skill_audit_policy
}
WORKFLOW_POLICIES: dict[str, WorkflowPolicy] = {
    DEFAULT_WORKFLOW_POLICY: default_workflow_admission_policy
}


@dataclass(frozen=True)
class AdmissionConfig:
    skill_max_high_findings: int = 0
    skill_policy: str = DEFAULT_SKILL_POLICY
    workflow_policy: str = DEFAULT_WORKFLOW_POLICY

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> AdmissionConfig:
        raw = document.get(SKILL_MAX_HIGH_KEY, 0)
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            raise AdmissionRefused(
                "ADMISSION_CONFIG_INVALID", {"key": SKILL_MAX_HIGH_KEY}
            ) from None
        if not 0 <= limit <= 50:
            raise AdmissionRefused(
                "ADMISSION_CONFIG_INVALID", {"key": SKILL_MAX_HIGH_KEY}
            )
        skill = str(document.get(SKILL_POLICY_KEY, DEFAULT_SKILL_POLICY))
        workflow = str(document.get(WORKFLOW_POLICY_KEY, DEFAULT_WORKFLOW_POLICY))
        if skill not in SKILL_POLICIES:
            raise AdmissionRefused(
                "ADMISSION_CONFIG_INVALID", {"key": SKILL_POLICY_KEY}
            )
        if workflow not in WORKFLOW_POLICIES:
            raise AdmissionRefused(
                "ADMISSION_CONFIG_INVALID", {"key": WORKFLOW_POLICY_KEY}
            )
        return cls(limit, skill, workflow)
