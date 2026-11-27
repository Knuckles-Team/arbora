"""The served agent surface: llms.txt, best-practice resources and the skill export.

ARBORA-DEPLOY-POLICY-R006. Every artifact is generated for one release digest and
only after :func:`gate_mcp_resource_publication` accepted the reconciliation
receipt for the candidate catalog. A missing or stale receipt refuses with the gate's
own ``reingestion-unreconciled`` result and publishes nothing; Arbora fabricates no
receipt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arbora.api.domain.fleet.mcp_resource_reconciliation import (
    PublicationRefusedError,
    ReconciliationReceipt,
    require_reconciled_for_swap,
)
from arbora.core.environment import read_env

__all__ = [
    "BEST_PRACTICES",
    "AgentSurface",
    "SurfaceCandidate",
    "build_llms_txt",
    "deployed_surface",
    "export_skills",
    "no_receipt",
    "register_agent_surface",
]

#: The best-practice resources served beside the operation resources.
BEST_PRACTICES: dict[str, str] = {
    "discover-first": "Call find before concluding a capability is unavailable.",
    "preview-then-confirm": "write, act and manage preview first; resubmit with the returned plan_ref and execute=true.",
    "one-idempotency-key": "Send one idempotency key per intended effect; a replay returns the recorded outcome.",
}

#: ``receipt()`` returns the engine's reconciliation receipt, or ``None``.
ReceiptSource = Callable[[], Awaitable[ReconciliationReceipt | None]]


@dataclass(frozen=True, slots=True)
class SurfaceCandidate:
    """The catalog generation the surface would publish."""

    tenant_id: str
    generation: int
    snapshot_digest: str


@dataclass(frozen=True, slots=True)
class AgentSurface:
    registry: Any
    release_digest: str
    candidate: SurfaceCandidate
    receipt: ReceiptSource
    clock_ms: Callable[[], int]
    docs_url: str = "https://knuckles-team.github.io/arbora/"
    max_age_ms: int = 900_000

    async def require_reconciled(self) -> None:
        """Refuse with ``reingestion-unreconciled`` unless the receipt matches."""
        require_reconciled_for_swap(
            await self.receipt(),
            tenant_id=self.candidate.tenant_id,
            expected_generation=self.candidate.generation,
            expected_digest=self.candidate.snapshot_digest,
            now_ms=self.clock_ms(),
            max_age_ms=self.max_age_ms,
        )


async def build_llms_txt(surface: AgentSurface) -> str:
    """The operations and docs links of the deployed version, gated first."""
    await surface.require_reconciled()
    lines = [
        "# Arbora",
        "",
        f"> Release digest: {surface.release_digest}",
        f"> Registry digest: {surface.registry.digest}",
        "",
        "## Operations",
        "",
    ]
    lines += [
        f"- {op.id}: {op.summary}"
        for op in sorted(surface.registry, key=lambda op: op.id)
    ]
    lines += ["", "## Docs", "", f"- [Operator documentation]({surface.docs_url})", ""]
    return "\n".join(lines)


async def export_skills(
    surface: AgentSurface, source: Path, target: Path, *, digest_stamp: bool
) -> list[str]:
    """Write each ``SKILL.md`` under ``source`` to ``target``, stamped with the release digest."""
    await surface.require_reconciled()
    written: list[str] = []
    for skill in sorted(source.glob("*/SKILL.md")):
        body = skill.read_text(encoding="utf-8")
        if digest_stamp:
            body = f"<!-- release-digest: {surface.release_digest} -->\n{body}"
        out = target / skill.parent.name / "SKILL.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        written.append(skill.parent.name)
    return written


def register_agent_surface(mcp: Any, surface: AgentSurface) -> None:
    """Register ``GET /llms.txt`` and the best-practice resources on one server."""
    from starlette.responses import JSONResponse, PlainTextResponse

    @mcp.custom_route("/llms.txt", methods=["GET"])
    async def llms_txt(request: Any) -> Any:  # noqa: ARG001
        try:
            return PlainTextResponse(await build_llms_txt(surface))
        except PublicationRefusedError as refused:
            return JSONResponse(
                {"code": refused.result.code, "reason": refused.result.reason},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

    @mcp.resource("arbora://best-practices/{topic}", mime_type="text/plain")
    async def best_practice(topic: str) -> str:
        await surface.require_reconciled()
        if topic not in BEST_PRACTICES:
            raise ValueError("Unknown best-practice topic")
        return f"{BEST_PRACTICES[topic]}\n\nrelease-digest: {surface.release_digest}\n"


async def no_receipt() -> ReconciliationReceipt | None:
    """The receipt source until the engine serves the reconciliation receipt: none."""
    return None


def deployed_surface(registry: Any, *, clock_ms: Callable[[], int]) -> AgentSurface:
    """The surface of the running release: its digest names the release and the catalog."""

    release = str(read_env("ARBORA_RELEASE_DIGEST") or "").strip() or registry.digest
    tenant_id = str(read_env("ARBORA_TENANT") or "").strip()
    return AgentSurface(
        registry=registry,
        release_digest=release,
        candidate=SurfaceCandidate(tenant_id, 0, registry.digest),
        receipt=no_receipt,
        clock_ms=clock_ms,
    )
