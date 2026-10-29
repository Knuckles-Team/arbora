"""Shared fixtures for the agent-host log tests."""

from __future__ import annotations

import pytest

from arbora.agent_host import log_sink


@pytest.fixture
def unsynced_log_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take disk-flush latency out of tests that go through request admission.

    The admission middleware gives the required sink write one second, and the
    sink flushes every record to stable storage.  On a CI runner whose disk is
    busy that flush alone can exceed the deadline, so ``/health`` answered 503
    (``log_unavailable``) at random.  Tests using this fixture exercise
    bind/read/refusal behaviour, not durability, so the flush is a no-op; the
    failed-flush path keeps its own test in ``test_log_replay``.
    """

    monkeypatch.setattr(log_sink.os, "fsync", lambda fd: None)
