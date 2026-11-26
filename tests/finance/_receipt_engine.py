"""A recording engine for the finance receipt machine.

``ReceiptMachine`` answers ``CreateNodeIfAbsent`` and ``FinanceReceiptTransition``
with the shapes of MEDULLA-FINANCE-PRIMITIVES-R011.4 and R011.5: a receipt starts
``due`` at attempt 1 and moves only along the machine edges. Its table lives
outside any one host, so a test can drop the port and read the same receipts
back.
"""

from __future__ import annotations

from typing import Any

import msgpack

from arbora.api.errors import EngineRefusal
from arbora.finance.receipts import ServedReceiptEngine

EDGES: dict[tuple[str, str], str] = {
    ("due", "claim"): "claimed",
    ("claimed", "deliver"): "delivered",
    ("delivered", "complete"): "completed",
    ("due", "miss"): "missed",
    ("claimed", "fail"): "failed",
    ("delivered", "fail"): "failed",
    ("failed", "retry"): "due",
    ("missed", "retry"): "due",
}


class ReceiptMachine:
    """Engine-resident receipts keyed by node id, with the engine's refusals."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    async def send(self, method: str, params: dict[str, Any]) -> Any:
        self.sent.append((method, params))
        if self.fail:
            raise ConnectionError("engine unreachable")
        if method == "CreateNodeIfAbsent":
            fields = msgpack.unpackb(params["properties_msgpack"], raw=False)
            created = params["node_id"] not in self.nodes
            self.nodes.setdefault(params["node_id"], fields)
            return created
        node = self.nodes.get(params["receipt_id"])
        if node is None:
            raise EngineRefusal("RECEIPT_NOT_FOUND")
        step = (node["receiptState"], params["event"])
        if step not in EDGES:
            raise EngineRefusal("TRANSITION_REFUSED")
        node["receiptState"] = EDGES[step]
        if params["event"] == "retry":
            node["receiptAttempt"] += 1
        return node

    def engine(self) -> ServedReceiptEngine:
        return ServedReceiptEngine(self.send)
