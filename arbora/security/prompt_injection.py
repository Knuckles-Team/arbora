"""Pattern-based prompt and command injection scanner for served intents.

The scanner matches untrusted text against a fixed set of threat patterns
(reverse shells, exfiltration, destructive commands, privilege escalation,
credential access, reconnaissance, injection and jailbreak templates). A
finding is malicious when its confidence reaches the threshold, read from
``SECURITY_PROMPT_THRESHOLD`` (default 0.8).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from arbora.security.settings import setting

__all__ = ["PromptInjectionScanner", "RiskLevel", "ScanResult", "ThreatPattern"]


class RiskLevel(StrEnum):
    """Threat severity classification."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def confidence_score(self) -> float:
        """Map risk level to a numeric confidence score."""
        return {
            RiskLevel.LOW: 0.3,
            RiskLevel.MEDIUM: 0.6,
            RiskLevel.HIGH: 0.8,
            RiskLevel.CRITICAL: 0.95,
        }[self]


# ---------------------------------------------------------------------------
# Threat patterns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreatPattern:
    """A single threat detection pattern.

    Attributes:
        name: Human-readable pattern identifier.
        regex: Compiled regular expression for matching.
        risk_level: Severity of the matched threat.
        description: Explanation shown to the user.
    """

    name: str
    regex: re.Pattern[str]
    risk_level: RiskLevel
    description: str


@dataclass(frozen=True)
class PatternMatch:
    """Result of a single pattern match against input text."""

    pattern_name: str
    matched_text: str
    risk_level: RiskLevel
    description: str
    confidence: float


# -- Shell injection / reverse shell patterns --------------------------------
_REVERSE_SHELL_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="netcat_reverse_shell",
        regex=re.compile(r"nc\s+.*-e\s+(/bin/(ba)?sh|cmd)", re.IGNORECASE),
        risk_level=RiskLevel.CRITICAL,
        description="Netcat reverse shell detected",
    ),
    ThreatPattern(
        name="bash_reverse_shell",
        regex=re.compile(r"bash\s+-i\s+>&?\s*/dev/tcp/", re.IGNORECASE),
        risk_level=RiskLevel.CRITICAL,
        description="Bash /dev/tcp reverse shell detected",
    ),
    ThreatPattern(
        name="python_reverse_shell",
        regex=re.compile(
            r"python[23]?\s+.*socket.*connect\s*\(", re.IGNORECASE | re.DOTALL
        ),
        risk_level=RiskLevel.CRITICAL,
        description="Python socket reverse shell detected",
    ),
    ThreatPattern(
        name="perl_reverse_shell",
        regex=re.compile(r"perl\s+.*socket\s*\(.*INET", re.IGNORECASE | re.DOTALL),
        risk_level=RiskLevel.CRITICAL,
        description="Perl socket reverse shell detected",
    ),
]

# -- Data exfiltration patterns ---------------------------------------------
_EXFIL_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="curl_pipe_shell",
        regex=re.compile(r"curl\s+.*\|\s*(ba)?sh", re.IGNORECASE),
        risk_level=RiskLevel.CRITICAL,
        description="Remote code execution via curl | bash",
    ),
    ThreatPattern(
        name="wget_pipe_shell",
        regex=re.compile(r"wget\s+.*-O\s*-\s*\|\s*(ba)?sh", re.IGNORECASE),
        risk_level=RiskLevel.CRITICAL,
        description="Remote code execution via wget | bash",
    ),
    ThreatPattern(
        name="curl_data_exfil",
        regex=re.compile(r"curl\s+.*-d\s+.*(\$\(|`)", re.IGNORECASE),
        risk_level=RiskLevel.HIGH,
        description="Data exfiltration via curl POST with command substitution",
    ),
    ThreatPattern(
        name="base64_decode_exec",
        regex=re.compile(
            r"(echo|printf)\s+.*\|\s*base64\s+(-d|--decode)\s*\|\s*(ba)?sh",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.CRITICAL,
        description="Encoded payload execution via base64 decode",
    ),
]

