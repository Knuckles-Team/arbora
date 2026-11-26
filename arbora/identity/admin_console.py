"""Typed model for admin-console identity tabs (ARBORA-IDENTITY-R007).

Slice .1: the typed model, construction validation, and refusal tests only.
The dry-run preview and mode wizard behaviors are later slices.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .engine import IdentityUnavailable

_TABS = ("users", "sessions", "providers", "policy")


@dataclass(frozen=True, slots=True)
class AdminConsoleTab:
    """One identity-administration tab, named from the fixed tab set."""

    name: str

    def __post_init__(self) -> None:
        if self.name not in _TABS:
            raise IdentityUnavailable(
                f"unknown admin console tab {self.name!r}; expected one of {_TABS}"
            )


def dry_run_mapping(
    tab: AdminConsoleTab,
    rules: list[tuple[str, frozenset[str]]],
    claims: dict[str, object],
) -> frozenset[str]:
    """Preview the roles an ordered mapping-rule set would assign, without applying them.

    Slice .2: the dry-run preview over the .1 typed tab model. ``rules`` is
    evaluated in order; the first rule whose claim key is present and truthy
    in ``claims`` wins. No rule matching returns an empty role set.
    """
    if tab.name != "providers":
        raise IdentityUnavailable("mapping dry-run only applies to the providers tab")
    for claim_key, roles in rules:
        if claims.get(claim_key):
            return roles
    return frozenset()


def transition_mode_step(tab: AdminConsoleTab, target_mode: str) -> dict[str, str]:
    """One guided step of the mode-transition wizard, scoped to the policy tab."""
    if tab.name != "policy":
        raise IdentityUnavailable("the mode wizard only applies to the policy tab")
    if target_mode not in ("none", "local", "external"):
        raise IdentityUnavailable(f"unknown target auth mode {target_mode!r}")
    return {"step": "confirm", "target_mode": target_mode}


def dry_run_provider_rules(
    rules: list[dict[str, object]], claims: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Preview what a provider's stored mapping rules assign, as the engine would.

    The engine takes the union of every matching rule (``equals`` or ``prefix``
    on one claim path) and splits each ``role:<id>`` or ``group:<id>`` target.
    It writes nothing; the result names the matching rule ids in rule order.
    """
    roles: set[str] = set()
    groups: set[str] = set()
    matched: list[str] = []
    for rule in rules:
        _require_valid_rule(rule)
        values = claims.get(str(rule.get("claim_path", "")), [])
        kind, expected = str(rule.get("match_kind", "")), str(rule.get("value", ""))
        hit = any(
            (kind == "equals" and value == expected)
            or (kind == "prefix" and value.startswith(expected))
            for value in values
        )
        if not hit:
            continue
        matched.append(str(rule.get("rule_id", "")))
        target_kind, _, target_id = str(rule.get("target", "")).partition(":")
        if target_kind == "role" and target_id:
            roles.add(target_id)
        elif target_kind == "group" and target_id:
            groups.add(target_id)
    return {
        "roles": sorted(roles),
        "groups": sorted(groups),
        "matched_rules": matched,
    }


class AdminConsoleRefused(IdentityUnavailable):
    """A closed refusal code of the admin console (ARBORA-IDENTITY-R007.2)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require_valid_rule(rule: dict[str, object]) -> None:
    target_kind, _, target_id = str(rule.get("target", "")).partition(":")
    if (
        str(rule.get("match_kind", "")) not in {"equals", "prefix"}
        or not str(rule.get("claim_path", ""))
        or target_kind not in {"role", "group"}
        or not target_id
    ):
        raise AdminConsoleRefused("MAPPING_RULE_INVALID")


#: The wizard's fixed steps, in order; a step needs the previous step's receipt.
WIZARD_STEPS = (
    "plan",
    "backup_confirmed",
    "link_migration_dry_run",
    "transition",
    "verify",
)


@dataclass(frozen=True, slots=True)
class StepReceipt:
    """The proof that one wizard step ran, chained to the step before it."""

    step: str
    digest: str
    ownership_changes: int = 0


def advance_wizard(
    previous: StepReceipt | None, step: str, *, ownership_changes: int = 0
) -> StepReceipt:
    """Run one step of the mode wizard; the order is fixed and each step is chained.

    The link-migration dry run reports its ownership changes in its receipt, and the
    transition step refuses unless that receipt reports zero.
    """
    if step not in WIZARD_STEPS:
        raise AdminConsoleRefused("STEP_OUT_OF_ORDER")
    expected = 0 if previous is None else WIZARD_STEPS.index(previous.step) + 1
    if WIZARD_STEPS.index(step) != expected:
        raise AdminConsoleRefused("STEP_OUT_OF_ORDER")
    if step == "transition" and previous is not None and previous.ownership_changes:
        raise AdminConsoleRefused("OWNERSHIP_CHANGES_PRESENT")
    changes = ownership_changes if step == "link_migration_dry_run" else 0
    link = "" if previous is None else previous.digest
    digest = hashlib.sha256(f"{link}\0{step}\0{changes}".encode()).hexdigest()
    return StepReceipt(step, digest, changes)
