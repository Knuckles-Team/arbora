"""Shared doubles for the certified-workflow served tests (ARBORA-FLEET-R034.8 to R034.15)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import Idempotency, Surface
from arbora.fleet.admission import AdmissionGate
from arbora.fleet.engine_admission import EngineAdmission
from tests.api.test_invoke_contract import operation, run, setup_services
from tests.fleet.test_admission_wiring import GOOD, _RunParams

TENANT = "tenant:one"


def h(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def attestation(skill: str, *, version: str = "v1", deps: tuple[str, ...] = ()) -> dict:
    """An engine ``LocalCertificationAttestation`` of ``skill``."""

    def dependency(name: str, digest: str) -> dict:
        return {"component_id": name, "kind": "skill", "definition_digest": digest}

    return {
        "body": {
            "skill": dependency(skill, h(f"{skill}:{version}")),
            "pins": {
                "model": dependency("model", h("model:r1")),
                "dataset": {"id": "d", "revision": 1, "digest": h("data")},
                "criteria_policy": dependency("policy", h("policy:r1")),
            },
            "dependencies": [dependency(d, h(f"{d}:v1")) for d in deps],
        },
        "digest": h(f"attestation:{skill}:{version}"),
    }


def certification(
    skill: str, *, version: str = "v1", deps: tuple[str, ...] = ()
) -> dict:
    """The binding the caller presents for ``attestation``'s skill."""
    return {
        "skill_id": skill,
        "skill_version": h(f"{skill}:{version}"),
        "dependency_digests": [h(f"{d}:v1") for d in deps],
        "model_endpoint_revision": h("model:r1"),
        "system_prompt_digest": h("policy:r1"),
        "attestation_digest": h(f"attestation:{skill}:{version}"),
    }


class Engine:
    """A structural double of the engine: it stores attestations and answers by digest."""

    def __init__(self, journal: list[str]) -> None:
        self.stored: dict[str, dict] = {}
        self.journal = journal
        self.calls: list[tuple[str, dict]] = []

    def store(self, item: dict) -> None:
        self.stored[item["digest"]] = item

    async def send(self, method: str, params: dict, tenant: str) -> Any:
        self.calls.append((method, params))
        self.journal.append(f"engine:{method}")
        assert method == "DecisionEval" and params["op"] == "certification_get"
        item = self.stored.get(params["request"]["attestation_digest"])
        if item is None:
            raise LookupError("no such receipt")
        return {"attestation": item}


class DenyingPolicy(PolicyGate):
    """The one narrowing boundary, denying named tools at the ``load`` phase."""

    def __init__(self, denied: frozenset[str] = frozenset()) -> None:
        super().__init__("none")
        self.denied = denied
        self.asked: list[list[str]] = []

    async def loadable(self, items, caller):  # type: ignore[override]
        self.asked.append([i.resource for i in items])
        return [
            i.resource.removeprefix("fleet:tool:") not in self.denied for i in items
        ]


def workflow_metadata(
    *,
    tools: Mapping[str, str] | None = None,
    grant_revision: str = "g1",
    dependencies: Mapping[str, tuple[str, ...]] | None = None,
    certifications: tuple[dict, ...] = (),
) -> dict:
    return {
        "declaration": GOOD,
        "binding": {
            "workflow_digest": h("workflow"),
            "tool_schema_digests": dict(tools or {"crm.read": h("crm.read")}),
            "grant_revision": grant_revision,
            "dependencies": {k: list(v) for k, v in (dependencies or {}).items()},
        },
        "certifications": list(certifications),
    }


class World:
    """One served ``agents.run`` with the engine, grants and policy doubles bound."""

    def __init__(
        self,
        *,
        denied: frozenset[str] = frozenset(),
        grant_tools: tuple[str, ...] = ("crm.read",),
        wired: bool = True,
    ) -> None:
        self.journal: list[str] = []
        self.engine = Engine(self.journal)
        self.grant = {"revision": "g1", "tools": list(grant_tools)}
        self.policy = DenyingPolicy(denied)

        async def grants(_caller: Any) -> Mapping[str, Any]:
            return self.grant

        gate = AdmissionGate(
            port=EngineAdmission(self.engine.send, lambda _t: {}) if wired else None,
            grants=grants,
            declarations=lambda: {"crm.read": h("crm.read")},
        )
        op = operation(
            id="agents.run", params=_RunParams, idempotency=Idempotency.KEY_REQUIRED
        )
        self.services, self.runtime, self.audit, self.journal_store, _ = setup_services(
            op, admission=gate, policy_gate=self.policy
        )

        async def dispatch() -> dict:
            self.journal.append("run")
            return {"value": 1}

        self.runtime.on_dispatch = dispatch

    def call(self, metadata: dict, *, surface: Surface = Surface.HTTP, key: str = "k"):
        return run(
            self.services,
            op_id="agents.run",
            params={"message": {"metadata": metadata}},
            surface=surface,
            idempotency_key=key,
        )


def no_pin_lag(monkeypatch: Any) -> None:
    """Treat the engine ``main`` shapes as served (the structural double speaks them)."""
    from arbora.fleet import engine_admission

    monkeypatch.setattr(
        engine_admission,
        "pin_lag",
        SimpleNamespace(
            guard_certification_read=lambda: None, guard_skill_upgrade=lambda: None
        ),
    )
