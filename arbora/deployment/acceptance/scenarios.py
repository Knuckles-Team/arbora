"""The scenario interpreter: declarative steps, a ``needs`` DAG and expectations.

A scenario file is YAML: ``name`` and ``steps``. Each step is
``{id, call: <operation id>, args, verb, needs: [step ids], expect: {path, op, value}}``.
The interpreter holds no system name: what a scenario exercises is data. A step runs
once every step it needs has passed; a step whose need failed is reported FAIL
(``blocked``) and its call is never made. ``${step.path}`` inside ``args`` is replaced
by a value read from an earlier step's result.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .runner import ResultWriter, err, excerpt, ok, unwrap

#: ``call(verb, op, params)`` returns the gateway envelope.
Caller = Callable[..., Awaitable[dict[str, Any]]]
DEFAULT_VERB = "ask"
_VERBS = frozenset({"find", "ask", "why", "write", "act", "manage"})
_OPERATORS = frozenset({"eq", "ne", "contains", "exists", "gt", "lt", "in", "nonempty"})
_REFERENCE = re.compile(r"\$\{([A-Za-z0-9_.\-\[\]]+)\}")
_STEP_KEYS = frozenset({"id", "call", "args", "verb", "needs", "expect"})


class ScenarioError(ValueError):
    """A scenario file is malformed; the message names the step and field."""


@dataclass(frozen=True)
class Expectation:
    path: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class Step:
    id: str
    call: str
    args: dict[str, Any] = field(default_factory=dict)
    verb: str = DEFAULT_VERB
    needs: tuple[str, ...] = ()
    expect: Expectation | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    steps: tuple[Step, ...]


def _expectation(raw: Any, where: str) -> Expectation | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"path", "op", "value"}:
        raise ScenarioError(f"{where}: expect takes path, op and value")
    op = raw.get("op", "eq")
    if op not in _OPERATORS:
        raise ScenarioError(f"{where}: unknown expect op {op!r}")
    return Expectation(str(raw.get("path", "")), op, raw.get("value"))


def parse_scenario(raw: Any, source: str) -> Scenario:
    if not isinstance(raw, dict) or set(raw) - {"name", "steps"}:
        raise ScenarioError(f"{source}: a scenario holds name and steps")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ScenarioError(f"{source}: name must be a non-empty string")
    steps: list[Step] = []
    for index, item in enumerate(raw.get("steps") or []):
        where = f"{source}: steps[{index}]"
        if not isinstance(item, dict) or set(item) - _STEP_KEYS:
            raise ScenarioError(f"{where}: unknown step fields")
        if not isinstance(item.get("id"), str) or not isinstance(item.get("call"), str):
            raise ScenarioError(f"{where}: id and call are required strings")
        verb = item.get("verb", DEFAULT_VERB)
        if verb not in _VERBS:
            raise ScenarioError(f"{where}: unknown verb {verb!r}")
        steps.append(
            Step(
                id=item["id"],
                call=item["call"],
                args=dict(item.get("args") or {}),
                verb=verb,
                needs=tuple(item.get("needs") or ()),
                expect=_expectation(item.get("expect"), where),
            )
        )
    ids = [s.id for s in steps]
    if len(set(ids)) != len(ids):
        raise ScenarioError(f"{source}: duplicate step id")
    for step in steps:
        missing = [n for n in step.needs if n not in ids]
        if missing:
            raise ScenarioError(f"{source}: {step.id} needs unknown step {missing[0]}")
    _reject_cycle(steps, source)
    return Scenario(name, tuple(steps))


def _reject_cycle(steps: list[Step], source: str) -> None:
    needs = {s.id: set(s.needs) for s in steps}
    resolved: set[str] = set()
    while needs:
        ready = [i for i, deps in needs.items() if deps <= resolved]
        if not ready:
            raise ScenarioError(f"{source}: the needs graph has a cycle")
        for item in ready:
            resolved.add(item)
            del needs[item]


def load_scenarios(directory: Path) -> dict[str, Scenario]:
    """Every ``*.yml`` scenario under ``directory``, by name, in file order."""
    out: dict[str, Scenario] = {}
    for path in sorted(directory.glob("*.yml")):
        scenario = parse_scenario(
            yaml.safe_load(path.read_text(encoding="utf-8")), path.name
        )
        if scenario.name in out:
            raise ScenarioError(f"{path.name}: duplicate scenario {scenario.name}")
        out[scenario.name] = scenario
    return out


def read_path(value: Any, path: str) -> tuple[bool, Any]:
    """Read ``a.b[0].c`` from nested data; the flag says whether the path exists."""
    current = value
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            index = int(part[1:-1])
            if not isinstance(current, list) or index >= len(current):
                return False, None
            current = current[index]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def check(expectation: Expectation, value: Any) -> tuple[bool, str]:
    """Evaluate ``expectation`` over a step's unwrapped result."""
    found, actual = (
        read_path(value, expectation.path) if expectation.path else (True, value)
    )
    op, want = expectation.op, expectation.value
    if op == "exists":
        passed = found
    elif not found:
        passed = False
    elif op == "eq":
        passed = actual == want
    elif op == "ne":
        passed = actual != want
    elif op == "contains":
        passed = want in actual if isinstance(actual, (str, list, dict)) else False
    elif op == "in":
        passed = actual in want if isinstance(want, (list, str, dict)) else False
    elif op == "nonempty":
        passed = bool(actual)
    elif op == "gt":
        passed = isinstance(actual, (int, float)) and actual > want
    else:
        passed = isinstance(actual, (int, float)) and actual < want
    path = expectation.path or "<result>"
    return (
        passed,
        f"{path} {op} {excerpt(want, 40)}; got {excerpt(actual, 60) if found else 'absent'}",
    )


