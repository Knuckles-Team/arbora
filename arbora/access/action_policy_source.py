"""Strict source snapshots for the canonical ActionPolicy owner.

A source revision is descriptive evidence, never an action grant. Legacy
best-effort policy loading remains unchanged; opted-in boundaries use this
stricter source contract before requesting an exact canonical decision.
"""

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


def qualified_policy_source(policy: Any) -> _QualifiedPolicySource:
    """Capture complete source evidence without changing decisions or state."""
    from arbora.access.action_policy import _canonical_policy_value, _coerce_rule

    try:
        document = _configured_document(policy.policy_path)
        for raw in document["rules"]:
            _validate_rule(raw)
            if _coerce_rule(raw, origin="file") is None:
                raise RuntimeError("Malformed configured policy rule")
        overrides = _override_rules(policy.engine)
        normalized = json.dumps(
            _canonical_policy_value(
                {
                    "schema": "action-policy-source.v1",
                    "configuration_origin": "configured_file",
                    "file": document,
                    "graph_overrides": [asdict(rule) for rule in overrides],
                    "irreversibility_aversion": policy._irreversibility_aversion,
                }
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        revision = "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001 - redact source failures before refusal
        normalized = None
    if normalized is None:
        # Raise outside the handler: even __context__ must not retain policy data.
        raise RuntimeError("Qualified configured action policy unavailable")
    return _QualifiedPolicySource(
        revision=revision,
        configuration_origin="configured_file",
        normalized_document=normalized,
    )
