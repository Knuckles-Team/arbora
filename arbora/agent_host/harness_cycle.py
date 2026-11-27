"""The host side of the harness evolution cycle (ARBORA-HOST-R035).

``HostProbeProvider`` runs one materialized probe through ``meristem.graph.execute_graph``
under the host's one ``HostPorts`` record and reports the trace the harness checks.
``HostCycleProvider`` drives one propose-probe-compose-persist-reflect tick: it reads the
improvement library through ``harness.components.list`` and writes the cycle envelopes through
``eg.graph.AddNode``, both as the run's delegated principal and tenant over the host's
run-principal call (ARBORA-OPS-R890). Both providers hold no state across ticks and decide
nothing; the engine owns every fact and refusal.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from arbora.agent_host.host_port_adapters import RunCall

__all__ = [
    "COMPONENTS_OP",
    "ENVELOPE_OP",
    "HostCycleProvider",
    "HostProbeProvider",
]

COMPONENTS_OP = "harness.components.list"
ENVELOPE_OP = "eg.graph.AddNode"
#: Most components one tick reads from the improvement library.
LIBRARY_PAGE = 200


class HostProbeProvider:
    """``HarnessProbeProvider``: execute one probe and return its trace."""

    def __init__(self, ports_of: Callable[[], Any]) -> None:
        self._ports_of = ports_of

    async def __call__(
        self,
        *,
        graph: Any,
        config: Mapping[str, Any] | None,
        task: str,
        run_id: str,
        mode: str,
        requested_model_id: str | None,
        budget_ref: str,
        agent_name: str,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        from meristem.graph import execute_graph
        from meristem.graph.harness_cycle import conditions_digest

        probe = metadata["harness_probe"]
        result = await execute_graph(
            graph,
            config,
            task,
            run_id=run_id,
            mode=mode,
            requested_model_id=requested_model_id,
            agent_name=agent_name,
            metadata=metadata,
            ports=self._ports_of(),
        )
        status = result.get("status")
        return {
            "run_id": result.get("run_id", run_id),
            "status": getattr(status, "value", status),
            "steps": result.get("steps", 0),
            "usage": result.get("usage") or {},
            "error": result.get("error"),
            "conditions_digest": conditions_digest(
                task=task,
                seed=str(probe.get("seed") or ""),
                model_ref=str(requested_model_id or ""),
                budget_ref=budget_ref,
            ),
        }


def _library_versions(rows: Sequence[Any], task_class: str) -> list[Mapping[str, Any]]:
    """The improvement-library versions a tick may propose for ``task_class``."""
    versions: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        kind = row.get("component_kind") or row.get("kind")
        ref = (
            row.get("component_version_ref") or row.get("version_ref") or row.get("id")
        )
        scoped = row.get("task_class")
        if kind and ref and scoped in (None, "", task_class):
            versions.append(
                {"component_kind": str(kind), "component_version_ref": str(ref)}
            )
    return versions


def _envelope_ref(envelope: Mapping[str, Any]) -> str:
    body = json.dumps(envelope, sort_keys=True, default=repr, separators=(",", ":"))
    return "harness-envelope:" + hashlib.sha256(body.encode()).hexdigest()[:32]


class HostCycleProvider:
    """``HarnessCycleProvider``: one evolution tick under the run's principal."""

    def __init__(self, call: RunCall) -> None:
        self._call = call

    async def _answered(
        self, op_id: str, params: Mapping[str, Any], run_ref: str
    ) -> Mapping[str, Any]:
        answer = await self._call(op_id, params, run_ref)
        if answer.get("outcome") != "answered":
            raise RuntimeError(f"{op_id} refused: {answer.get('refusal_code')}")
        return dict(answer.get("payload") or {})

    async def _library(self, run_ref: str) -> Sequence[Any]:
        payload = await self._answered(COMPONENTS_OP, {"limit": LIBRARY_PAGE}, run_ref)
        value = payload.get("value")
        rows = value.get("components") if isinstance(value, Mapping) else None
        return rows if isinstance(rows, list) else []

    async def _flush(
        self, envelopes: Sequence[tuple[str, Mapping[str, Any]]], run_ref: str
    ) -> None:
        import msgpack

        for ref, envelope in envelopes:
            packed = msgpack.packb(dict(envelope), use_bin_type=True)
            await self._answered(
                ENVELOPE_OP,
                {"node_id": ref, "properties_msgpack": list(packed)},
                run_ref,
            )

    async def __call__(
        self,
        request: Any,
        plan: Any,
        *,
        task_class: str,
        graph: Any,
        config: Mapping[str, Any] | None = None,
        root: str | Path = ".",
        mode: str = "ask",
    ) -> Any:
        from meristem.graph.harness_cycle import run_cycle_tick

        rows = await self._library(request.run_id)
        versions = _library_versions(rows, task_class)
        written: list[tuple[str, Mapping[str, Any]]] = []

        def write_envelope(envelope: Mapping[str, Any]) -> str:
            ref = _envelope_ref(envelope)
            written.append((ref, dict(envelope)))
            return ref

        outcome = await run_cycle_tick(
            request,
            plan,
            task_class=task_class,
            graph=graph,
            config=config,
            library_query=lambda _task_class: versions,
            write_envelope=write_envelope,
            root=root,
            mode=mode,
            agent_name=getattr(request, "agent_name", ""),
        )
        await self._flush(written, request.run_id)
        return outcome
