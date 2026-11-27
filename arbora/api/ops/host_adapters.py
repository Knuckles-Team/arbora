"""The harness adapters the host supplies to served operations.

``HarnessProbeRunner`` is the production ``SecurityProbeRunner`` of
``security.evaluate`` (ARBORA-OPS-R051.3): it submits the existing ``agents.run``
operation through the governed step pipeline, one run per selected catalog entry
and authorized target, and reports each run as ids and scores only.
``SandboxProcessTerminator`` is the production ``ProcessTerminator`` of
``ops.process.terminate`` (ARBORA-OPS-R055.2): it ends exactly one process of one
run through the host's per-run sandbox process table, which re-checks admission.
Neither decides anything; the engine judges the probes and the approval binds the
termination.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import OpError
from arbora.api.ops.security_eval import EvaluationCatalog, ProbeRun

__all__ = ["HarnessProbeRunner", "SandboxProcessTerminator"]

#: The A2A message metadata keys that name the probe a run carries.
PROBE_ENTRY_KEY = "red_team_entry_id"
PROBE_TARGET_KEY = "red_team_target_id"


def _unwrap(value: Any) -> Mapping[str, Any]:
    """The run's value, one ``value`` envelope deep."""
    if isinstance(value, Mapping) and isinstance(value.get("value"), Mapping):
        value = value["value"]
    return value if isinstance(value, Mapping) else {}


class HarnessProbeRunner:
    """Runs each selected probe as one ``agents.run`` against one authorized target."""

    def _key(self, context: Any, entry_id: str, target_id: str) -> str:
        seed = f"{context.idempotency_key}|{target_id}|{entry_id}".encode()
        return "security-probe:" + hashlib.sha256(seed).hexdigest()[:48]

    def _probe(
        self, entry_id: str, target_id: str, value: Mapping[str, Any]
    ) -> ProbeRun:
        reported = _unwrap(value.get("probe") or value)
        task = str(value.get("task_id") or value.get("id") or entry_id)
        try:
            return ProbeRun.model_validate(
                {
                    "probe_id": f"{target_id}:{entry_id}",
                    "attack_id": entry_id,
                    "transform_ids": reported.get("transform_ids") or (),
                    "category": reported.get("category") or "uncategorized",
                    "evidence_ref": f"run:{task}",
                    "scores": reported.get("scores") or (),
                }
            )
        except ValidationError as invalid:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "probe result is malformed"}
            ) from invalid

    async def run(
        self, context: Any, entries: EvaluationCatalog, target_id: str
    ) -> tuple[ProbeRun, ...]:
        invoke = context.services.get("plan_step_invoke")
        if invoke is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the invoke pipeline is not bound"}
            )
        runs: list[ProbeRun] = []
        for entry in entries.entries:
            message = {
                "role": "user",
                "parts": [{"kind": "text", "text": f"probe {entry.entry_id}"}],
                "metadata": {
                    PROBE_ENTRY_KEY: entry.entry_id,
                    PROBE_TARGET_KEY: target_id,
                },
            }
            outcome = await invoke(
                "agents.run",
                {"message": message},
                context.caller,
                self._key(context, entry.entry_id, target_id),
            )
            if isinstance(outcome, OpError):
                raise OperationRefused(
                    "UNAVAILABLE",
                    {"reason": "probe run refused", "code": outcome.code},
                )
            runs.append(self._probe(entry.entry_id, target_id, _unwrap(outcome.value)))
        return tuple(runs)


class SandboxProcessTerminator:
    """Ends one run's process through the host's process table, resolved at call time."""

    def __init__(self, table: Callable[[], Any]) -> None:
        self._table = table

    async def terminate(
        self, *, run_id: str, process_id: str, approval_ref: str
    ) -> None:
        from arbora.agent_host.port_services import SandboxProcessRefused

        table = self._table()
        if table is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the sandbox process table is not composed"}
            )
        try:
            ended = await table.terminate(run_id, process_id)
        except SandboxProcessRefused as refused:
            raise OperationRefused("POLICY_DENIED", {"reason": refused.code}) from None
        if not ended:
            # A process of another run answers exactly like an absent one.
            raise OperationRefused("POLICY_DENIED", {"reason": "process_mismatch"})
