"""One application admission at a time, with rollback (ARBORA-DATA-MARKET-R004).

``AdmissionRun`` is the explicit state machine of one admission. Its states are the
closed :class:`AdmissionState` enum and its moves are the closed :data:`TRANSITIONS`
table. The run holds no state: every read and every write goes through the
:class:`AdmissionStore` port, which the host binds to the engine (a graph node per
run and one active-run node per tenant). ``AdmissionRun._move`` is the only writer
of ``state``; it swaps the node only when the stored state is the one the move
leaves, so two writers cannot both win.

Approval is a governed change of kind ``governed.source-authority``
(MEDULLA-SRC-AUTH-R003.1) that the engine approves under
``governance:approve-source-authority``. Arbora carries the approver's call and
never decides it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol

from arbora.control_plane.admission.models import AdmissionIntent
from arbora.control_plane.admission.service import plan_admission

__all__ = [
    "SOURCE_AUTHORITY_KIND",
    "TRANSITIONS",
    "AdmissionError",
    "AdmissionGovernance",
    "AdmissionRun",
    "AdmissionSources",
    "AdmissionState",
    "AdmissionStore",
    "admission_run_id",
    "plan_digest",
]

#: The governed-change kind that approves a source's authority (MEDULLA-SRC-AUTH-R003.1).
SOURCE_AUTHORITY_KIND = "governed.source-authority"


class AdmissionState(StrEnum):
    PLANNED = "planned"
    APPROVED = "approved"
    PROBING = "probing"
    ADMITTED = "admitted"
    ROLLED_BACK = "rolled_back"
    RETIRED = "retired"


#: Every legal move. A state absent as a key has no move out (terminal).
TRANSITIONS: Mapping[AdmissionState, frozenset[AdmissionState]] = {
    AdmissionState.PLANNED: frozenset(
        {AdmissionState.APPROVED, AdmissionState.ROLLED_BACK}
    ),
    AdmissionState.APPROVED: frozenset(
        {AdmissionState.PROBING, AdmissionState.ROLLED_BACK}
    ),
    AdmissionState.PROBING: frozenset(
        {AdmissionState.ADMITTED, AdmissionState.ROLLED_BACK}
    ),
    AdmissionState.ADMITTED: frozenset({AdmissionState.RETIRED}),
    AdmissionState.ROLLED_BACK: frozenset(),
    AdmissionState.RETIRED: frozenset(),
}
#: States in which the run holds its tenant's one active slot.
_ACTIVE = frozenset(
    {AdmissionState.PLANNED, AdmissionState.APPROVED, AdmissionState.PROBING}
)


class AdmissionError(Exception):
    """A refusal with the served code (``CONFLICT``, ``POLICY_DENIED``, ...)."""

    def __init__(self, code: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


class AdmissionStore(Protocol):
    """The durable state of runs; the host binds it to the engine."""

    async def read(self, tenant: str, run_id: str) -> dict[str, Any] | None: ...

    async def create(
        self, tenant: str, run_id: str, fields: Mapping[str, Any]
    ) -> None: ...

    async def swap(
        self,
        tenant: str,
        run_id: str,
        expect: Mapping[str, Any],
        updates: Mapping[str, Any],
    ) -> bool: ...

    async def claim_active(self, tenant: str, run_id: str) -> str:
        """Hold the tenant's one slot; the holder's run id (``run_id`` when taken)."""
        ...

    async def release_active(self, tenant: str, run_id: str) -> None: ...


class AdmissionGovernance(Protocol):
    """The governed change that approves a source (engine ``GovernedChange``)."""

    async def propose(
        self, change_id: str, digest: str, target: str, justification: str
    ) -> None: ...

    async def approve(self, change_id: str, digest: str) -> None: ...

    async def status(self, change_id: str) -> str | None:
        """The engine status (``proposed``, ``approved``, ...) or ``None`` when unknown."""
        ...


class AdmissionSources(Protocol):
    """The source side effects and measurements of one admission."""

    async def attach(self, source_id: str, tables: Sequence[str]) -> None: ...

    async def detach(self, source_id: str, tables: Sequence[str]) -> None: ...

    async def probe(self, source_id: str) -> bool: ...

    async def table_counts(
        self, source_id: str, tables: Sequence[str]
    ) -> Mapping[str, int]: ...

    async def sample_digest(
        self, source_id: str, tables: Sequence[str], limit: int
    ) -> str: ...


def plan_digest(intent: AdmissionIntent, source_id: str, tables: Sequence[str]) -> str:
    """The digest the governed change binds: intent, source and parity tables."""
    body = json.dumps(
        [intent.model_dump(mode="json"), source_id, list(tables)],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def admission_run_id(tenant: str, digest: str) -> str:
    """The run node id: one tenant and one plan are one run, so a replay finds it."""
    return f"admission-run:{tenant}:{digest[:32]}"


def _change_id(run_id: str) -> str:
    return "admit-" + hashlib.sha256(run_id.encode()).hexdigest()[:32]


def _retire_change_id(run_id: str) -> str:
    return "retire-" + hashlib.sha256(run_id.encode()).hexdigest()[:32]


def _view(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: run[key]
        for key in (
            "run_id",
            "state",
            "app_name",
            "source_id",
            "plan_digest",
            "change_id",
            "parity",
            "failure",
        )
        if key in run
    }


class AdmissionRun:
    """Plan, approve, probe, admit, roll back and retire one source admission."""

    def __init__(
        self,
        store: AdmissionStore,
        governance: AdmissionGovernance,
        sources: AdmissionSources,
        *,
        parity_sample: int,
    ) -> None:
        self._store = store
        self._governance = governance
        self._sources = sources
        self._sample = parity_sample

    async def open(
        self,
        tenant: str,
        intent: AdmissionIntent,
        *,
        source_id: str,
        prior_connector: str | None,
        tables: Sequence[str],
        fence: str,
    ) -> dict[str, Any]:
        """Create the run in ``planned`` and propose its approval, once per plan."""
        plan = plan_admission(intent)
        if not plan.ready:
            raise AdmissionError("POLICY_DENIED", {"blockers": list(plan.blockers)})
        digest = plan_digest(intent, source_id, tables)
        run_id = admission_run_id(tenant, digest)
        existing = await self._store.read(tenant, run_id)
        if existing is not None:
            return existing
        if await self._store.claim_active(tenant, run_id) != run_id:
            raise AdmissionError("CONFLICT", {"reason": "an admission is active"})
        fields = {
            "run_id": run_id,
            "tenant": tenant,
            "state": AdmissionState.PLANNED.value,
            "app_name": intent.app_name,
            "source_id": source_id,
            "prior_connector": prior_connector or "",
            "tables": list(tables),
            "plan_digest": digest,
            "change_id": _change_id(run_id),
            "fence": fence,
        }
        await self._store.create(tenant, run_id, fields)
        await self._propose(fields["change_id"], digest, source_id, intent.app_name)
        return fields

    async def _propose(
        self, change_id: str, digest: str, target: str, justification: str
    ) -> None:
        if await self._governance.status(change_id) is None:
            await self._governance.propose(change_id, digest, target, justification)

    async def _load(self, tenant: str, run_id: str) -> dict[str, Any]:
        run = await self._store.read(tenant, run_id)
        if run is None:
            raise AdmissionError("NOT_FOUND")
        return run

    async def _move(
        self,
        tenant: str,
        run: dict[str, Any],
        to: AdmissionState,
        **fields: Any,
    ) -> dict[str, Any]:
        """The one writer of ``state``: swap only from the stored state, only on a legal move."""
        current = AdmissionState(run["state"])
        if to not in TRANSITIONS[current]:
            raise AdmissionError("CONFLICT", {"from": current.value, "to": to.value})
        updates = {"state": to.value, **fields}
        if not await self._store.swap(
            tenant, run["run_id"], {"state": current.value}, updates
        ):
            raise AdmissionError("CONFLICT", {"reason": "the run moved concurrently"})
        moved = {**run, **updates}
        if current in _ACTIVE and to not in _ACTIVE:
            await self._store.release_active(tenant, run["run_id"])
        return moved

    async def advance(
        self, tenant: str, run_id: str, *, approve: bool = False
    ) -> dict[str, Any]:
        """Take the run as far as its approval and probes allow; a replay is safe."""
        run = await self._load(tenant, run_id)
        if AdmissionState(run["state"]) is AdmissionState.PLANNED:
            if approve:
                await self._governance.approve(run["change_id"], run["plan_digest"])
            if await self._governance.status(run["change_id"]) != "approved":
                return _view(run)
            run = await self._move(tenant, run, AdmissionState.APPROVED)
        if AdmissionState(run["state"]) is AdmissionState.APPROVED:
            run = await self._begin_probing(tenant, run)
        if AdmissionState(run["state"]) is AdmissionState.PROBING:
            run = await self._probe(tenant, run)
        return _view(run)

    async def _begin_probing(self, tenant: str, run: dict[str, Any]) -> dict[str, Any]:
        moved = await self._move(tenant, run, AdmissionState.PROBING)
        try:
            await self._sources.attach(run["source_id"], run["tables"])
        except Exception as error:
            await self._roll_back(tenant, moved, "attach_failed")
            raise error
        return moved

    async def _probe(self, tenant: str, run: dict[str, Any]) -> dict[str, Any]:
        """Health then parity; a failure or a crash detaches the new source only."""
        try:
            if not await self._sources.probe(run["source_id"]):
                await self._roll_back(tenant, run, "probe_failed")
                raise AdmissionError("UNAVAILABLE", {"reason": "probe_failed"})
            parity = await self._parity(run)
        except AdmissionError:
            raise
        except Exception as error:
            await self._roll_back(tenant, run, "probe_error")
            raise error
        if not parity["passed"]:
            await self._roll_back(tenant, run, "parity_failed", parity=parity)
            raise AdmissionError("PARITY_FAILED", {"parity": parity})
        return await self._move(tenant, run, AdmissionState.ADMITTED, parity=parity)

    async def _parity(self, run: Mapping[str, Any]) -> dict[str, Any]:
        tables = list(run["tables"])
        prior = run["prior_connector"]
        if not prior:
            return {"passed": True, "tables": {}, "sample": "no prior connector"}
        new = await self._sources.table_counts(run["source_id"], tables)
        old = await self._sources.table_counts(prior, tables)
        counts = {t: {"new": new.get(t), "prior": old.get(t)} for t in tables}
        new_digest = await self._sources.sample_digest(
            run["source_id"], tables, self._sample
        )
        old_digest = await self._sources.sample_digest(prior, tables, self._sample)
        passed = (
            all(
                c["new"] is not None and c["new"] == c["prior"] for c in counts.values()
            )
            and new_digest == old_digest
        )
        return {
            "passed": passed,
            "tables": counts,
            "sample": {"new": new_digest, "prior": old_digest, "limit": self._sample},
        }

    async def _roll_back(
        self,
        tenant: str,
        run: dict[str, Any],
        failure: str,
        *,
        parity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Detach the new source and leave the prior connector untouched."""
        await self._sources.detach(run["source_id"], run["tables"])
        fields: dict[str, Any] = {"failure": failure}
        if parity is not None:
            fields["parity"] = dict(parity)
        return await self._move(tenant, run, AdmissionState.ROLLED_BACK, **fields)

    async def retire(
        self, tenant: str, run_id: str, *, approve: bool = False
    ) -> dict[str, Any]:
        """Retire the prior connector once admitted, parity passed and approved."""
        run = await self._load(tenant, run_id)
        if AdmissionState(run["state"]) is not AdmissionState.ADMITTED:
            raise AdmissionError("CONFLICT", {"state": run["state"]})
        if not (run.get("parity") or {}).get("passed"):
            raise AdmissionError("PARITY_FAILED")
        if not run["prior_connector"]:
            raise AdmissionError("CONFLICT", {"reason": "no prior connector"})
        change = _retire_change_id(run_id)
        await self._propose(
            change, run["plan_digest"], run["prior_connector"], run["app_name"]
        )
        if approve:
            await self._governance.approve(change, run["plan_digest"])
        if await self._governance.status(change) != "approved":
            raise AdmissionError("POLICY_DENIED", {"reason": "approval_required"})
        await self._sources.detach(run["prior_connector"], run["tables"])
        retired = await self._move(tenant, run, AdmissionState.RETIRED)
        return _view(retired)

    async def read(self, tenant: str, run_id: str) -> dict[str, Any]:
        return _view(await self._load(tenant, run_id))
