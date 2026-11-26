"""Screen untrusted content and redact secrets at the invoke chokepoint.

ARBORA-OPS-R042. Pure functions: the default rules and the default output
redactor are registered by name in one :class:`ScreenRegistry`; the invoke
pipeline selects them from settings. A finding carries a class, a span and a
rule id, never the matched text, so evidence cannot leak what it describes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.registry.spec import ContentOrigin

__all__ = [
    "ContentOrigin",
    "ContentScreenVerdict",
    "AttackClass",
    "ScreenFinding",
    "ScreenOutcome",
    "ScreenRule",
    "ScreenRegistry",
    "SCREEN_REGISTRY",
    "QUARANTINE_CLASSES",
    "DEFAULT_RULES",
    "DEFAULT_REDACTOR",
    "default_screen_rules",
    "default_output_redactor",
    "screen_result",
    "screen_params",
    "redact_result",
    "screening_fields",
]


class ContentScreenVerdict(StrEnum):
    CLEAN = "clean"
    FLAGGED = "flagged"
    QUARANTINED = "quarantined"


class AttackClass(StrEnum):
    PERCEPTION = "perception"
    REASONING = "reasoning"
    MEMORY = "memory"
    ACTION = "action"
    MULTI_AGENT = "multi_agent"
    OVERSEER = "overseer"


#: Findings in these classes quarantine the content; this set is ``Fixed``.
QUARANTINE_CLASSES: frozenset[AttackClass] = frozenset(
    {
        AttackClass.MEMORY,
        AttackClass.ACTION,
        AttackClass.MULTI_AGENT,
        AttackClass.OVERSEER,
    }
)


class ScreenFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attack_class: AttackClass
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    rule_id: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ScreenRule:
    rule_id: str
    attack_class: AttackClass
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class ScreenOutcome:
    """The verdict, the findings and the value with flagged spans removed."""

    verdict: ContentScreenVerdict
    findings: tuple[ScreenFinding, ...]
    value: Any

    @property
    def finding_classes(self) -> tuple[str, ...]:
        return tuple(sorted({f.attack_class.value for f in self.findings}))

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(sorted({f.rule_id for f in self.findings}))


_FLAGS = re.IGNORECASE | re.DOTALL


def _rule(rule_id: str, attack_class: AttackClass, pattern: str) -> ScreenRule:
    return ScreenRule(rule_id, attack_class, re.compile(pattern, _FLAGS))


_HIDDEN_STYLE = r"(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0)"

#: One default rule per attack class. Hidden text, rendered text instructions
#: and markup-embedded instructions all match through these patterns.
DEFAULT_RULES: tuple[ScreenRule, ...] = (
    _rule(
        "screen.perception.hidden_text",
        AttackClass.PERCEPTION,
        r"[\u200b-\u200f\u2060\ufeff]+"
        r"|<(\w+)\b[^>]*style\s*=\s*[\"'][^\"']*"
        + _HIDDEN_STYLE
        + r"[^\"']*[\"'][^>]*>"
        r".*?</\1\s*>",
    ),
    _rule(
        "screen.reasoning.override",
        AttackClass.REASONING,
        r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions"
        r"|disregard\s+(?:all\s+|the\s+)?(?:previous|prior|above|system)\s+"
        r"(?:instructions|prompt)"
        r"|<!--.*?(?:instruction|ignore|assistant).*?-->",
    ),
    _rule(
        "screen.memory.implant",
        AttackClass.MEMORY,
        r"(?:remember|store|save|write)\s+(?:this|the following)\s+"
        r"(?:in|to|into)\s+(?:your\s+)?(?:long[- ]term\s+)?memory"
        r"|add\s+(?:this|the following)\s+to\s+your\s+(?:long[- ]term\s+)?memory",
    ),
    _rule(
        "screen.action.exfiltrate",
        AttackClass.ACTION,
        r"(?:send|post|upload|forward)\s+(?:the\s+|all\s+|your\s+)?"
        r"(?:files?|data|secrets?|credentials?|keys?|tokens?)\s+to\s+https?://"
        r"|(?:execute|run)\s+the\s+following\s+(?:shell\s+)?command"
        r"|call\s+the\s+\w+\s+tool\s+with",
    ),
    _rule(
        "screen.multi_agent.propagate",
        AttackClass.MULTI_AGENT,
        r"(?:tell|instruct|forward\s+this\s+to)\s+(?:all\s+|the\s+)?"
        r"(?:other\s+|sub-?)?agents?\b"
        r"|pass\s+these\s+instructions\s+to\s+(?:every|all)\s+agents?",
    ),
    _rule(
        "screen.overseer.approval",
        AttackClass.OVERSEER,
        r"(?:tell|inform|notify)\s+the\s+(?:reviewer|overseer|supervisor|judge)\s+"
        r"(?:that\s+)?(?:this|it)\s+(?:is|was)\s+(?:safe|approved|compliant)"
        r"|mark\s+this\s+(?:output|response)\s+as\s+(?:safe|approved)",
    ),
)


def default_screen_rules() -> tuple[ScreenRule, ...]:
    return DEFAULT_RULES


_REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(r"password|secret|token", re.IGNORECASE)
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
)

Redactor = Callable[[Any], tuple[Any, int]]


def default_output_redactor(value: Any) -> tuple[Any, int]:
    """Mask secrets in a JSON-shaped value; return it with the mask count."""
    if isinstance(value, str):
        count = 0
        text = value
        for pattern in _SECRET_PATTERNS:
            text, hits = pattern.subn(_REDACTED, text)
            count += hits
        return text, count
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        total = 0
        for key, child in value.items():
            if (
                isinstance(key, str)
                and _SECRET_KEY.search(key)
                and child not in (None, "")
            ):
                out[key] = _REDACTED
                total += 1
                continue
            out[key], hits = default_output_redactor(child)
            total += hits
        return out, total
    if isinstance(value, (list, tuple)):
        items = [default_output_redactor(child) for child in value]
        return [item for item, _ in items], sum(hits for _, hits in items)
    return value, 0


class ScreenRegistry:
    """Rule sets and redactors selected by name."""

    def __init__(self) -> None:
        self._rules: dict[str, Callable[[], Sequence[ScreenRule]]] = {}
        self._redactors: dict[str, Redactor] = {}

    def register_rules(
        self, name: str, rules: Callable[[], Sequence[ScreenRule]]
    ) -> None:
        self._rules[name] = rules

    def register_redactor(self, name: str, redactor: Redactor) -> None:
        self._redactors[name] = redactor

    def rules(self, name: str) -> Sequence[ScreenRule]:
        return self._rules[name]()

    def redactor(self, name: str) -> Redactor:
        return self._redactors[name]


DEFAULT_RULES_NAME = "default_screen_rules"
DEFAULT_REDACTOR = "default_output_redactor"
SCREEN_REGISTRY = ScreenRegistry()
SCREEN_REGISTRY.register_rules(DEFAULT_RULES_NAME, default_screen_rules)
SCREEN_REGISTRY.register_redactor(DEFAULT_REDACTOR, default_output_redactor)


def _settings() -> tuple[str, str, int]:
    from arbora.core.settings import runtime_config

    rules = str(getattr(runtime_config, "content_screen_rules", DEFAULT_RULES_NAME))
    redactor = str(getattr(runtime_config, "content_screen_redactor", DEFAULT_REDACTOR))
    limit = getattr(runtime_config, "content_screen_max_scan_bytes", 1_048_576)
    return rules, redactor, int(limit)


def _scan_string(
    text: str, rules: Sequence[ScreenRule]
) -> tuple[str, list[ScreenFinding]]:
    findings: list[ScreenFinding] = []
    removals: list[tuple[int, int]] = []
    for rule in rules:
        for match in rule.pattern.finditer(text):
            findings.append(
                ScreenFinding(
                    attack_class=rule.attack_class,
                    start=match.start(),
                    end=match.end(),
                    rule_id=rule.rule_id,
                )
            )
            if rule.attack_class not in QUARANTINE_CLASSES:
                removals.append((match.start(), match.end()))
    cleaned, cursor = [], 0
    for start, end in sorted(removals):
        if start < cursor:
            cursor = max(cursor, end)
            continue
        cleaned.append(text[cursor:start])
        cursor = end
    cleaned.append(text[cursor:])
    return "".join(cleaned), findings


def _walk(value: Any, rules: Sequence[ScreenRule], found: list[ScreenFinding]) -> Any:
    if isinstance(value, str):
        cleaned, findings = _scan_string(value, rules)
        found.extend(findings)
        return cleaned
    if isinstance(value, Mapping):
        return {key: _walk(child, rules, found) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_walk(child, rules, found) for child in value]
    return value


def _size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False).encode())
    except (TypeError, ValueError):
        return 1 << 62


def screen_result(value: Any) -> ScreenOutcome:
    """Screen one open-web result. Content above the scan limit is quarantined."""
    rules_name, _, limit = _settings()
    if _size(value) > limit:
        finding = ScreenFinding(
            attack_class=AttackClass.PERCEPTION,
            start=0,
            end=limit,
            rule_id="screen.scan_limit",
        )
        return ScreenOutcome(ContentScreenVerdict.QUARANTINED, (finding,), None)
    found: list[ScreenFinding] = []
    cleaned = _walk(value, SCREEN_REGISTRY.rules(rules_name), found)
    if any(f.attack_class in QUARANTINE_CLASSES for f in found):
        return ScreenOutcome(ContentScreenVerdict.QUARANTINED, tuple(found), None)
    verdict = ContentScreenVerdict.FLAGGED if found else ContentScreenVerdict.CLEAN
    return ScreenOutcome(verdict, tuple(found), cleaned)


def screen_params(params: Mapping[str, Any]) -> ScreenOutcome:
    """Screen request params that carry open-web content before an effect."""
    return screen_result(dict(params))


def redact_result(value: Any) -> tuple[Any, int]:
    """Mask secrets in a result with the configured redactor."""
    _, redactor, _ = _settings()
    return SCREEN_REGISTRY.redactor(redactor)(value)


def screening_fields(outcome: ScreenOutcome | None, redactions: int) -> dict[str, str]:
    """The four evidence fields: identifiers and counts, never matched text."""
    if outcome is None and not redactions:
        return {}
    return {
        "screen_verdict": outcome.verdict.value if outcome else "none",
        "finding_classes": ",".join(outcome.finding_classes) if outcome else "",
        "rule_ids": ",".join(outcome.rule_ids) if outcome else "",
        "redaction_count": str(redactions),
    }
