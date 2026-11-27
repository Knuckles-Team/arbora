"""The shape of one entry of the arbora verbose tool surface."""

from typing import TypedDict


class ArboraAction(TypedDict):
    tool: str
    action: str | None
    name: str
