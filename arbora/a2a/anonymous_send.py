"""Explicit local text execution over native run admission and the shared harness.

This source does not configure transports, credentials or deployments. Answer
bodies use native RunEvent storage and an owner-bound committed reference.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

import msgpack
from agent_utilities.runtime import AgentRunExecutor, RunPlan, RunRequest
from epistemic_graph.client import EngineResponseError, EpistemicGraphClient
from epistemic_graph.generated import coordination as native
from epistemic_graph.generated import graph as native_graph
from fastapi import FastAPI, Request

from .anonymous import AnonymousReadPolicy, AnonymousTaskReadOwner, _unique_object


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AnonymousTextPolicy:
    origin: str
    graph: str
    revision: str
    model_id: str
    max_in_flight: int = 1
    operations: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        AnonymousReadPolicy(self.origin, "local", self.revision)
        if (
            type(self.operations) is not frozenset
            or not self.operations <= {"message/send"}
            or type(self.max_in_flight) is not int
            or not 1 <= self.max_in_flight <= 4
            or any(
                type(v) is not str or not v or len(v) > 128
                for v in (self.graph, self.model_id)
            )
        ):
            raise ValueError("Explicit bounded local text policy required")


class AnonymousTextService:
    """One fixed host plan, native owner and request-bound text operation."""

    def __init__(
        self,
        *,
        mode: str,
        policy: AnonymousTextPolicy,
        client: EpistemicGraphClient,
        plan: RunPlan,
    ) -> None:
        if mode != "none" or type(policy) is not AnonymousTextPolicy:
            raise ValueError("Explicit none mode required")
        if type(client) is not EpistemicGraphClient or type(plan) is not RunPlan:
            raise ValueError("Exact native client and run plan required")
        if (
            plan.execution_constraint != "text-only"
            or plan.model.model_name != policy.model_id
            or type(plan.budget.timeout_seconds) not in (int, float)
            or not 0 < plan.budget.timeout_seconds <= 60
        ):
            raise ValueError("Fixed bounded text plan required")
        self._policy = policy
        self._client = client
        self._plan = replace(
            plan,
            harness=replace(
                plan.harness,
                model_settings=copy.deepcopy(dict(plan.harness.model_settings)),
            ),
        )
        self._signature = self._plan_signature()
        self._model_identity = (
            id(plan.model),
            id(plan.model.provider),
            id(plan.model.provider.client),
        )
        self._revoked = False
        self._active: dict[asyncio.Task[Any], list[Any]] = {}
        self._check_mode()

    def _plan_signature(self) -> str:
        plan = self._plan
        return _digest(
            [
                plan.instructions,
                plan.steps,
                plan.tool_names,
                plan.toolsets,
                plan.budget.__dict__,
                plan.stop.__dict__,
                plan.harness.__dict__,
                plan.initial_usage.__dict__,
                plan.initial_elapsed_seconds,
                plan.input_media_types,
                plan.model.model_name,
                plan.model.settings,
                str(plan.model.provider.client.base_url),
            ]
        )

    @property
    def policy(self) -> AnonymousTextPolicy:
        return self._policy

    def _check_mode(self) -> None:
        if self._client.auth_mode != "none" or self._client.authenticated is not False:
            raise PermissionError("Native anonymous mode changed")

    def revoke(self) -> None:
        self._revoked = True
        for task in tuple(self._active):
            task.cancel()

    @staticmethod
    def _task() -> asyncio.Task[Any]:
        task = asyncio.current_task()
        if task is None:
            raise PermissionError("Anonymous request task required")
        return task

    def _check(self) -> None:
        self._check_mode()
        if self._revoked or asyncio.current_task() not in self._active:
            raise PermissionError("Anonymous send admission unavailable")
        if self._task().cancelling():
            raise asyncio.CancelledError
        request, snapshot, deadline, *_ = self._active[self._task()]
        if AnonymousTaskReadOwner._snapshot(request) != snapshot:
            raise PermissionError("Anonymous request changed")
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Anonymous send expired")
        if (
            self._plan.model.model_name != self.policy.model_id
            or self._plan_signature() != self._signature
            or (
                id(self._plan.model),
                id(self._plan.model.provider),
                id(self._plan.model.provider.client),
            )
            != self._model_identity
        ):
            raise PermissionError("Configured model changed")

    def _validate_request(self, request: Request) -> None:
        # Reuse the exact loopback/origin/header/credential rejection contract.
        AnonymousTaskReadOwner._validate_ingress(request, self.policy.origin)

    @contextmanager
    def admission(self, request: Request):
        self._validate_request(request)
        task = asyncio.current_task()
        if (
            task is None
            or task in self._active
            or self._revoked
            or "message/send" not in self.policy.operations
            or len(self._active) >= self.policy.max_in_flight
        ):
            raise PermissionError("Anonymous text policy or capacity refused")
        self._active[task] = [
            request,
            AnonymousTaskReadOwner._snapshot(request),
            asyncio.get_running_loop().time() + self._plan.budget.timeout_seconds,
            False,
            None,
            None,
        ]
        try:
            yield
        finally:
            del self._active[task]

    async def _native(
        self, name: str, params: dict[str, Any], *, key=None, finalizing=False
    ):
        def check():
            if not finalizing:
                self._check()
                return
            self._check_mode()
            binding = self._active.get(asyncio.current_task())
            if (
                binding is None
                or AnonymousTaskReadOwner._snapshot(binding[0]) != binding[1]
                or params.get("work_item_id") != binding[4]
                or not (
                    name == "CancelWorkItem"
                    or (
                        name == "CommitWorkItemResult"
                        and params.get("outcome") in {"failed", "cancelled"}
                    )
                )
            ):
                raise PermissionError("Terminal cleanup binding required")

        check()
        if await self._client.supports(name) is not True:
            raise RuntimeError("Native text execution method unavailable")
        check()
        sender, decoder = {
            "SubmitWorkItem": (
                native.send_submit_work_item,
                native.decode_submit_work_item,
            ),
            "ClaimWorkItem": (
                native.send_claim_work_item,
                native.decode_claim_work_item,
            ),
            "CommitWorkItemResult": (
                native.send_commit_work_item_result,
                native.decode_commit_work_item_result,
            ),
            "CancelWorkItem": (
                native.send_cancel_work_item,
                native.decode_cancel_work_item,
            ),
            "GetRunWorkItem": (
                native.send_get_run_work_item,
                native.decode_get_run_work_item,
            ),
        }[name]
        result = await sender(
            self._client, params, graph=self.policy.graph, idempotency_key=key
        )
        decoder(result)
        if name == "SubmitWorkItem":
            value = result.payload
            expected = params["request"]
            if (
                value["created"] is True
                and value["replayed"] is False
                and value["idempotency_key"] == expected["idempotency_key"]
                and value["command_digest"] == expected["command_digest"]
            ):
                self._active[self._task()][4] = value["work_item_id"]
        if name == "ClaimWorkItem":
            value = result.payload
            expected = params["request"]
            if (
                value["claimed"] is True
                and value["reason"] == "claimed"
                and value["work_item_id"] == expected["work_item_id"]
                and value["lease_holder_ref"] == expected["worker_ref"]
                and value["attempt"] == 1
                and value["max_attempts"] == 1
                and value["kind"] == "orchestrator_task"
                and all(
                    type(value.get(k)) is int and value[k] >= 1
                    for k in ("lease_epoch", "fencing_token")
                )
            ):
                self._active[self._task()][5] = dict(value)
        check()
        return result.payload

    async def send(self, *, text: str, key: str) -> dict[str, Any]:
        self._check()
        binding = self._active[self._task()]
        if binding[3]:
            raise PermissionError("Anonymous send admission already consumed")
        binding[3] = True
        if type(text) is not str or not text.strip() or len(text.encode()) > 8192:
            raise ValueError("Bounded text required")
        if type(key) is not str or not key or len(key) > 128:
            raise ValueError("Bounded request key required")
        # The stable identity excludes content: changing content under one key conflicts.
        run_id = "a2a-" + _digest(["anonymous-text", self.policy.graph, key])
        digest = _digest(
            [
                text,
                self.policy.revision,
                self.policy.model_id,
                self._signature,
                self.policy.max_in_flight,
            ]
        )
        template = replace(
            self._plan,
            harness=replace(
                self._plan.harness,
                model_settings=copy.deepcopy(dict(self._plan.harness.model_settings)),
            ),
        )
        now = int(time.time() * 1000)
        request = {
            "schema_version": "1",
            "run_id": run_id,
            "work_item_id": None,
            "context": {
                "schema_version": "2",
                "request_id": run_id,
                "trace_id": run_id,
                "subject_id": "anonymous",
                "agent_id": "anonymous",
                "tenant_id": "local",
                "audience": "epistemic-graph",
                "authentication_method": "none",
                "policy_version": "no-auth",
                "scopes": [],
                "graph": self.policy.graph,
                "placement_epoch": None,
                "issued_at_ms": now,
                "expires_at_ms": now
                + int((self._plan.budget.timeout_seconds + 10) * 1000),
            },
            "idempotency_key": run_id,
            "command_digest": digest,
            "kind": "orchestrator_task",
            "priority": 0,
            "depends_on": [],
            "input_ref": "sha256:" + _digest(text),
            "policy_digest": _digest(self.policy.revision),
            "catalog_digest": _digest("text-only"),
            "model_digest": _digest(self.policy.model_id),
            "max_attempts": 1,
            "deadline_unix": time.time() + self._plan.budget.timeout_seconds + 10,
            "metadata": {"a2a_context_id": "a2a-context-" + _digest(run_id)},
            "provenance_refs": [],
            "max_tenant_in_flight": self.policy.max_in_flight,
        }
        binding.append(self._stable_admission(request))
        try:
            return await self._admitted(
                request, text, run_id, digest, template, binding
            )
        except BaseException as exc:
            known_claim = binding[5]
            if known_claim is not None:
                try:
                    async with asyncio.timeout(2):
                        await self._finish(
                            run_id,
                            known_claim["work_item_id"],
                            known_claim["lease_holder_ref"],
                            known_claim,
                            "cancelled"
                            if isinstance(exc, asyncio.CancelledError)
                            else "failed",
                            None,
                            finalizing=True,
                        )
                except BaseException as cleanup_error:
                    logging.getLogger(__name__).warning(
                        "Anonymous native cleanup unavailable (%s)",
                        type(cleanup_error).__name__,
                    )
            # Queued cancellation never cancels an active lease. Unknown claim
            # acknowledgement remains in-flight/unknown and is never re-executed.
            item_id = binding[4]
            if item_id is not None:
                try:
                    async with asyncio.timeout(2):
                        await self._native(
                            "CancelWorkItem",
                            {
                                "tenant": "local",
                                "work_item_id": item_id,
                                "idempotency_key": run_id + ":abandon",
                                "now_ms": int(time.time() * 1000),
                                "reason_ref": "anonymous-send-abandoned",
                            },
                            finalizing=True,
                        )
                except BaseException as cleanup_error:
                    logging.getLogger(__name__).warning(
                        "Anonymous native cleanup unavailable (%s)",
                        type(cleanup_error).__name__,
                    )
            raise

    async def _admitted(self, request, text, run_id, digest, template, binding):
        existing = await self._lookup(run_id, digest)
        if existing is not None:
            return await self._replay(existing, run_id)
        try:
            admitted = await self._native("SubmitWorkItem", {"request": request})
        except EngineResponseError as exc:
            if exc.code != "IDEMPOTENCY_CONFLICT":
                raise
            # Native envelopes include admission timestamps. A concurrent retry
            # must recover the original, not invent an identical fresh envelope.
            existing = await self._lookup(run_id, digest)
            if existing is not None:
                return await self._replay(existing, run_id)
            raise
        item_id = admitted["work_item_id"]
        if (
            admitted["command_digest"] != digest
            or admitted["idempotency_key"] != run_id
            or admitted["created"] == admitted["replayed"]
            or not item_id.startswith("work-item:")
        ):
            raise RuntimeError("Native admission binding mismatch")
        if not admitted["created"]:
            original = await self._read(run_id, item_id, digest)
            return await self._replay(original, run_id)
        # Each first submit winner gets a fresh, non-replayed claim operation identity.
        worker = "arbora-anonymous-" + uuid.uuid4().hex
        claim = await self._native(
            "ClaimWorkItem",
            {
                "request": {
                    "schema_version": "1",
                    "tenant_ref": "local",
                    "work_item_id": item_id,
                    "queue_ref": None,
                    "resource_class": None,
                    "fairness_group": None,
                    "worker_ref": worker,
                    "now_ms": int(time.time() * 1000),
                    "lease_ms": int((self._plan.budget.timeout_seconds + 10) * 1000),
                    "max_tenant_in_flight": self.policy.max_in_flight,
                }
            },
            key=worker,
        )
        if (
            claim["claimed"] is not True
            or claim["work_item_id"] != item_id
            or claim["lease_holder_ref"] != worker
            or claim["attempt"] != 1
            or claim["max_attempts"] != 1
            or claim["payload_ref"] != request["input_ref"]
            or claim["lease_expires_at_ms"] <= int(time.time() * 1000)
            or claim["reason"] != "claimed"
            or item_id not in claim["changed_work_item_ids"]
            or claim["kind"] != "orchestrator_task"
            or any(
                type(claim.get(k)) is not int or claim[k] < 1
                for k in ("lease_epoch", "fencing_token")
            )
        ):
            raise RuntimeError("Native claim unavailable")
        try:
            return await self._run_claimed(
                text, run_id, digest, template, binding, item_id, worker, claim
            )
        except BaseException as exc:
            try:
                async with asyncio.timeout(2):
                    await self._finish(
                        run_id,
                        item_id,
                        worker,
                        claim,
                        "cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed",
                        None,
                        finalizing=True,
                    )
            except BaseException as cleanup_error:
                logging.getLogger(__name__).warning(
                    "Anonymous native cleanup unavailable (%s)",
                    type(cleanup_error).__name__,
                )
            raise

    async def _run_claimed(
        self, text, run_id, digest, template, binding, item_id, worker, claim
    ):
        self._check()
        elapsed = self._plan.budget.timeout_seconds - (
            binding[2] - asyncio.get_running_loop().time()
        )
        plan = replace(template, steps=(text,), initial_elapsed_seconds=max(0, elapsed))
        from arbora.api.contracts.session import suspend_session
        from arbora.security.actor_context import suspend_actor

        with suspend_session(), suspend_actor():
            result = await AgentRunExecutor().execute_text_only(
                RunRequest(task=text, run_id=run_id), plan=plan
            )
        self._check()
        outcome = (
            "succeeded"
            if result.status == "completed" and 0 < len(result.output) <= 65536
            else "failed"
        )
        usage = {
            k: v
            for k, v in result.usage.items()
            if k
            in {
                "requests",
                "tool_calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
            }
        }
        reference = (
            await self._store_reply(run_id, item_id, result.output)
            if outcome == "succeeded"
            else None
        )
        committed = await self._finish(
            run_id, item_id, worker, claim, outcome, usage, reference=reference
        )
        if (
            committed["status"] != outcome
            or item_id not in committed["changed_work_item_ids"]
        ):
            raise RuntimeError("Native completion outcome unknown")
        original = await self._read(run_id, item_id, digest)
        receipt = original.get("run_completion")
        if (
            not receipt
            or receipt["payload"]["outcome"] != outcome
            or receipt["payload"]["tenant"] != "local"
            or receipt["payload"]["work_item_id"] != item_id
            or receipt["payload"]["completion"]["run_id"] != run_id
            or receipt["payload"]["completion"].get("usage") != usage
            or original["work_item"]["status"] != outcome
            or receipt["payload"]["result_ref"] != reference
        ):
            raise RuntimeError("Native completion receipt unavailable")
        return {
            "id": run_id,
            "status": outcome,
            "replayed": False,
            "answer_available": outcome == "succeeded",
            "text": result.output if outcome == "succeeded" else None,
        }

    async def _content(self, name: str, params: dict[str, Any]):
        self._check()
        if await self._client.supports(name) is not True:
            raise RuntimeError("Native reply storage unavailable")
        self._check()
        sender = {
            "CreateNodeIfAbsent": native_graph.send_create_node_if_absent,
            "GetNodeProperties": native_graph.send_get_node_properties,
        }[name]
        result = await sender(self._client, params, graph=self.policy.graph)
        self._check()
        if name == "CreateNodeIfAbsent":
            return result
        value = result.payload
        if isinstance(value, (bytes, bytearray)):
            if len(value) > 300000:
                raise RuntimeError("Native reply properties exceed bound")
            value = msgpack.unpackb(
                value,
                raw=False,
                strict_map_key=True,
                max_str_len=262144,
                max_bin_len=0,
                max_array_len=16,
                max_map_len=16,
            )
        if value is not None and type(value) is not dict:
            raise RuntimeError("Native reply properties unavailable")
        return value

    async def _store_reply(self, run_id: str, item_id: str, text: str) -> str:
        event = {
            "type": "RunEvent",
            "run_id": run_id,
            "task_id": run_id,
            "work_item_id": item_id,
            "event_type": "graph_complete",
            "payload": json.dumps(
                {
                    "type": "graph_complete",
                    "run_id": run_id,
                    "result": {"run_id": run_id, "status": "completed", "output": text},
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        }
        encoded = msgpack.packb(event, use_bin_type=True)
        if len(event["payload"].encode()) > 262144 or len(encoded) > 300000:
            raise RuntimeError("Serialized native answer exceeds recovery bound")
        digest = _digest(event)
        node_id = "runevent:" + run_id + ":" + digest
        await self._content(
            "CreateNodeIfAbsent",
            {
                "node_id": node_id,
                "properties_msgpack": encoded,
            },
        )
        stored = await self._content("GetNodeProperties", {"node_id": node_id})
        if stored != event:
            raise RuntimeError("Native reply storage readback differs")
        return "urn:arbora:anonymous-reply:" + digest

    async def _recover(self, original, run_id, item_id):
        if original["work_item"]["status"] != "succeeded":
            return None
        receipt = original.get("run_completion")
        if not receipt:
            raise RuntimeError("Native completion receipt unavailable")
        payload = receipt["payload"]
        prefix = "urn:arbora:anonymous-reply:"
        reference = payload["result_ref"]
        if (
            payload["tenant"] != "local"
            or payload["work_item_id"] != item_id
            or payload["outcome"] != "succeeded"
            or payload["completion"]["run_id"] != run_id
            or type(reference) is not str
            or not reference.startswith(prefix)
        ):
            raise RuntimeError("Native answer reference unavailable")
        digest = reference[len(prefix) :]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RuntimeError("Invalid native answer digest")
        stored = await self._content(
            "GetNodeProperties", {"node_id": "runevent:" + run_id + ":" + digest}
        )
        if (
            type(stored) is not dict
            or _digest(stored) != digest
            or stored.get("run_id") != run_id
            or stored.get("task_id") != run_id
            or stored.get("work_item_id") != item_id
            or stored.get("event_type") != "graph_complete"
            or stored.get("type") != "RunEvent"
            or type(stored.get("payload")) is not str
            or len(stored["payload"].encode()) > 300000
        ):
            raise RuntimeError("Native answer content unavailable")
        event = json.loads(stored["payload"], object_pairs_hook=_unique_object)
        result = event.get("result") if type(event) is dict else None
        if (
            type(result) is not dict
            or event.get("type") != "graph_complete"
            or event.get("run_id") != run_id
            or result.get("run_id") != run_id
            or result.get("status") != "completed"
            or type(result.get("output")) is not str
            or not 0 < len(result["output"]) <= 65536
        ):
            raise RuntimeError("Native terminal reply unavailable")
        return result["output"]

    async def _finish(
        self,
        run_id,
        item_id,
        worker,
        claim,
        outcome,
        usage,
        *,
        finalizing=False,
        reference=None,
    ):
        completion = {"schema_version": 1, "run_id": run_id}
        if usage is not None:
            completion["usage"] = usage
        return await self._native(
            "CommitWorkItemResult",
            {
                "tenant": "local",
                "work_item_id": item_id,
                "worker_id": worker,
                "lease_epoch": claim["lease_epoch"],
                "fencing_token": claim["fencing_token"],
                "idempotency_key": worker + ":completion",
                "outcome": outcome,
                "now_ms": int(time.time() * 1000),
                "retryable": False,
                "result_ref": reference,
                "error_ref": None,
                "run_completion": completion,
            },
            key=worker + ":completion",
            finalizing=finalizing,
        )

    async def _lookup(self, run_id, digest):
        original = await self._native(
            "GetRunWorkItem", {"tenant": "local", "run_id": run_id}
        )
        if original is None:
            return None
        self._validate_original(
            original, run_id, original["work_item"]["work_item_id"], digest
        )
        return original

    async def _replay(self, original, run_id):
        item_id = original["work_item"]["work_item_id"]
        answer = await self._recover(original, run_id, item_id)
        return {
            "id": run_id,
            "status": original["work_item"]["status"],
            "replayed": True,
            "answer_available": answer is not None,
            "text": answer,
        }

    async def _read(self, run_id: str, item_id: str, digest: str):
        original = await self._native(
            "GetRunWorkItem", {"tenant": "local", "run_id": run_id}
        )
        self._validate_original(original, run_id, item_id, digest)
        return original

    @staticmethod
    def _stable_admission(request):
        canonical = copy.deepcopy(request)
        canonical.pop("deadline_unix", None)
        canonical["context"].pop("issued_at_ms", None)
        canonical["context"].pop("expires_at_ms", None)
        return json.dumps(canonical, sort_keys=True, separators=(",", ":"))

    def _validate_original(self, original, run_id, item_id, digest):
        if (
            original is None
            or original["work_item"]["work_item_id"] != item_id
            or original["admission"]["run_id"] != run_id
            or original["admission"]["command_digest"] != digest
            or original["admission"]["context"]["graph"] != self.policy.graph
            or original["admission"]["context"]["tenant_id"] != "local"
            or self._stable_admission(original["admission"])
            != self._active[self._task()][6]
        ):
            raise RuntimeError("Native original admission unavailable")


def anonymous_text_app(service: AnonymousTextService) -> FastAPI:
    """Opt-in source application, never installed into a protected deployment."""
    if type(service) is not AnonymousTextService:
        raise ValueError("Exact anonymous text service required")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.post("/a2a")
    async def send(request: Request):
        from fastapi.responses import JSONResponse

        from .application import _error

        request_id = None
        try:
            with service.admission(request):
                async with asyncio.timeout(service._plan.budget.timeout_seconds + 5):
                    raw = bytearray()
                    async for chunk in request.stream():
                        if len(raw) + len(chunk) > 16384:
                            raise ValueError("Request too large")
                        raw.extend(chunk)
                    body = json.loads(raw, object_pairs_hook=_unique_object)
                    if type(body) is not dict or set(body) != {
                        "jsonrpc",
                        "id",
                        "method",
                        "params",
                    }:
                        raise ValueError("Invalid envelope")
                    request_id = body["id"]
                    if type(request_id) not in (str, int) or len(str(request_id)) > 128:
                        request_id = None
                        raise ValueError("Invalid identifier")
                    if body["jsonrpc"] != "2.0" or body["method"] != "message/send":
                        raise PermissionError("Operation unavailable")
                    from .application import _SendParams
                    from .models import A2ATextPart

                    params = _SendParams.model_validate(body["params"])
                    message = params.message
                    if (
                        message.context_id is not None
                        or message.metadata
                        or len(message.parts) != 1
                        or type(message.parts[0]) is not A2ATextPart
                        or params.context_budget_tokens is not None
                        or (
                            params.configuration is not None
                            and params.configuration.model_dump(exclude_none=True)
                            != {"blocking": True}
                        )
                    ):
                        raise ValueError("Only one blocking text message is supported")
                    result = await service.send(
                        text=message.parts[0].text, key=message.message_id
                    )
                    from .authority import _STATE_MAP
                    from .models import A2AArtifact, A2ATask, A2ATaskStatus

                    answer = result.get("text")
                    task = A2ATask(
                        id=result["id"],
                        context_id="a2a-context-" + _digest(result["id"]),
                        status=A2ATaskStatus(state=_STATE_MAP[result["status"]]),
                        artifacts=[
                            A2AArtifact(
                                artifact_id="live-answer",
                                parts=[A2ATextPart(text=answer)],
                            )
                        ]
                        if answer
                        else None,
                        metadata={
                            "authMode": "none",
                            "replayed": result["replayed"],
                            "answerAvailable": result["answer_available"],
                            "durableAnswerAvailable": result["answer_available"],
                        },
                    )
                    return JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": task.model_dump(by_alias=True, exclude_none=True),
                        },
                        headers={"Cache-Control": "no-store"},
                    )
        except PermissionError:
            return _error(request_id, -32001, "Anonymous send refused", 403)
        except (ValueError, TypeError):
            return _error(request_id, -32602, "Invalid text request")
        except Exception:
            return _error(
                request_id,
                -32014,
                "Text outcome unavailable; native recovery may be required",
                503,
            )

    return app
