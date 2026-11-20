"""Ensure the shared pipelines repository is always referenced at ``main``."""

from __future__ import annotations

import re
from pathlib import Path

_PIPELINES_REPO = "https://github.com/Knuckles-Team/pipelines"
_PIPELINES_USE = "Knuckles-Team/pipelines"
_REV = re.compile(r"^\s*rev:\s*(?P<ref>[^\s#]+)")
_USES = re.compile(
    rf"^\s*uses:\s*{re.escape(_PIPELINES_USE)}(?:/[^@\s]+)?@(?P<ref>[^\s#]+)"
)


def reference_pins(root: Path) -> list[str]:
    """Return references to the shared pipeline source that are not ``main``."""
    findings: list[str] = []
    pre_commit = root / ".pre-commit-config.yaml"
    if pre_commit.is_file():
        findings.extend(_pre_commit_findings(pre_commit, root))

    workflow_root = root / ".github" / "workflows"
    if workflow_root.is_dir():
        for workflow in sorted(workflow_root.glob("*.yml")):
            findings.extend(_workflow_findings(workflow, root))
        for workflow in sorted(workflow_root.glob("*.yaml")):
            findings.extend(_workflow_findings(workflow, root))
    return findings


def _pre_commit_findings(path: Path, root: Path) -> list[str]:
    findings: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if _PIPELINES_REPO not in line:
            continue
        found_ref = False
        for offset, candidate in enumerate(lines[index + 1 :], start=index + 2):
            if candidate.lstrip().startswith("- repo:"):
                break
            match = _REV.match(candidate)
            if match:
                found_ref = True
                if match.group("ref") != "main":
                    findings.append(_finding(path, root, offset, match.group("ref")))
                break
        if not found_ref:
            findings.append(_finding(path, root, index + 1, "missing rev"))
    return findings


def _workflow_findings(path: Path, root: Path) -> list[str]:
    findings: list[str] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        match = _USES.match(line)
        if match and match.group("ref") != "main":
            findings.append(_finding(path, root, line_number, match.group("ref")))
    return findings


def _finding(path: Path, root: Path, line: int, ref: str) -> str:
    return f"PIPELINES_REF_PINNED {path.relative_to(root)}:{line} ({ref})"
