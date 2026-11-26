"""The operator's explicit deploy or reuse choice per component (ARBORA-DEPLOY-R013.2).

Every component the deployment manifest declares gets exactly one confirmed
choice, ``deploy`` or ``reuse``. A ``reuse`` choice names the ``endpoint_ref`` of
the running service and is connect-only (ARBORA-DEPLOY-R013.3); only ``deploy``
components go to the engine's placement question. The inventory carries a
canonical ``choices_digest`` and no credential: an endpoint reference holding
user information is refused.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

ACTIONS = ("deploy", "reuse")
CHOICE_MISSING = "CHOICE_MISSING"
ENDPOINT_CONFLICT = "ENDPOINT_CONFLICT"
COMPONENT_UNDECLARED = "COMPONENT_UNDECLARED"


class ChoiceRefused(ValueError):
    """A refusal named by ``code``; it is raised before any call is made."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ComponentDeploymentChoice:
    """One component's confirmed decision."""

    component_id: str
    action: str
    endpoint_ref: str | None
    profile: str
    confirmed: bool

    def evidence(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "action": self.action,
            "endpoint_ref": self.endpoint_ref,
            "profile": self.profile,
        }


@dataclass(frozen=True)
class ChoiceInventory:
    """The confirmed choices of one deployment, ordered by component id."""

    choices: tuple[ComponentDeploymentChoice, ...]

    @property
    def deploy_components(self) -> tuple[str, ...]:
        return tuple(c.component_id for c in self.choices if c.action == "deploy")

    @property
    def reuse_components(self) -> tuple[str, ...]:
        return tuple(c.component_id for c in self.choices if c.action == "reuse")

    @property
    def choices_digest(self) -> str:
        raw = json.dumps(
            [c.evidence() for c in self.choices],
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def placement_components(self) -> list[str]:
        """The components that go to the engine placement question: ``deploy`` only."""
        return list(self.deploy_components)


def _choice(raw: Mapping[str, Any]) -> ComponentDeploymentChoice:
    component = str(raw.get("component_id", ""))
    action = raw.get("action")
    if action not in ACTIONS:
        raise ChoiceRefused(
            CHOICE_MISSING, f"{component or '?'}: action must be deploy or reuse"
        )
    endpoint = raw.get("endpoint_ref")
    if action == "reuse":
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ChoiceRefused(
                ENDPOINT_CONFLICT, f"{component}: reuse needs an endpoint_ref"
            )
        if urlsplit(endpoint).username is not None or urlsplit(endpoint).password:
            raise ChoiceRefused(
                ENDPOINT_CONFLICT, f"{component}: endpoint_ref carries a credential"
            )
    elif endpoint:
        raise ChoiceRefused(
            ENDPOINT_CONFLICT, f"{component}: a deploy choice has no endpoint_ref"
        )
    return ComponentDeploymentChoice(
        component_id=component,
        action=str(action),
        endpoint_ref=str(endpoint) if endpoint else None,
        profile=str(raw.get("profile", "")),
        confirmed=raw.get("confirmed") is True,
    )


def build_inventory(
    declared: Iterable[str], answers: Sequence[Mapping[str, Any]]
) -> ChoiceInventory:
    """Validate ``answers`` against the declared component ids.

    Refusals, each before any call: ``COMPONENT_UNDECLARED`` for a choice naming
    a component the manifest does not declare, ``CHOICE_MISSING`` for a declared
    component with no confirmed choice, ``ENDPOINT_CONFLICT`` for a reuse choice
    without an endpoint, a deploy choice with one, a credential in it, or one
    endpoint claimed by two components.
    """
    declared_ids = list(declared)
    choices = [_choice(a) for a in answers]
    for choice in choices:
        if choice.component_id not in declared_ids:
            raise ChoiceRefused(COMPONENT_UNDECLARED, choice.component_id)
    seen: dict[str, ComponentDeploymentChoice] = {}
    for choice in choices:
        if choice.component_id in seen:
            raise ChoiceRefused(
                ENDPOINT_CONFLICT, f"{choice.component_id}: chosen twice"
            )
        seen[choice.component_id] = choice
    missing = [c for c in declared_ids if c not in seen or not seen[c].confirmed]
    if missing:
        raise ChoiceRefused(CHOICE_MISSING, ", ".join(sorted(missing)))
    endpoints: dict[str, str] = {}
    for choice in choices:
        if choice.endpoint_ref is not None:
            owner = endpoints.setdefault(choice.endpoint_ref, choice.component_id)
            if owner != choice.component_id:
                raise ChoiceRefused(
                    ENDPOINT_CONFLICT,
                    f"{owner} and {choice.component_id} share one endpoint_ref",
                )
    return ChoiceInventory(tuple(sorted(choices, key=lambda c: c.component_id)))


def ask_once_per_component(
    declared: Iterable[str],
    ask: Callable[[str], Mapping[str, Any] | None],
    *,
    profile: str,
) -> ChoiceInventory:
    """Ask each declared component once; an unanswered component is ``CHOICE_MISSING``."""
    answers: list[Mapping[str, Any]] = []
    declared_ids = list(declared)
    for component in declared_ids:
        reply = ask(component)
        if reply is not None:
            answers.append(
                {
                    "component_id": component,
                    "profile": profile,
                    "confirmed": True,
                    **reply,
                }
            )
    return build_inventory(declared_ids, answers)
