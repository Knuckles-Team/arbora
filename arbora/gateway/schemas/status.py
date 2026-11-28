"""The status texts of the gateway's response envelopes."""

from __future__ import annotations

from enum import StrEnum


class ResponseStatus(StrEnum):
    """The one status a successful gateway envelope carries."""

    SUCCESS = "success"
