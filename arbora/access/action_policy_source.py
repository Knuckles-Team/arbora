"""Strict source snapshots for the canonical ActionPolicy owner.

A source revision is descriptive evidence, never an action grant. Legacy
best-effort policy loading remains unchanged; opted-in boundaries use this
stricter source contract before requesting an exact canonical decision.
"""

import asyncio
import contextvars
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_MAX_POLICY_BYTES = 1_048_576
_MAX_OVERRIDE_RULES = 100


@dataclass(frozen=True, slots=True)
class _QualifiedPolicySource:
    revision: str
    configuration_origin: str
    normalized_document: str = field(repr=False)

    @property
    def irreversibility_aversion(self) -> bool:
        return bool(json.loads(self.normalized_document)["irreversibility_aversion"])

    def inputs(self) -> tuple[dict[str, Any], list[Any]]:
        from arbora.access.action_policy import ActionRule

        data = json.loads(self.normalized_document)
        return data["file"], [ActionRule(**rule) for rule in data["graph_overrides"]]


def _configured_document(path: Path | None) -> dict[str, Any]:
    if path is None:
        raise RuntimeError("Explicitly configured action policy required")
    import yaml

    with path.open("rb") as stream:
        raw = stream.read(_MAX_POLICY_BYTES + 1)
    if len(raw) > _MAX_POLICY_BYTES:
        raise RuntimeError("Configured action policy exceeds the size bound")
    data = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise RuntimeError("Configured action policy rules are unavailable")
    return data


def _override_rules(engine: Any) -> list[Any]:
    if engine is None:
        raise RuntimeError("Canonical policy override authority required")
    rows = engine.query_cypher(
        "MATCH (r:governance_rule {scope: 'action_policy'}) RETURN r LIMIT 101"
    )
    return _rules_from_rows(rows)


def _rules_from_rows(rows: Any) -> list[Any]:
    if not isinstance(rows, list) or len(rows) > _MAX_OVERRIDE_RULES:
        raise RuntimeError("Complete bounded policy overrides required")
    rules = []
    for row in rows:
        rule = _override_rule(row)
        if rule is not None:
            rules.append(rule)
    rules.sort(key=lambda rule: -rule.priority)
    return rules


def _override_rule(row: Any) -> Any:
    from arbora.access.action_policy import _coerce_rule

    props = row.get("r") if isinstance(row, dict) else None
    if not isinstance(props, dict):
        raise RuntimeError("Malformed policy override")
    if str(props.get("active", "true")).lower() in {"false", "0", "no"}:
        return None
    raw = props
    if props.get("rule_json"):
        payload = json.loads(props["rule_json"])
        if not isinstance(payload, dict):
            raise RuntimeError("Malformed policy override payload")
        raw = {**props, **payload}
    _validate_rule(raw)
    rule = _coerce_rule(raw, origin="kg")
    if rule is None:
        raise RuntimeError("Malformed effective policy override")
    return rule


def _validate_rule(raw: Any) -> None:
    from arbora.access.action_policy import _TIERS, TIER_APPROVAL

    if not isinstance(raw, dict):
        raise RuntimeError("Malformed configured policy rule")
    tier = raw.get("tier") or TIER_APPROVAL
    if not isinstance(tier, str) or tier not in _TIERS:
        raise RuntimeError("Unknown configured policy tier")


def _validated_document(path: Path | None) -> dict[str, Any]:
    from arbora.access.action_policy import _coerce_rule

    document = _configured_document(path)
    for raw in document["rules"]:
        _validate_rule(raw)
        if _coerce_rule(raw, origin="file") is None:
            raise RuntimeError("Malformed configured policy rule")
    return document


def _source_from_inputs(
    document: dict[str, Any], overrides: list[Any], aversion: bool
) -> _QualifiedPolicySource:
    from arbora.access.action_policy import _canonical_policy_value

    normalized = json.dumps(
        _canonical_policy_value(
            {
                "schema": "action-policy-source.v1",
                "configuration_origin": "configured_file",
                "file": document,
                "graph_overrides": [asdict(rule) for rule in overrides],
                "irreversibility_aversion": aversion,
            }
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _QualifiedPolicySource(
        revision="sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        configuration_origin="configured_file",
        normalized_document=normalized,
    )


def qualified_policy_source(policy: Any) -> _QualifiedPolicySource:
    """Capture complete source evidence without changing decisions or state."""
    try:
        source = _source_from_inputs(
            _validated_document(policy.policy_path),
            _override_rules(policy.engine),
            policy._irreversibility_aversion,
        )
    except Exception:  # noqa: BLE001 - redact source failures before refusal
        source = None
    if source is None:
        raise RuntimeError("Qualified configured action policy unavailable")
    return source


async def _retained_file_document(path: Path | None) -> dict[str, Any]:
    """Cancellation retains this task until its exact file worker completes."""
    context = contextvars.copy_context()
    worker = asyncio.get_running_loop().run_in_executor(
        None, context.run, _validated_document, path
    )
    cancelled = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            cancelled = error
        except Exception:  # noqa: BLE001 - retrieve and redact in source owner
            break
    if cancelled is not None:
        # Retrieve any failure without leaking it through the cancellation chain.
        if not worker.cancelled():
            worker.exception()
        raise cancelled
    return worker.result()


def _check_selection(policy: Any, engine: Any, path: Any, aversion: bool) -> None:
    if (
        policy.engine is not engine
        or policy.policy_path != path
        or policy._irreversibility_aversion != aversion
    ):
        raise RuntimeError("Configured policy selection changed")


async def qualified_policy_source_async(policy: Any) -> _QualifiedPolicySource:
    """Use one captured caller and source selection; issue no effect authority.

    Callers must retain capacity until this task completes, even if cancelled.
    A stalled file worker therefore denies capacity rather than accumulating
    detached workers. File latency and parsing CPU are not wall-clock bounded.
    """
    from arbora.api.contracts.session import resolve_session

    try:
        engine, path = policy.engine, policy.policy_path
        aversion = policy._irreversibility_aversion
        caller = resolve_session(None, required_scope="kg:read")
        document = await _retained_file_document(path)
        _check_selection(policy, engine, path, aversion)
        resolve_session(caller, required_scope="kg:read")
        rows = await engine.query_cypher_async(
            "MATCH (r:governance_rule {scope: 'action_policy'}) RETURN r LIMIT 101",
            session=caller,
        )
        _check_selection(policy, engine, path, aversion)
        resolve_session(caller, required_scope="kg:read")
        source = _source_from_inputs(document, _rules_from_rows(rows), aversion)
    except Exception:  # noqa: BLE001 - redact source failures before refusal
        source = None
    if source is None:
        raise RuntimeError("Qualified configured action policy unavailable")
    return source
