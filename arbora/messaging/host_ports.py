"""Host-supplied ports that the messaging front end calls.

The messaging front end receives, routes and sends. It does not run the
agent, decide whether a send is allowed, or store records. The composition
root binds one implementation for each port at startup:

* ``turn_handler``: hands one inbound turn to the agent harness and returns
  the reply text.
* ``planner_handler_factory``: builds the default inbound handler for an
  engine.
* ``action_gate``: returns the send decision for one outbound message, or
  ``None`` when authorization is unavailable. No bound gate refuses the send.
* ``outbound_mirror``: records a sent message in the knowledge graph.
* ``runtime_signal``: records a runtime-reliability signal.

An unbound optional port is a no-op. An unbound gate fails closed.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

__all__ = ["MessagingHostPorts", "bind_host_ports", "host_ports", "reset_host_ports"]

TurnHandler = Callable[..., Awaitable[str | None]]
PlannerHandlerFactory = Callable[[Any], Awaitable[Callable[..., Awaitable[Any]]]]
ActionGate = Callable[[Mapping[str, str], Any], Any]
OutboundMirror = Callable[[Any, Any], Awaitable[None]]
RuntimeSignal = Callable[[str, str, Mapping[str, Any]], None]


@dataclasses.dataclass(frozen=True)
class MessagingHostPorts:
    """The implementations bound by the composition root."""

    turn_handler: TurnHandler | None = None
    planner_handler_factory: PlannerHandlerFactory | None = None
    action_gate: ActionGate | None = None
    outbound_mirror: OutboundMirror | None = None
    runtime_signal: RuntimeSignal | None = None


_ports = MessagingHostPorts()


def bind_host_ports(**ports: Any) -> MessagingHostPorts:
    """Bind the named ports, keep the others, and return the new set."""
    global _ports
    _ports = dataclasses.replace(_ports, **ports)
    return _ports


def host_ports() -> MessagingHostPorts:
    """Return the currently bound ports."""
    return _ports


def reset_host_ports() -> None:
    """Unbind every port."""
    global _ports
    _ports = MessagingHostPorts()
