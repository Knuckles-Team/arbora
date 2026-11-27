"""Operator hooks: registered READ operations that vet input and output.

ARBORA-OPS-R009.6 to R009.9. A hook is a reference to a registered operation;
Arbora loads no code from the configuration file. The chain runs each hook in
declared order through the invoke pipeline itself, as the host service
principal, with hooks switched off for the hook call. A hook that raises,
times out or answers an unknown decision rejects the call with ``HOOK_FAILED``.
The records of the decisions name the hook, the point, the decision and the
duration; they never hold matched text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from arbora.api.registry import Effect, Registry
from arbora.security.operation_result import OpError, OpResult

__all__ = [
    "HOOKS_FILE",
    "HookChain",
    "HookConfigError",
    "HookInvoker",
    "HookOutcome",
    "HookPoint",
    "HookRecord",
    "HookSpec",
    "OPERATOR_HOOK_TIMEOUT_S",
    "Span",
    "apply_spans",
    "load_hooks",
]

HOOKS_FILE = "operator_hooks.json"
#: ``operator_hooks.timeout_s``: ``Fixed`` at two seconds.
OPERATOR_HOOK_TIMEOUT_S = 2.0
REDACTED_TEXT = "[redacted]"


class HookPoint(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


@dataclass(frozen=True, slots=True)
class Span:
    path: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class HookSpec:
    name: str
    point: HookPoint
    op_ref: str
    include_errors: bool = False


@dataclass(frozen=True, slots=True)
class HookRecord:
    """One decision. No matched text."""

    name: str
    point: str
    decision: str
    duration_ms: int

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "point": self.point,
            "decision": self.decision,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """The subject after the chain, or the error that ended the call."""

    subject: Any
    error: OpError | None = None
    records: tuple[HookRecord, ...] = ()


class HookConfigError(ValueError):
    """The hook configuration refuses the host start with ``hook_invalid``."""

    reason = "hook_invalid"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"hook_invalid: {detail}")


#: ``(op_ref, params, caller tenant, request id, surface)`` -> the hook outcome.
HookInvoker = Callable[
    [str, Mapping[str, Any], str, str, Any], Awaitable["OpResult | OpError"]
]


def _spec_from_row(row: Any) -> HookSpec:
    if not isinstance(row, Mapping):
        raise HookConfigError("a hook entry must be an object")
    name, point, op_ref = row.get("name"), row.get("point"), row.get("op_ref")
    if not all(isinstance(v, str) and v for v in (name, point, op_ref)):
        raise HookConfigError("a hook needs name, point and op_ref")
    try:
        hook_point = HookPoint(str(point))
    except ValueError as exc:
        raise HookConfigError(f"unknown hook point for {name}") from exc
    include = row.get("include_errors", False)
    if not isinstance(include, bool):
        raise HookConfigError(f"include_errors of {name} must be a boolean")
    return HookSpec(str(name), hook_point, str(op_ref), include)


#: One table of rules: each takes the spec, the registry and the specs before it.
def _rule_registered(
    spec: HookSpec, registry: Registry, _prior: Sequence[HookSpec]
) -> str | None:
    return None if registry.get(spec.op_ref) is not None else "op_ref is not registered"


def _rule_read_only(
    spec: HookSpec, registry: Registry, _prior: Sequence[HookSpec]
) -> str | None:
    op = registry.get(spec.op_ref)
    return None if op is None or op.effect == Effect.READ else "op_ref has an effect"


def _rule_not_a_hook(
    spec: HookSpec, _registry: Registry, prior: Sequence[HookSpec]
) -> str | None:
    if any(spec.op_ref == other.op_ref for other in prior):
        return "op_ref is already a hook"
    if any(spec.name == other.name for other in prior):
        return "name is already used"
    return None


_RULES = (_rule_registered, _rule_read_only, _rule_not_a_hook)


def load_hooks(
    registry: Registry,
    *,
    path: Path | None = None,
    timeout_s: float = OPERATOR_HOOK_TIMEOUT_S,
) -> HookChain:
    """Read ``operator_hooks.json`` and validate each hook against the registry."""
    if path is None:
        from arbora.core.settings import config_dir

        path = config_dir() / HOOKS_FILE
    if not path.exists():
        return HookChain((), timeout_s=timeout_s)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HookConfigError("the hook file is not readable JSON") from exc
    rows = document.get("hooks") if isinstance(document, Mapping) else None
    if not isinstance(rows, list):
        raise HookConfigError("the hook file needs a hooks list")
    specs: list[HookSpec] = []
    for row in rows:
        spec = _spec_from_row(row)
        for rule in _RULES:
            problem = rule(spec, registry, specs)
            if problem is not None:
                raise HookConfigError(f"{spec.name}: {problem}")
        specs.append(spec)
    return HookChain(tuple(specs), timeout_s=timeout_s)


def _walk(root: Any, path: str) -> tuple[Any, Any]:
    """The container and key that a dotted path names, or ``KeyError``."""
    node: Any = root
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    last = parts[-1]
    return node, (int(last) if isinstance(node, list) else last)


def apply_spans(subject: Any, spans: Sequence[Span]) -> Any:
    """Replace each named span of a string value by the fixed redaction text."""
    data = json.loads(json.dumps(subject, default=_plain))
    by_path: dict[str, list[Span]] = {}
    for span in spans:
        by_path.setdefault(span.path, []).append(span)
    for path, named in by_path.items():
        container, key = _walk(data, path)
        text = container[key]
        if not isinstance(text, str):
            raise TypeError("a span names a non-string value")
        for span in sorted(named, key=lambda s: s.start, reverse=True):
            if not 0 <= span.start <= span.end <= len(text):
                raise ValueError("span out of range")
            text = text[: span.start] + REDACTED_TEXT + text[span.end :]
        container[key] = text
    return data


def _plain(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError("not JSON-shaped")


def _decision(value: Any) -> tuple[str, tuple[Span, ...], str | None]:
    """Parse one hook answer into ``(kind, spans, code)``; anything else raises."""
    if not isinstance(value, Mapping):
        raise ValueError("hook answer is not an object")
    kind = value.get("decision")
    if kind == "allow":
        return "allow", (), None
    if kind == "redact":
        rows = value.get("spans")
        if not isinstance(rows, list) or not rows:
            raise ValueError("redact needs spans")
        spans = []
        for row in rows:
            if not (
                isinstance(row, Mapping)
                and isinstance(row.get("path"), str)
                and type(row.get("start")) is int
                and type(row.get("end")) is int
            ):
                raise ValueError("invalid span")
            spans.append(Span(row["path"], row["start"], row["end"]))
        return "redact", tuple(spans), None
    if kind == "reject":
        code = value.get("code")
        return "reject", (), code if isinstance(code, str) and code else None
    raise ValueError("unknown hook decision")


@dataclass(slots=True)
class HookChain:
    """The ordered hooks and the invoker that runs them."""

    hooks: tuple[HookSpec, ...] = ()
    timeout_s: float = OPERATOR_HOOK_TIMEOUT_S
    clock: Callable[[], float] = time.monotonic
    invoker: HookInvoker | None = field(default=None, repr=False)

    def bind(self, invoker: HookInvoker) -> None:
        self.invoker = invoker

    def __bool__(self) -> bool:
        return bool(self.hooks)

    async def run_input(
        self,
        op_id: str,
        params: Mapping[str, Any],
        tenant: str,
        request_id: str,
        surface: Any,
    ) -> HookOutcome:
        return await self._run(
            HookPoint.INPUT, op_id, params, tenant, request_id, surface
        )

    async def run_output(
        self,
        op_id: str,
        result: OpResult | OpError,
        tenant: str,
        request_id: str,
        surface: Any,
    ) -> HookOutcome:
        """Run the output hooks on a result; an error passes only ``include_errors``."""
        is_error = isinstance(result, OpError)
        subject: Any = (
            {"code": result.code, "details": dict(result.details)}
            if isinstance(result, OpError)
            else result.value
        )
        outcome = await self._run(
            HookPoint.OUTPUT,
            op_id,
            subject,
            tenant,
            request_id,
            surface,
            errors=is_error,
        )
        return outcome

    async def _run(
        self,
        point: HookPoint,
        op_id: str,
        subject: Any,
        tenant: str,
        request_id: str,
        surface: Any,
        *,
        errors: bool = False,
    ) -> HookOutcome:
        records: list[HookRecord] = []
        for hook in self.hooks:
            if hook.point != point or (errors and not hook.include_errors):
                continue
            started = self.clock()
            try:
                kind, spans, code = await self._ask(
                    hook, op_id, subject, tenant, request_id, surface
                )
            except Exception:
                logging.getLogger(__name__).debug(
                    "every failure rejects, closed", exc_info=True
                )
                records.append(self._record(hook, "failed", started))
                return HookOutcome(
                    subject, OpError("HOOK_FAILED", {"hook": hook.name}), tuple(records)
                )
            records.append(self._record(hook, kind, started))
            if kind == "reject":
                return HookOutcome(
                    subject,
                    OpError(code or "HOOK_REJECTED", {"hook": hook.name}),
                    tuple(records),
                )
            if kind == "redact":
                try:
                    subject = apply_spans(subject, spans)
                except Exception:
                    logging.getLogger(__name__).debug(
                        "an unusable span rejects, closed", exc_info=True
                    )
                    records[-1] = self._record(hook, "failed", started)
                    return HookOutcome(
                        subject,
                        OpError("HOOK_FAILED", {"hook": hook.name}),
                        tuple(records),
                    )
        return HookOutcome(subject, None, tuple(records))

    def _record(self, hook: HookSpec, decision: str, started: float) -> HookRecord:
        elapsed = max(0.0, self.clock() - started)
        return HookRecord(hook.name, hook.point.value, decision, int(elapsed * 1000))

    async def _ask(
        self,
        hook: HookSpec,
        op_id: str,
        subject: Any,
        tenant: str,
        request_id: str,
        surface: Any,
    ) -> tuple[str, tuple[Span, ...], str | None]:
        if self.invoker is None:
            raise RuntimeError("hook invoker unbound")
        key = "params_json" if hook.point == HookPoint.INPUT else "result_json"
        payload = {
            "op_id": op_id,
            "caller_tenant": tenant,
            key: json.dumps(subject, default=_plain, sort_keys=True),
        }
        answer = await asyncio.wait_for(
            self.invoker(hook.op_ref, payload, tenant, request_id, surface),
            timeout=self.timeout_s,
        )
        if isinstance(answer, OpError) or answer.code != "OK":
            raise RuntimeError("hook call refused")
        return _decision(answer.value)


def host_hook_invoker(
    services: Any, caller_for: Callable[[str, str], Any]
) -> HookInvoker:
    """Run a hook through ``invoke`` as the host service principal.

    ``caller_for(tenant, request_id)`` is the composition root's service
    caller. The hook call has hooks switched off and the original request id.
    """

    async def run(
        op_ref: str,
        params: Mapping[str, Any],
        tenant: str,
        request_id: str,
        surface: Any,
    ) -> OpResult | OpError:
        from arbora.api.invoke.pipeline import invoke

        caller = caller_for(tenant, request_id)
        return await invoke(
            op_ref, params, caller, surface, services=services, hooks_enabled=False
        )

    return run
