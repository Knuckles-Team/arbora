"""The one scope registry, ingested from the scopes each server serves (ARBORA-IDENTITY-R002.2).

The engine and every connector serve their scopes, each with its class, in their handshake
(``MEDULLA-CONTRACT-R054``, ``MEDULLA-CONTRACT-R057``). Arbora holds them in one registry
object filled by :func:`register_served_scopes`; no module is rendered from a packaged file
and no scope list is committed. The engine's own scopes come from its run-time method
catalog, read once on first use (:func:`registry`). A scope an engine or
connector does not serve never reaches a session.

A served scope whose name is already registered with a different class refuses the
onboarding of that server (:class:`OnboardingRefused`) and registers nothing of it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping
from functools import cache
from types import MappingProxyType
from typing import Any

#: The five scope classes the engine's registry recognizes.
CLASSES: tuple[str, ...] = ("user", "domain", "service-only", "approver", "admin")

__all__ = [
    "CLASSES",
    "OnboardingRefused",
    "ScopeRegistry",
    "approver_groups",
    "register_served_scopes",
    "registry",
    "scope_classes",
    "session_scopes",
]


class OnboardingRefused(ValueError):
    """A served scope list the registry cannot take; ``server`` names the refused server."""

    def __init__(self, server: str, reason: str) -> None:
        self.server = server
        self.reason = reason
        super().__init__(f"onboarding of {server} refused: {reason}")


def _stage(
    server: str, entries: Iterable[Mapping[str, Any]]
) -> dict[str, tuple[str, str | None]]:
    """One server's served scopes as ``name -> (class, approver group)``, each checked."""
    staged: dict[str, tuple[str, str | None]] = {}
    for entry in entries:
        name, scope_class = entry.get("scope"), entry.get("class")
        if not isinstance(name, str) or not name:
            raise OnboardingRefused(server, "a served scope has no name")
        if scope_class not in CLASSES:
            raise OnboardingRefused(server, f"scope {name} has unknown class")
        group = entry.get("approver_group")
        served = (str(scope_class), str(group) if group else None)
        if staged.setdefault(name, served) != served:
            raise OnboardingRefused(server, f"scope {name} is served twice")
    return staged


class ScopeRegistry:
    """name -> class (and approver group) for every scope a server has served."""

    def __init__(self) -> None:
        self._classes: dict[str, str] = {}
        self._groups: dict[str, str] = {}
        self._lock = threading.Lock()

    def register(self, server: str, entries: Iterable[Mapping[str, Any]]) -> None:
        """Take one server's served scopes whole, or refuse and take none of them."""
        staged = _stage(server, entries)
        with self._lock:
            for name, (scope_class, _group) in staged.items():
                held = self._classes.get(name)
                if held is not None and held != scope_class:
                    raise OnboardingRefused(
                        server, f"scope {name} is registered as {held}"
                    )
            for name, (scope_class, group) in staged.items():
                self._classes[name] = scope_class
                if group:
                    self._groups[name] = group

    def classes(self) -> Mapping[str, str]:
        """scope -> class, read-only."""
        return MappingProxyType(self._classes)

    def groups(self) -> Mapping[str, str]:
        """approver-class scope -> the one built-in group that confers it, read-only."""
        return MappingProxyType(self._groups)

    def scopes(self) -> frozenset[str]:
        """Every registered scope: what a verified identity may project into a session."""
        return frozenset(self._classes)


@cache
def registry() -> ScopeRegistry:
    """The process registry, built once with the engine's served scopes ingested.

    A failed engine read builds nothing, so the next call reads again.
    """
    import importlib

    held = ScopeRegistry()
    engine = importlib.import_module("medulla.engine")
    held.register("engine", json.loads(engine.method_catalog())["scopes"])
    return held


def register_served_scopes(server: str, entries: Iterable[Mapping[str, Any]]) -> None:
    """Register one connector's handshake scopes, or refuse its onboarding on a conflict."""
    registry().register(server, entries)


def scope_classes() -> Mapping[str, str]:
    """scope -> class of every served scope."""
    return registry().classes()


def approver_groups() -> Mapping[str, str]:
    """approver-class scope -> its built-in group."""
    return registry().groups()


def session_scopes() -> frozenset[str]:
    """The allowlist a verified identity may project into a session: exactly the registry."""
    return registry().scopes()
