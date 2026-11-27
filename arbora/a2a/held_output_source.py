"""The source of a run's held-output refs (ARBORA-A2A-R022.1).

Arbora composes the source with engine methods; medulla owns the shapes
(``MEDULLA-DECISION-ENGINE-R038.3``). When a native run ends with a reply, before the
reply is read:

1. the reply is written as a held ``run_output_proposal`` node through the engine node
   write (``CreateNodeIfAbsent``); its id is ``item_ref``;
2. the engine ``held_item_admission`` question is asked about that node and the exact
   digest of the reply; the sealing record's id is ``engine_decision_ref`` and the
   answer's checklist is the checklist;
3. the four refs are stored on a second node, so a later task read lifts the same block
   the harness lifts (``arbora.a2a.held_output.lift_held_output``).

Both writes are create-if-absent under an id derived from the tenant, the task and the
digest: replay owner is the run (``task_id``), and a replayed run writes nothing twice.
Arbora decides nothing. A missing checklist, an unanswered question or a refused write
yields no refs, so the output stays held (``POLICY_EVIDENCE_MISSING``).

The checklist is read from the engine's ``held_item_admission`` answer. The engine
records ``critique_ref``, ``critique_digest`` and ``critique_verdict`` in its own
decision record and on the proposal node; arbora never writes them.

When a critic's answers are available, the engine records the critique: arbora sends
``DecisionEval`` ``held_item_critique`` with the answers and the engine derives the
verdict and writes the critique fields onto the proposal node. The later admission then
answers ``admitted`` or ``rejected`` rather than abstaining ``CRITIQUE_MISSING``.

PIN-LAG: until the pinned engine's answer carries a checklist, no block is produced and
the gate holds every output; until it declares ``held_item_critique`` no critique is
recorded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from arbora.unique_records import create_record

from .engine_deciders import HELD_ITEM_KIND, AskDecide
from .held_output import lift_held_output, reply_bytes

__all__ = [
    "HELD_ITEM_LABEL",
    "HELD_REFS_LABEL",
    "HeldOutputSource",
    "bind_held_output",
    "held_block",
    "held_item_id",
    "read_held_refs",
]

logger = logging.getLogger(__name__)

HELD_ITEM_LABEL = "HeldItem"
HELD_REFS_LABEL = "HeldOutputRefs"
ITEM_KIND = "run_output_proposal"
_MAX_CHECKLIST = 64


def held_item_id(tenant: str, task_id: str, digest: str) -> str:
    """The node id of one run's held output: stable for the tenant, task and bytes."""
    material = json.dumps([tenant, task_id, digest], separators=(",", ":"))
    return "held:" + hashlib.sha256(material.encode()).hexdigest()


def _checklist(value: Any) -> list[dict[str, str]] | None:
    """The answer's typed checklist, or ``None`` when absent or malformed."""
    if not isinstance(value, list) or not 0 < len(value) <= _MAX_CHECKLIST:
        return None
    items: list[dict[str, str]] = []
    for entry in value:
        ident = entry.get("id") if isinstance(entry, Mapping) else None
        question = entry.get("question") if isinstance(entry, Mapping) else None
        if not (isinstance(ident, str) and ident and isinstance(question, str)):
            return None
        if not question:
            return None
        items.append({"id": ident, "question": question})
    return items


