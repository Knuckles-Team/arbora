"""Map an OTLP/JSON logs body to skill invocation events (ARBORA-OPS-R953).

Only the nine ``skill.*`` and ``session.run_id`` attributes are accepted. Any
other attribute or any log body text refuses the whole request with
``TELEMETRY_FIELD_UNKNOWN``, so a hook can never carry a prompt, an argument
or an output. The mapper never sets the principal pseudonym: the engine
derives it from the verified principal for a human caller.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

MAX_RECORDS = 500
_ATTRIBUTES = frozenset(
    {
        "skill.ref",
        "skill.version",
        "skill.chosen_by",
        "session.run_id",
        "skill.outcome",
        "skill.error_code",
        "skill.cost_delta_micro_usd",
        "skill.tokens_in",
        "skill.tokens_out",
    }
)


class TelemetryFieldUnknown(ValueError):
    """A record carried a field outside the accepted list."""


def _value(raw: Mapping[str, Any]) -> Any:
    for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if kind in raw:
            return raw[kind]
    raise TelemetryFieldUnknown("attribute value kind")


def _attributes(record: Mapping[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for item in record.get("attributes") or ():
        key = item.get("key")
        if key not in _ATTRIBUTES:
            raise TelemetryFieldUnknown("attribute")
        found[key] = _value(item.get("value") or {})
    return found


def _whole(value: Any) -> int:
    return int(value or 0)


def _event(tenant: str, record: Mapping[str, Any]) -> dict[str, Any]:
    if record.get("body"):
        raise TelemetryFieldUnknown("body")
    attrs = _attributes(record)
    try:
        ref, version = attrs["skill.ref"], attrs["skill.version"]
        run_id, outcome = attrs["session.run_id"], attrs["skill.outcome"]
        ended_ms = int(record["timeUnixNano"]) // 1_000_000
    except KeyError:
        raise TelemetryFieldUnknown("missing attribute") from None
    # A log record carries one instant; the invocation is stamped with it at
    # both ends, so a duration is never invented.
    event: dict[str, Any] = {
        "tenant_id": tenant,
        "skill_ref": ref,
        "skill_version": version,
        "chosen_by": "user" if attrs.get("skill.chosen_by") == "user" else "agent",
        "run_id": run_id,
        "started_ms": ended_ms,
        "ended_ms": ended_ms,
        "outcome": (
            {"error": {"code": str(attrs.get("skill.error_code") or "UNKNOWN")}}
            if outcome == "error"
            else "ok"
        ),
        "tokens_in": _whole(attrs.get("skill.tokens_in")),
        "tokens_out": _whole(attrs.get("skill.tokens_out")),
    }
    if "skill.cost_delta_micro_usd" in attrs:
        event["cost_delta_micro_usd"] = int(attrs["skill.cost_delta_micro_usd"])
    seed = f"{tenant}|{run_id}|{ref}|{version}|{ended_ms}|{outcome}"
    event["invocation_id"] = "inv:" + hashlib.sha256(seed.encode()).hexdigest()[:32]
    return event


def events_from_logs(
    tenant: str, resource_logs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Every log record as one event; one bad record refuses all of them."""
    records = [
        record
        for resource in resource_logs
        for scope in resource.get("scopeLogs") or ()
        for record in scope.get("logRecords") or ()
    ]
    if len(records) > MAX_RECORDS:
        raise TelemetryFieldUnknown("too many records")
    return [_event(tenant, record) for record in records]


def body_key(params: Mapping[str, Any]) -> str:
    """An idempotency key for an exporter that sends none: the body's digest."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return "otlp:" + hashlib.sha256(canonical.encode()).hexdigest()