# -- Destructive commands ---------------------------------------------------
_DESTRUCTIVE_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="rm_rf_root",
        regex=re.compile(
            r"rm\s+(-[rRf]+\s+)+/\s*$|rm\s+(-[rRf]+\s+)+/[^a-zA-Z]", re.MULTILINE
        ),
        risk_level=RiskLevel.CRITICAL,
        description="Recursive deletion of root filesystem",
    ),
    ThreatPattern(
        name="dd_dev_zero",
        regex=re.compile(
            r"dd\s+.*if=/dev/(zero|urandom)\s+.*of=/dev/[sh]d", re.IGNORECASE
        ),
        risk_level=RiskLevel.CRITICAL,
        description="Disk wipe via dd",
    ),
    ThreatPattern(
        name="mkfs_disk",
        regex=re.compile(r"mkfs\.\w+\s+/dev/[sh]d", re.IGNORECASE),
        risk_level=RiskLevel.CRITICAL,
        description="Filesystem format on physical disk",
    ),
    ThreatPattern(
        name="chmod_world_writable",
        regex=re.compile(r"chmod\s+(-R\s+)?777\s+/", re.IGNORECASE),
        risk_level=RiskLevel.HIGH,
        description="World-writable permissions on root",
    ),
]

# -- Privilege escalation ---------------------------------------------------
_PRIVESC_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="sudo_nopasswd",
        regex=re.compile(r"echo\s+.*NOPASSWD.*>>\s*/etc/sudoers", re.IGNORECASE),
        risk_level=RiskLevel.CRITICAL,
        description="Passwordless sudo injection",
    ),
    ThreatPattern(
        name="passwd_modification",
        regex=re.compile(r"echo\s+.*>>\s*/etc/(passwd|shadow)", re.IGNORECASE),
        risk_level=RiskLevel.CRITICAL,
        description="Direct /etc/passwd or /etc/shadow modification",
    ),
    ThreatPattern(
        name="setuid_binary",
        regex=re.compile(r"chmod\s+[ugo]*s\s+", re.IGNORECASE),
        risk_level=RiskLevel.HIGH,
        description="SetUID/SetGID bit manipulation",
    ),
]

# -- Environment / credential harvesting ------------------------------------
_CREDENTIAL_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="env_dump",
        regex=re.compile(r"\benv\b|\bprintenv\b|cat\s+.*/\.env\b", re.IGNORECASE),
        risk_level=RiskLevel.MEDIUM,
        description="Environment variable dump (potential credential exposure)",
    ),
    ThreatPattern(
        name="ssh_key_exfil",
        regex=re.compile(
            r"cat\s+.*/.ssh/(id_rsa|id_ed25519|authorized_keys)", re.IGNORECASE
        ),
        risk_level=RiskLevel.HIGH,
        description="SSH key file access",
    ),
    ThreatPattern(
        name="history_access",
        regex=re.compile(r"cat\s+.*/(\.bash_history|\.zsh_history)", re.IGNORECASE),
        risk_level=RiskLevel.MEDIUM,
        description="Shell history file access",
    ),
    ThreatPattern(
        name="aws_credentials",
        regex=re.compile(r"cat\s+.*/.aws/credentials", re.IGNORECASE),
        risk_level=RiskLevel.HIGH,
        description="AWS credentials file access",
    ),
]

# -- Network reconnaissance ------------------------------------------------
_RECON_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="port_scan",
        regex=re.compile(r"nmap\s+|masscan\s+", re.IGNORECASE),
        risk_level=RiskLevel.MEDIUM,
        description="Network port scanning tool execution",
    ),
    ThreatPattern(
        name="dns_exfil",
        regex=re.compile(r"dig\s+.*\$\(|nslookup\s+.*\$\(", re.IGNORECASE),
        risk_level=RiskLevel.HIGH,
        description="DNS-based data exfiltration via command substitution",
    ),
]

# -- Prompt injection markers -----------------------------------------------
_INJECTION_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="ignore_instructions",
        regex=re.compile(
            r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|guidelines)",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.HIGH,
        description="Prompt injection: instruction override attempt",
    ),
    ThreatPattern(
        name="new_persona",
        regex=re.compile(
            r"you\s+are\s+now\s+|from\s+now\s+on\s+you\s+are|act\s+as\s+if\s+you\s+are",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.MEDIUM,
        description="Prompt injection: persona override attempt",
    ),
    ThreatPattern(
        name="unicode_tags",
        regex=re.compile(r"[\U000E0020-\U000E007F]"),
        risk_level=RiskLevel.HIGH,
        description="Unicode tag characters detected (steganographic injection)",
    ),
]