def _answer_value(record: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    answer = record.get("answer") if isinstance(record, Mapping) else None
    value = answer.get("value") if isinstance(answer, Mapping) else None
    return value if isinstance(value, Mapping) else None


Send = Callable[[str, dict[str, Any], str], Awaitable[Any]]
#: ``critic(checklist, text)`` answers each question ``{id, passed}``, or ``None``.
Critic = Callable[
    [Sequence[Mapping[str, str]], str], Awaitable[Sequence[Mapping[str, Any]] | None]
]


def _recorded(reply: Any) -> Mapping[str, Any] | None:
    """The ``HeldItemCritiqueRecorded`` of an engine reply, plain or under its body."""
    for candidate in (reply, reply.get("body") if isinstance(reply, Mapping) else None):
        if isinstance(candidate, Mapping) and "critique_ref" in candidate:
            return candidate
    return None


def _critique_op(
    tenant: str,
    graph: str,
    item_ref: str,
    digest: str,
    checklist: Sequence[Mapping[str, str]],
    answers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The ``held_item_critique`` op; the answers must be one boolean per checklist id."""
    typed = [{"id": str(a["id"]), "passed": a["passed"]} for a in answers]
    ids = sorted(item["id"] for item in checklist)
    if sorted(a["id"] for a in typed) != ids or not all(
        isinstance(a["passed"], bool) for a in typed
    ):
        raise ValueError("answers do not match the checklist")
    request = {
        "tenant_id": tenant,
        "graph": graph,
        "item_ref": item_ref,
        "content_digest": digest,
        "answers": typed,
    }
    return {"op": {"op": "held_item_critique", "request": request}}


class HeldOutputSource:
    """Produce the held-output refs of one finished run from engine methods."""

    def __init__(
        self, ask: AskDecide, send: Send | None = None, critic: Critic | None = None
    ) -> None:
        self._ask = ask
        self._send = send
        self._critic = critic

    async def _record_critique(
        self,
        tenant: str,
        item_ref: str,
        digest: str,
        checklist: list[dict[str, str]],
        text: str,
        graph: str,
    ) -> None:
        """Have the engine record the critic's answers; arbora writes no critique field.

        Raises when the answers do not match the checklist or the engine does not
        confirm the recording, so the caller holds the output.
        """
        if self._critic is None or self._send is None:
            return
        answers = await self._critic(checklist, text)
        if answers is None:
            return
        from arbora.pin_lag.medulla import guard_held_item_critique

        guard_held_item_critique()
        reply = await self._send(
            "DecisionEval",
            _critique_op(tenant, graph, item_ref, digest, checklist, answers),
            tenant,
        )
        recorded = _recorded(reply) or {}
        if (recorded.get("item_ref"), recorded.get("content_digest")) != (
            item_ref,
            digest,
        ):
            raise RuntimeError("critique not recorded")

    async def produce(
        self,
        engine: Any,
        *,
        tenant: str,
        task_id: str,
        text: str,
        graph: str = "",
    ) -> dict[str, Any] | None:
        """Write the held node, ask the engine, store the refs; ``None`` holds the output."""
        digest = hashlib.sha256(reply_bytes(task_id, text)).hexdigest()
        item_ref = held_item_id(tenant, task_id, digest)
        try:
            await asyncio.to_thread(
                create_record,
                engine,
                item_ref,
                HELD_ITEM_LABEL,
                {
                    "held_item_kind": ITEM_KIND,
                    "content_digest": digest,
                    "lifecycle": "active",
                    "task_id": task_id,
                },
            )
            record = await self._ask(
                HELD_ITEM_KIND,
                {
                    "item_kind": ITEM_KIND,
                    "item_ref": item_ref,
                    "content_digest": digest,
                },
                tenant,
            )
            value = _answer_value(record)
            checklist = _checklist(None if value is None else value.get("checklist"))
            decision = None if value is None else value.get("verdict_ref")
            if not (isinstance(decision, str) and decision and checklist):
                return None
            await self._record_critique(
                tenant, item_ref, digest, checklist, text, graph
            )
            block = {
                "item_ref": item_ref,
                "content_digest": digest,
                "engine_decision_ref": decision,
                "checklist": checklist,
            }
            await asyncio.to_thread(
                create_record,
                engine,
                f"{item_ref}:refs",
                HELD_REFS_LABEL,
                {
                    "task_id": task_id,
                    "item_ref": item_ref,
                    "content_digest": digest,
                    "engine_decision_ref": decision,
                    "checklist_json": json.dumps(checklist, separators=(",", ":")),
                },
            )
        except Exception as exc:  # noqa: BLE001 - no refs leaves the output held
            logger.warning(
                "held output refs unavailable for %s: %s", task_id, type(exc).__name__
            )
            return None
        return block


def read_held_refs(engine: Any, task_id: str) -> dict[str, Any] | None:
    """The stored four-key block of ``task_id``'s held output, or ``None``."""
    rows = engine.query_cypher(
        "MATCH (n) WHERE n.type = $label AND n.task_id = $task_id "
        "RETURN n.item_ref AS item_ref, n.content_digest AS content_digest, "
        "n.engine_decision_ref AS engine_decision_ref, "
        "n.checklist_json AS checklist_json LIMIT 2",
        {"label": HELD_REFS_LABEL, "task_id": task_id},
    )
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    try:
        checklist = json.loads(str(row.get("checklist_json")))
    except ValueError:
        return None
    items = _checklist(checklist)
    fields = ("item_ref", "content_digest", "engine_decision_ref")
    if items is None or not all(isinstance(row.get(name), str) for name in fields):
        return None
    return {**{name: row[name] for name in fields}, "checklist": items}


def _no_refs(engine: Any, task_id: str) -> None:
    """A host without a composed source stores no refs, so it makes no read."""
    return None


def held_block(
    result: Any, engine: Any, task_id: str, source: Any
) -> dict[str, Any] | None:
    """The run's held-output block: the harness's own, else the stored refs."""
    stored = (_no_refs, read_held_refs)[source is not None]
    return lift_held_output(result) or stored(engine, task_id)


def bind_held_output(
    authority: Any, invocation: Any, *, critic: Critic | None = None
) -> bool:
    """Bind the source to ``authority`` once the host has the engine ``Decide`` seam.

    ``invocation`` is the served invocation: its engine sender, decision graph, tenant
    feature-schema pin and service principal. Returns whether it was bound. A host
    without any of them keeps no source, and every output stays held (R022.1).
    """
    from .engine_decide import build_ask_decide

    send = getattr(invocation, "engine_send", None)
    schema = getattr(invocation, "decision_feature_schema", None)
    principal = getattr(invocation, "decision_principal", None)
    if None in (send, schema, principal) or not hasattr(
        authority, "held_output_source"
    ):
        return False
    authority.held_output_source = HeldOutputSource(
        build_ask_decide(
            send,
            graph=getattr(invocation, "decision_graph", ""),
            feature_schema=schema,
            principal=principal,
        ),
        send,
        critic,
    )
    authority.held_output_graph = getattr(invocation, "decision_graph", "")
    return True
