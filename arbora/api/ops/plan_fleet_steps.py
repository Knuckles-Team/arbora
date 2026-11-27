"""The fleet tools one plan step calls: bound items, idempotency keys, replay and the governed caller (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pydantic import Field
from pydantic_ai import RunContext

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params
from arbora.api.registry import (
    Effect,
)

#: The one argument a foreach step shows the harness: its resolved source array.
FOREACH_SOURCE_ARGUMENT = "items"


#: Step failures that are refusals rather than failed calls (the harness outcome).
_REFUSED_STEP_CODES = frozenset(
    {"SCOPE_DENIED", "CONFIRMATION_REQUIRED", "STEP_UP_REQUIRED", "UNAVAILABLE"}
)


class FleetToolRef(Params):
    """One fleet tool a plan's agent steps may call."""

    server: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)


def step_idempotency_key(record_id: str, task_id: str, index: int) -> str:
    """The key of one step call: the plan run's replay record, task and index.

    ``record_id`` is the run's replay record (:func:`replay_record`). A replay
    of the same served call makes the same calls with the same keys, so the
    effect journal returns the recorded outcome and runs no second effect.
    """
    return f"plan-step:{record_id}:{task_id}:{index}"


def replay_record(caller: Any) -> str:
    """The plan run's replay record: the pipeline-bound request identity.

    The invoke pipeline binds a governed call's ``request_id`` to the tenant,
    the principal, the operation and the caller's idempotency key, so a replay
    of the same served call carries the same value. The engine decision record
    id is not stable: each ``Decide`` stamps its own ``created_at_ms``.
    """
    request_id = getattr(caller, "request_id", None)
    if not isinstance(request_id, str) or not request_id:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the plan run has no bound replay identity"}
        )
    return f"run-{request_id[:32]}"


#: The replay owner of a served plan run (ARBORA-RELEASE-R003.4.11). A run's only
#: external effects are its step ``fleet.call`` effects; each is reserved in the
#: EG effect journal under ``plan-step:<record>:<task>:<idx>``, where
#: ``<record>`` derives from the run's own idempotency key. The engine
#: ``TaskPlan`` decision is evaluate-only, and ``PublishCurrentPlan`` is keyed by
#: the run and the plan digest. A replayed run therefore returns each recorded
#: step outcome and starts no second effect.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "agents.plan_run": "arbora.api.domain.mcp_server.effect_journal:EgEffectJournal",
    }
)


def _audit_fields(outcome: Any) -> dict[str, Any]:
    details = getattr(outcome, "details", None)
    ref = details.get("audit_ref") if isinstance(details, Mapping) else None
    return {"audit_ref": ref} if ref else {}


