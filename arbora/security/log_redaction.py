#!/usr/bin/python
from __future__ import annotations

"""Redact runtime locations/endpoints/caller identifiers before they reach a log call.

CONCEPT:MERISTEM-OS.observability.log-location-privacy — a raw filesystem path, network
endpoint, or caller identifier in a log line leaks host/tenant topology to anyone
with log read access (a log-aggregation operator, a compromised sidecar, a support
bundle). ``tests/unit/security/test_log_location_privacy_static_gate.py`` statically
walks every ``meristem`` module for a log call (``logger.info``/``warning``/
etc.) that passes a variable/attribute NAMED like one of these (``path``,
``endpoint``, ``host``, ``file_path``, ...) and fails the build if it finds one —
the AST match is a proxy for "a sensitive runtime value flows into a log record",
so the fix is to stop logging the raw value, not to rename the variable.

:func:`redact_for_log` is the one shared, deterministic redaction every log call
site uses instead: the same input always produces the same short tag within a
process, so operators can still correlate repeated log lines about the same
path/endpoint/caller across a session — without the log itself ever carrying the
literal value.
"""

import hashlib
import logging
import traceback

__all__ = ["install_traceback_redaction", "redact_for_log", "redacted_traceback"]


def redact_for_log(value: object) -> str:
    """Return a short, deterministic, non-reversible tag for a sensitive value.

    ``None``/empty values redact to ``"<empty>"``. Anything else is rendered as
    ``str(value)`` and reduced to a 12-hex-character SHA-256 prefix — enough to
    tell two DIFFERENT values apart in a log stream without exposing either one.
    """
    if value is None or value == "":
        return "<empty>"
    digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()
    return f"<redacted:{digest[:12]}>"


def _cause_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    node: BaseException | None = exc
    while node is not None and node not in chain:
        chain.append(node)
        node = node.__cause__ or node.__context__
    return chain


def _render_one(item: BaseException) -> str:
    frames = "".join(
        f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
        for frame in traceback.extract_tb(item.__traceback__)
    )
    return (
        f"Traceback (most recent call last):\n{frames}"
        f"{type(item).__qualname__}: {redact_for_log(str(item))}\n"
    )


def redacted_traceback(exc: BaseException) -> str:
    """Render ``exc`` and its cause chain with every message redacted.

    Frames keep only file, line and function name: source lines can quote a
    literal and are dropped. The message of each exception is replaced by
    :func:`redact_for_log`, so a secret carried in a message never reaches a
    log sink.
    """
    return "\n".join(_render_one(item) for item in reversed(_cause_chain(exc))).rstrip(
        "\n"
    )


_RECORD_FACTORY_MARK = "_arbora_traceback_redaction"


def install_traceback_redaction() -> None:
    """Redact the exception text of every log record that carries ``exc_info``.

    A log call with ``exc_info=True`` renders the exception message through the
    formatter; that message may hold a secret. The record factory pre-fills the
    record's rendered traceback with :func:`redacted_traceback`, which every
    stdlib formatter then reuses. Safe to call more than once.
    """
    current = logging.getLogRecordFactory()
    if getattr(current, _RECORD_FACTORY_MARK, False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = current(*args, **kwargs)
        info = record.exc_info
        if isinstance(info, tuple) and isinstance(info[1], BaseException):
            record.exc_text = redacted_traceback(info[1])
        return record

    setattr(factory, _RECORD_FACTORY_MARK, True)
    logging.setLogRecordFactory(factory)
