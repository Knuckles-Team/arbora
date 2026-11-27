"""ARBORA-HOST-R007.2.39: the gateway daemon keeps no local task queue."""

from __future__ import annotations

import pytest

from arbora.gateway import daemon


@pytest.mark.spec("ARBORA-HOST-R007.2.39")
def test_daemon_has_no_local_task_queue_purge() -> None:
    assert not hasattr(daemon, "drain_task_queue")
