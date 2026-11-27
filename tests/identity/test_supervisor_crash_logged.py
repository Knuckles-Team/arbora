"""The identity client-contract supervisor logs a crash with its traceback (ARBORA-HOST-R039.12)."""

from __future__ import annotations

import logging
import types
from typing import Any, cast

import pytest

from arbora.identity.client_contract import ClientContractSupervisor


def test_a_crashed_supervisor_loop_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def loop() -> None:
        raise RuntimeError("loop crashed")

    supervisor = types.SimpleNamespace(loop=loop)
    with caplog.at_level(logging.ERROR):
        ClientContractSupervisor._run(cast(Any, supervisor))
    records = [r for r in caplog.records if "supervisor crashed" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is not None
