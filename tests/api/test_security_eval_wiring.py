"""The registry lists the operation and the binder composes the engine evaluation (ARBORA-OPS-R051.3)."""

from __future__ import annotations

import pytest

from arbora.api.ops.registry_factory import get_registry
from arbora.api.ops.security_eval import EngineSecurityEvaluation
from arbora.api.registry import Composite, Confirm
from arbora.mcp_server.served_invocation import _with_security_evaluation

pytestmark = pytest.mark.spec("ARBORA-OPS-R051.3")


def test_registry_lists_operation_and_binder_composes_ports() -> None:
    op = get_registry()["security.evaluate"]
    assert op.scopes == frozenset({"agent:decision-evaluate"})
    assert op.confirm is Confirm.PLAN
    assert isinstance(op.binding, Composite)
    assert op.binding.handler == "arbora.api.ops.security_eval.handle_security_evaluate"

    composed = _with_security_evaluation({"other": object()})
    assert composed is not None
    assert isinstance(composed["security_evaluation"], EngineSecurityEvaluation)
    # The harness adapter is the host's; the binder never invents one.
    assert "security_probe_runner" not in composed
    assert _with_security_evaluation(None) is None
    bound = object()
    kept = _with_security_evaluation({"security_evaluation": bound})
    assert kept is not None and kept["security_evaluation"] is bound
