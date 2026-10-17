"""Admission screening and persistence redaction for agent task text."""

from __future__ import annotations

from agent_utilities.security.guardrails import PiiSanitizer
from agent_utilities.security.threat_defense_engine import PromptInjectionScanner


class TaskInjectionRejected(ValueError):
    """The task text did not pass the prompt-injection admission policy."""


def screen_and_redact_agent_task(
    task: str, *, scanner: PromptInjectionScanner | None = None
) -> str:
    """Reject injected task text, then return the PII-redacted durable form."""
    verdict = (scanner or PromptInjectionScanner()).scan_text(task)
    if verdict.is_malicious:
        raise TaskInjectionRejected("task rejected by the input screening policy")
    return PiiSanitizer().sanitize_text(task)
