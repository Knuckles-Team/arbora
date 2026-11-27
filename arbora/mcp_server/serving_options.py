"""Serving options of the one ``arbora`` entrypoint (ARBORA-HOST-R006.2).

``--transport stdio`` (the default) and ``--transport streamable-http`` are served by the
same entrypoint and one multiplexer. An unknown transport or a port outside ``1..65535``
exits with ``INVALID_REQUEST`` before anything is built; the diagnostic goes to stderr and
stdout stays free for protocol frames. The SDK owns transport, host, port and auth parsing
(``build_network_serving_config``); this module adds no second configuration type.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from typing import Any, NoReturn

__all__ = [
    "EXIT_INVALID_REQUEST",
    "INVALID_REQUEST",
    "SERVING_TRANSPORTS",
    "ServingOptionsRefused",
    "check_serving_options",
    "log_startup",
    "parse_serving_options",
    "startup_record",
]

INVALID_REQUEST = "INVALID_REQUEST"
#: ``EX_USAGE`` of sysexits: the command line is wrong, so nothing was started.
EXIT_INVALID_REQUEST = 64
SERVING_TRANSPORTS = ("stdio", "streamable-http")


class ServingOptionsRefused(ValueError):
    """The serving options are not a request the entrypoint can serve."""

    code = INVALID_REQUEST


def check_serving_options(args: Any) -> None:
    """Return when the transport and port are servable, else refuse ``INVALID_REQUEST``."""
    transport = str(getattr(args, "transport", "stdio") or "stdio")
    if transport not in SERVING_TRANSPORTS:
        raise ServingOptionsRefused("unknown transport")
    port = getattr(args, "port", 8000)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ServingOptionsRefused("port outside 1..65535")


def _exit_invalid(reason: str) -> NoReturn:
    sys.stderr.write(f"{INVALID_REQUEST}: {reason}\n")
    sys.stderr.flush()
    raise SystemExit(EXIT_INVALID_REQUEST)


def parse_serving_options(
    make_parser: Callable[[], argparse.ArgumentParser],
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    """Parse the entrypoint options; a malformed one exits ``INVALID_REQUEST``."""
    try:
        args, _unknown = make_parser().parse_known_args(argv)
    except SystemExit as parser_exit:
        if parser_exit.code in (0, None):
            raise
        _exit_invalid("the serving options were not understood")
    try:
        check_serving_options(args)
    except ServingOptionsRefused as refused:
        _exit_invalid(str(refused))
    return args


def log_startup(
    log: logging.Logger, transport: str, projection: Any, multiplexer: Any
) -> None:
    """Write the one startup record for the served transport."""
    digest = getattr(getattr(projection, "registry", None), "digest", "")
    log.info(
        "%s",
        startup_record(
            transport=transport,
            tool_digest=str(digest),
            multiplexer_instances=int(multiplexer is not None),
        ),
    )


def startup_record(
    *, transport: str, tool_digest: str, multiplexer_instances: int
) -> str:
    """The one startup log line: transport, tool digest and multiplexer instance count."""
    return json.dumps(
        {
            "event": "arbora.serving.start",
            "transport": transport,
            "tool_digest": tool_digest,
            "multiplexer_instances": multiplexer_instances,
        },
        sort_keys=True,
    )