@dataclass
class FleetStepTools:
    """The governed fleet tool an agent step may call (ARBORA-RELEASE-R003.4.5).

    Each call goes through the invoke pipeline as ``fleet.call``: the same
    effect authority, audit reservation and effect journal as any other
    governed write. A tool the caller did not name, a tool that needs a plan
    confirmation or a console step-up, and a tool whose scopes the caller lacks
    are refused as data for the step, never run.
    """

    context: Any
    allowed: frozenset[tuple[str, str]]
    record_id: str = ""
    foreach: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    _counts: dict[str, int] = field(default_factory=dict)

    async def call(
        self,
        task_id: str,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        index: int | None = None,
        guard: Callable[[Any], str | None] | None = None,
    ) -> dict[str, Any]:
        index = self._next_index(task_id, index)
        key = step_idempotency_key(self.record_id, task_id, index)
        entry: dict[str, Any] = {
            "sequence": len(self.ledger) + 1,
            "task_id": task_id,
            "index": index,
            "idempotency_key": key,
            "server": server,
            "tool": tool,
            "arguments": arguments,
        }
        self.ledger.append(entry)
        try:
            return await self._invoke(entry, key, guard)
        except OperationRefused as exc:
            entry.update(ok=False, error=exc.code, details=exc.details)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "the step sees the failure as data", exc_info=True
            )
            entry.update(ok=False, error=type(exc).__name__)
        return {"ok": False, "error": entry["error"]}

    def _next_index(self, task_id: str, index: int | None) -> int:
        if index is not None:
            return index
        self._counts[task_id] = self._counts.get(task_id, 0) + 1
        return self._counts[task_id]

    async def _decide(self, entry: dict[str, Any], guard: Any) -> Any:
        """The effect decision of one call; a refusal raises before any effect."""
        from arbora.api.ops.research import FleetCaller
        from arbora.api.registry import Confirm

        server, tool, arguments = entry["server"], entry["tool"], entry["arguments"]
        if (server, tool) not in self.allowed:
            raise OperationRefused(
                "SCOPE_DENIED", {"reason": "tool is not named in fleet_tools"}
            )
        if self.context.services.get("plan_step_invoke") is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the invoke pipeline is not bound"}
            )
        decision = await FleetCaller(self.context).decision(server, tool, arguments)
        if decision.confirm != Confirm.NONE:
            raise OperationRefused(
                "CONFIRMATION_REQUIRED",
                {"reason": "the tool needs a human-confirmed plan"},
            )
        refusal = guard(decision) if guard is not None else None
        if refusal is not None:
            raise OperationRefused(refusal)
        return decision

    async def _invoke(
        self, entry: dict[str, Any], key: str, guard: Any
    ) -> dict[str, Any]:
        from arbora.security.operation_result import OpError

        await self._decide(entry, guard)
        outcome = await self.context.services["plan_step_invoke"](
            "fleet.call",
            {
                "server": entry["server"],
                "tool": entry["tool"],
                "arguments": entry["arguments"],
            },
            self.context.caller,
            key,
        )
        if isinstance(outcome, OpError) or outcome.code != "OK":
            entry.update(ok=False, error=outcome.code, details=dict(outcome.details))
            return {"ok": False, "error": outcome.code}
        value = outcome.value
        result = value.get("value") if isinstance(value, Mapping) else value
        entry["result"] = json.loads(json.dumps(result, default=str))
        entry["ok"] = True
        entry.update(_audit_fields(outcome))
        return {"ok": True, "result": entry["result"]}

    async def run_step(
        self, task_id: str, server: str, tool: str, arguments: Mapping[str, Any]
    ) -> Any:
        """One typed tool step as the harness caller sees it (R003.4.9)."""
        from meristem.graph import ToolStepResult

        spec = self.foreach.get(task_id)
        if spec is None:
            done = await self.call(task_id, server, tool, dict(arguments))
        else:
            done = await self._foreach(task_id, server, tool, arguments, spec)
        if done["ok"]:
            return ToolStepResult(result=done["result"])
        code = str(done["error"])
        outcome = "refused" if code in _REFUSED_STEP_CODES else "failed"
        return ToolStepResult(outcome=outcome, error=code)

    def _refuse_item(self, task_id: str, index: int, code: str) -> dict[str, Any]:
        self.ledger.append(
            {
                "sequence": len(self.ledger) + 1,
                "task_id": task_id,
                "index": index,
                "item_index": index,
                "ok": False,
                "error": code,
            }
        )
        return {"ok": False, "error": code}

    async def _foreach(
        self,
        task_id: str,
        server: str,
        tool: str,
        arguments: Mapping[str, Any],
        spec: Mapping[str, Any],
    ) -> dict[str, Any]:
        """One call per array item, in item order (ARBORA-RELEASE-R003.4.14, .15)."""

        items = arguments.get(FOREACH_SOURCE_ARGUMENT)
        if not isinstance(items, list):
            return {"ok": False, "error": "FOREACH_SOURCE_NOT_ARRAY"}
        if len(items) > int(spec["max_items"]):
            return {"ok": False, "error": "FOREACH_ITEM_LIMIT"}
        seen: set[str] = set()
        results: list[Any] = []
        for position, item in enumerate(items):
            try:
                bound = _bind_item(spec["arguments"], item, position)
            except (KeyError, IndexError, TypeError):
                return self._refuse_item(task_id, position, "FOREACH_ITEM_UNRESOLVED")
            digest = hashlib.sha256(
                json.dumps(
                    {"server": server, "tool": tool, "arguments": bound},
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            ).hexdigest()

            def duplicate_write(decision: Any, digest: str = digest) -> str | None:
                if decision.effect == Effect.WRITE and digest in seen:
                    return "FOREACH_DUPLICATE_WRITE"
                return None

            done = await self.call(
                task_id,
                server,
                tool,
                bound,
                index=position,
                guard=duplicate_write,
            )
            self.ledger[-1]["item_index"] = position
            seen.add(digest)
            if not done["ok"]:
                return done
            results.append(done["result"])
        return {"ok": True, "result": results}

    def toolset(self) -> Any:
        from pydantic_ai.toolsets import FunctionToolset

        async def fleet_call(
            ctx: RunContext[Any], server: str, tool: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            """Call one governed fleet tool. Returns {ok, result} or {ok, error}."""
            task_id = str(getattr(ctx.deps, "task_id", None) or "task")
            return await self.call(task_id, server, tool, arguments)

        return FunctionToolset([fleet_call])


def _item_path(item: Any, tokens: list[str]) -> Any:
    found = item
    for token in tokens:
        if isinstance(found, Mapping):
            found = found[token]
        elif isinstance(found, list) and token.isdigit():
            found = found[int(token)]
        else:
            raise TypeError(token)
    return found


def _item_ref(reference: str, item: Any, index: int) -> Any:
    if reference == "item_index":
        return index
    if not reference.startswith("item."):
        raise KeyError(reference)
    return _item_path(item, reference[len("item.") :].split("."))


def _bind_item(value: Any, item: Any, index: int) -> Any:
    """Resolve ``item.<path>`` and ``item_index`` references of one foreach call.

    Any other binding inside a foreach call is not resolved here (the harness
    resolves only the source), so it is refused rather than sent as text.
    """
    if isinstance(value, list | tuple):
        return [_bind_item(child, item, index) for child in value]
    if not isinstance(value, Mapping):
        return value
    if set(value) == {"$ref"}:
        return _item_ref(str(value["$ref"]), item, index)
    return {key: _bind_item(child, item, index) for key, child in value.items()}


def _plan_calls(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [t for t in plan["tasks"] if isinstance(t.get("call"), Mapping)]


def _foreach_specs(calls: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        t["task_id"]: {
            "max_items": t["foreach"]["max_items"],
            "arguments": t["call"].get("arguments") or {},
        }
        for t in calls
        if isinstance(t.get("foreach"), Mapping)
    }


def _allowed_tools(
    refs: list[FleetToolRef], calls: list[Mapping[str, Any]]
) -> frozenset[tuple[str, str]]:
    named = {(r.server, r.tool) for r in refs}
    return frozenset(named | {(t["call"]["server"], t["call"]["tool"]) for t in calls})


def _fleet_step_tools(
    context: Any,
    refs: list[FleetToolRef],
    caller: Any,
    plan: Mapping[str, Any],
) -> FleetStepTools | None:
    """The governed tools of one run: the named ``fleet_tools`` and the plan's calls.

    A plan that declares its own typed calls names them by writing them; each
    still goes through the invoke pipeline under the caller's authority.
    """
    calls = _plan_calls(plan)
    if not (refs or calls):
        return None
    return FleetStepTools(
        context,
        _allowed_tools(refs, calls),
        replay_record(caller),
        foreach=_foreach_specs(calls),
    )


def _step_toolsets(fleet: FleetStepTools | None) -> tuple:
    """The toolsets a plan's agent steps run with: the fleet tools, or none."""
    return (fleet.toolset(),) if fleet is not None else ()


def _fleet_calls(fleet: FleetStepTools | None) -> list[dict[str, Any]]:
    """The recorded fleet calls of one plan run, in call order."""
    return list(fleet.ledger) if fleet is not None else []


#: The governed caller of the run in flight. The harness holds one process-wide
#: tool-caller slot; each run reads its own caller from this context variable.
_RUN_TOOLS: ContextVar[FleetStepTools | None] = ContextVar(
    "plan_run_tools", default=None
)


async def governed_tool_caller(
    server: str, tool: str, arguments: Mapping[str, Any], task_id: str
) -> Any:
    """The harness ``ToolCaller``: route a typed step to the run's governed tools."""
    from meristem.graph import ToolStepResult

    fleet = _RUN_TOOLS.get()
    if fleet is None:
        return ToolStepResult(outcome="refused", error="UNAVAILABLE")
    return await fleet.run_step(task_id, server, tool, arguments)