def substitute(value: Any, results: dict[str, Any]) -> Any:
    """Replace ``${step.path}`` references with values from earlier results."""
    if isinstance(value, dict):
        return {k: substitute(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, results) for v in value]
    if not isinstance(value, str):
        return value
    whole = _REFERENCE.fullmatch(value)

    def lookup(reference: str) -> Any:
        step, _, path = reference.partition(".")
        found, actual = (
            read_path(results.get(step), path)
            if path
            else (step in results, results.get(step))
        )
        if not found:
            raise ScenarioError(f"reference ${{{reference}}} does not resolve")
        return actual

    if whole:
        return lookup(whole.group(1))
    return _REFERENCE.sub(lambda m: str(lookup(m.group(1))), value)


async def run_scenario(
    scenario: Scenario,
    call: Caller,
    writer: ResultWriter,
    concurrency: int = 4,
) -> None:
    """Run ``scenario``'s DAG; one row per step."""
    results: dict[str, Any] = {}
    passed: dict[str, bool] = {}
    gate = asyncio.Semaphore(concurrency)
    done: dict[str, asyncio.Event] = {s.id: asyncio.Event() for s in scenario.steps}

    async def run_step(step: Step) -> None:
        for need in step.needs:
            await done[need].wait()
        try:
            blocked = [n for n in step.needs if not passed[n]]
            if blocked:
                writer.row(
                    scenario.name,
                    step.id,
                    step.call,
                    "FAIL",
                    f"blocked by {blocked[0]}",
                )
                passed[step.id] = False
                return
            try:
                params = substitute(step.args, results)
            except ScenarioError as exc:
                writer.row(scenario.name, step.id, step.call, "FAIL", str(exc))
                passed[step.id] = False
                return
            try:
                async with gate:
                    envelope = await call(step.verb, step.call, params)
            except Exception as exc:  # noqa: BLE001 - a transport fault is a FAIL row
                writer.row(
                    scenario.name, step.id, step.call, "FAIL", type(exc).__name__
                )
                passed[step.id] = False
                return
            if not ok(envelope):
                writer.row(scenario.name, step.id, step.call, "FAIL", err(envelope))
                passed[step.id] = False
                return
            value = unwrap(envelope)
            results[step.id] = value
            good, evidence = (True, "ok")
            if step.expect is not None:
                good, evidence = check(step.expect, value)
            writer.row(scenario.name, step.id, step.call, good, evidence)
            passed[step.id] = good
        finally:
            done[step.id].set()

    await asyncio.gather(*(run_step(step) for step in scenario.steps))