# -- Jailbreak attack patterns (SoK-derived, CONCEPT:AU-OS.safety.prompt-injection-scanner) ---------------
# Derived from: SoK: Robustness in LLMs against Jailbreak Attacks
# (arXiv:2605.05058v1, Score 16.2)
#
# Four-category taxonomy:
# 1. Template-based: DAN, AIM, STAN, Grandma, UCAR
# 2. Optimization-based: GCG suffix, token perturbation
# 3. LLM-based: multi-turn escalation, context confusion
# 4. Manual: role-play, hypothetical, authority


class JailbreakCategory(StrEnum):
    """Jailbreak attack categories from SoK taxonomy (CONCEPT:AU-OS.safety.prompt-injection-scanner)."""

    TEMPLATE_BASED = "template_based"
    OPTIMIZATION_BASED = "optimization_based"
    LLM_BASED = "llm_based"
    MANUAL = "manual"


_JAILBREAK_TEMPLATE_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="dan_jailbreak",
        regex=re.compile(
            r"(\bDAN\b|do\s+anything\s+now|developer\s+mode|jailbreak\s+mode)",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.HIGH,
        description="Template-based jailbreak: DAN/Developer Mode prompt",
    ),
    ThreatPattern(
        name="aim_jailbreak",
        regex=re.compile(
            r"(\bAIM\b|always\s+intelligent\s+and\s+machiavellian)",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.HIGH,
        description="Template-based jailbreak: AIM persona",
    ),
    ThreatPattern(
        name="ucar_jailbreak",
        regex=re.compile(
            r"(\bUCAR\b|uncondition|no\s+filter|without\s+restrictions|no\s+ethical)",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.HIGH,
        description="Template-based jailbreak: unrestricted mode",
    ),
    ThreatPattern(
        name="grandma_jailbreak",
        regex=re.compile(
            r"(my\s+(late\s+)?grandmother|deceased\s+relative).*used\s+to\s+(tell|read|say)",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.MEDIUM,
        description="Template-based jailbreak: Grandma exploit",
    ),
]

_JAILBREAK_OPTIMIZATION_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="gcg_suffix",
        regex=re.compile(
            r"[!@#$%^&*]{5,}|[\x80-\xff]{10,}|" r"(?:[A-Z]{2,3}\s+){5,}",
        ),
        risk_level=RiskLevel.MEDIUM,
        description="Optimization-based jailbreak: potential GCG adversarial suffix",
    ),
    ThreatPattern(
        name="token_smuggling",
        regex=re.compile(
            r"(?:&#\d{2,4};){3,}|(?:\\u[0-9a-fA-F]{4}){3,}|" r"(?:%[0-9a-fA-F]{2}){5,}",
        ),
        risk_level=RiskLevel.HIGH,
        description="Optimization-based jailbreak: token smuggling via encoding",
    ),
]

_JAILBREAK_LLM_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="context_confusion",
        regex=re.compile(
            r"(end\s+of\s+system\s+prompt|system\s+message\s+ends|"
            r"\[/INST\]|\[/SYS\]|<</SYS>>)",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.HIGH,
        description="LLM-based jailbreak: context boundary confusion",
    ),
    ThreatPattern(
        name="multi_turn_escalation",
        regex=re.compile(
            r"(now\s+that\s+we\'ve\s+established|building\s+on\s+your\s+previous|"
            r"since\s+you\s+already\s+agreed|you\s+said\s+earlier\s+that)",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.MEDIUM,
        description="LLM-based jailbreak: multi-turn escalation",
    ),
]

