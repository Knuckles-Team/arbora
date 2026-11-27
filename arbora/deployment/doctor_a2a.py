"""A2A task persistence check for the deployment doctor (ARBORA-HOST-R026.4).

Arbora holds no task store: the engine does. The check proves that a task the
service creates is read back from the engine, so a deployment that would lose
tasks on restart fails before it serves.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from arbora.api.domain.a2a.models import A2AMessage, A2ARouteDecision, A2ATask

from .doctor_support import _result

logger = logging.getLogger(__name__)

PROBE_TENANT = "doctor"
A2A_STORE_UNBOUND = "A2A_STORE_UNBOUND"
A2A_STORE_NOT_DURABLE = "A2A_STORE_NOT_DURABLE"

#: Reads one task straight from the engine, not through the service's own cache.
EngineTaskReader = Callable[[str], Awaitable[A2ATask | None]]


def _probe_message(probe_id: str) -> A2AMessage:
    return A2AMessage(
        role="user",
        parts=[{"kind": "text", "text": "arbora-doctor a2a persistence probe"}],
        message_id=f"doctor-probe-{probe_id}",
        metadata={"tenant": PROBE_TENANT},
    )


def _probe_decision() -> A2ARouteDecision:
    return A2ARouteDecision(agent_name="doctor-probe", selection_mode="doctor")


async def check_a2a_persistence(
    service: Any, read_task: EngineTaskReader | None
) -> dict[str, Any]:
    """Create one probe task, read it back from the engine, then cancel it.

    ``pass`` when the engine returns the task. ``fail`` with
    ``A2A_STORE_UNBOUND`` when no service or engine reader is bound, and with
    ``A2A_STORE_NOT_DURABLE`` when the read-back differs. The probe task
    carries tenant ``doctor`` and is cancelled on every path that created it.
    """
    if service is None or read_task is None:
        return _result(
            "a2a_persistence",
            "fail",
            f"{A2A_STORE_UNBOUND}: no engine client is bound to the A2A service",
            remediation="bind the engine client to the A2A task authority",
            data={"code": A2A_STORE_UNBOUND},
        )
    probe_id = secrets.token_hex(16)
    task_id: str | None = None
    try:
        created = await service.authority.dispatch(
            message=_probe_message(probe_id),
            idempotency_key=f"doctor-probe-{probe_id}",
            decision=_probe_decision(),
            blocking=False,
        )
        task_id = created.id
        stored = await read_task(task_id)
    except Exception as exc:
        logger.warning("a2a persistence probe failed", exc_info=True)
        return _result(
            "a2a_persistence",
            "fail",
            f"{A2A_STORE_NOT_DURABLE}: the probe task could not be stored "
            f"or read back ({type(exc).__name__})",
            remediation="check the engine connection of the A2A task authority",
            data={"code": A2A_STORE_NOT_DURABLE},
        )
    finally:
        if task_id is not None:
            try:
                await service.authority.cancel(task_id)
            except Exception:
                logger.warning("a2a probe cancel failed", exc_info=True)
    if stored is None or stored.id != task_id:
        return _result(
            "a2a_persistence",
            "fail",
            f"{A2A_STORE_NOT_DURABLE}: the engine did not return the probe task",
            remediation="the A2A task store must be the engine, not process memory",
            data={"code": A2A_STORE_NOT_DURABLE},
        )
    return _result(
        "a2a_persistence",
        "ok",
        "the engine returned the A2A probe task",
        data={"tenant": PROBE_TENANT},
    )
