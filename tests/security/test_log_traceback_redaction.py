"""Tracebacks logged at a boundary carry frames but never exception message text."""

from __future__ import annotations

import io
import logging

import pytest

from arbora.security.log_redaction import (
    install_traceback_redaction,
    redact_for_log,
    redacted_traceback,
)

SECRET = "hunter2-token-value"  # sanitizer:ignore - synthetic fixture proving redaction


def _raise_chain() -> None:
    try:
        raise OSError(SECRET)
    except OSError as cause:
        raise RuntimeError(f"wrapped {SECRET}") from cause


@pytest.mark.spec("ARBORA-HOST-R039.12")
def test_redacted_traceback_keeps_frames_and_drops_messages() -> None:
    try:
        _raise_chain()
    except RuntimeError as exc:
        text = redacted_traceback(exc)
    assert SECRET not in text
    assert "_raise_chain" in text
    assert f"OSError: {redact_for_log(SECRET)}" in text
    assert text.index("OSError") < text.index("RuntimeError")


@pytest.mark.spec("ARBORA-HOST-R039.12")
def test_installed_factory_redacts_every_formatter_and_is_idempotent() -> None:
    install_traceback_redaction()
    factory = logging.getLogRecordFactory()
    install_traceback_redaction()
    assert logging.getLogRecordFactory() is factory
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("arbora.test.traceback_redaction")
    log.addHandler(handler)
    log.propagate = False
    try:
        try:
            _raise_chain()
        except RuntimeError:
            log.warning("boundary failed", exc_info=True)
    finally:
        log.removeHandler(handler)
    rendered = stream.getvalue()
    assert "boundary failed" in rendered
    assert "_raise_chain" in rendered
    assert SECRET not in rendered