_JAILBREAK_MANUAL_PATTERNS: list[ThreatPattern] = [
    ThreatPattern(
        name="roleplay_jailbreak",
        regex=re.compile(
            r"(pretend\s+you\s+are|let\'s\s+play\s+a\s+game|imagine\s+you\s+are|"
            r"for\s+educational\s+purposes|in\s+a\s+hypothetical\s+scenario|"
            r"write\s+a\s+story\s+where\s+you)",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.MEDIUM,
        description="Manual jailbreak: role-play/hypothetical framing",
    ),
    ThreatPattern(
        name="authority_override",
        regex=re.compile(
            r"(as\s+your\s+creator|i\s+am\s+your\s+developer|"
            r"openai\s+internal|admin\s+override|sudo\s+mode)",
            re.IGNORECASE,
        ),
        risk_level=RiskLevel.HIGH,
        description="Manual jailbreak: false authority claim",
    ),
]

# Composite pattern list
ALL_PATTERNS: list[ThreatPattern] = (
    _REVERSE_SHELL_PATTERNS
    + _EXFIL_PATTERNS
    + _DESTRUCTIVE_PATTERNS
    + _PRIVESC_PATTERNS
    + _CREDENTIAL_PATTERNS
    + _RECON_PATTERNS
    + _INJECTION_PATTERNS
    + _JAILBREAK_TEMPLATE_PATTERNS
    + _JAILBREAK_OPTIMIZATION_PATTERNS
    + _JAILBREAK_LLM_PATTERNS
    + _JAILBREAK_MANUAL_PATTERNS
)


class ScanResult(BaseModel):
    """Result of scanning text for prompt injection threats.

    Attributes:
        is_malicious: Whether the confidence exceeds the configured threshold.
        confidence: Maximum confidence score across all matched patterns.
        explanation: Human-readable description of the finding.
        scanned: Whether scanning was actually performed.
        matches: All individual pattern matches found.
        finding_id: Unique identifier for this finding (SEC-<uuid>).
    """

    is_malicious: bool = False
    confidence: float = 0.0
    explanation: str = "No security threats detected"
    scanned: bool = True
    matches: list[dict[str, Any]] = Field(default_factory=list)
    finding_id: str = Field(default_factory=lambda: f"SEC-{uuid.uuid4().hex}")


# ---------------------------------------------------------------------------
# KG Model — SecurityFindingNode
# ---------------------------------------------------------------------------


class PromptInjectionScanner:
    """Scan text against the threat patterns and gate on the confidence threshold."""

    def __init__(
        self,
        patterns: list[ThreatPattern] | None = None,
        threshold: float | None = None,
    ) -> None:
        self.patterns = patterns or ALL_PATTERNS
        self._threshold = threshold

    @property
    def threshold(self) -> float:
        """Confidence threshold above which findings are flagged malicious."""
        if self._threshold is not None:
            return self._threshold
        try:
            return float(setting("SECURITY_PROMPT_THRESHOLD", "0.8"))
        except (ValueError, TypeError):
            return 0.8

    def scan_text(self, text: str) -> ScanResult:
        """Scan arbitrary text for threat patterns.

        Args:
            text: The text to scan (command, message, etc.).

        Returns:
            ScanResult with matches and confidence.
        """
        if not text or not text.strip():
            return ScanResult(scanned=False, explanation="Empty input")

        matches: list[PatternMatch] = []
        for pattern in self.patterns:
            for m in pattern.regex.finditer(text):
                matches.append(
                    PatternMatch(
                        pattern_name=pattern.name,
                        matched_text=m.group()[:100],  # Truncate for safety
                        risk_level=pattern.risk_level,
                        description=pattern.description,
                        confidence=pattern.risk_level.confidence_score(),
                    )
                )

        if not matches:
            return ScanResult()

        max_confidence = max(m.confidence for m in matches)
        top_match = max(matches, key=lambda m: m.confidence)
        is_malicious = max_confidence >= self.threshold

        match_dicts = [
            {
                "pattern_name": m.pattern_name,
                "matched_text": m.matched_text,
                "risk_level": m.risk_level.value,
                "confidence": m.confidence,
                "description": m.description,
            }
            for m in matches
        ]

        explanation = (
            f"Pattern-based detection: {top_match.description} "
            f"(Risk: {top_match.risk_level.value}, "
            f"Confidence: {max_confidence:.1%})"
        )

        if not is_malicious:
            explanation = (
                f"Finding below threshold ({self.threshold:.1%}): "
                f"{top_match.description}"
            )

        return ScanResult(
            is_malicious=is_malicious,
            confidence=max_confidence,
            explanation=explanation,
            matches=match_dicts,
        )
