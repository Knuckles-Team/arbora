"""Hook operation handlers the served hot-path test dispatches by dotted name."""

from __future__ import annotations

import json
from typing import Any

SEEN: list[tuple[str, str]] = []


async def hook_input(context: Any, params: Any, op: Any) -> dict[str, Any]:
    if params["op_id"] != "memory.write":
        return {"decision": "allow"}
    subject = json.loads(params["params_json"])
    SEEN.append(("one", subject["value"]["text"]))
    start = subject["value"]["text"].index("secret")
    return {
        "decision": "redact",
        "spans": [{"path": "value.text", "start": start, "end": start + 6}],
    }


async def hook_second(context: Any, params: Any, op: Any) -> dict[str, Any]:
    if params["op_id"] != "memory.write":
        return {"decision": "allow"}
    subject = json.loads(params["params_json"])
    SEEN.append(("two", subject["value"]["text"]))
    return {"decision": "allow"}


async def hook_output(context: Any, params: Any, op: Any) -> dict[str, Any]:
    if params["op_id"] == "memory.read":
        return {"decision": "reject", "code": "POLICY_DENIED"}
    return {"decision": "allow"}
