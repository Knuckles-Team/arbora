"""A federated query is refused without a verified session (BUG-036 proof).

The served federation operations run through the one invoke pipeline. A
caller that is not authenticated, or that lacks the federation read scope,
is refused before the operation runs and before anything is audited.
"""

from __future__ import annotations

import pytest

from arbora.api.ops import query_engine
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import Params, run, setup_services

pytestmark = pytest.mark.spec("ARBORA-OPS-R009")

_OP_ID = "federation.sources.columns"
caller = make_verified_caller("federation:read")


def _federated_op():
    """The served operation's own authority (scopes, verb, effect, surfaces).

    Only its engine parameter schema is swapped for a plain model, so the
    test needs no engine contract on disk.
    """
    (op,) = [op for op in query_engine.operations() if op.id == _OP_ID]
    assert op.scopes == frozenset({"federation:read"})
    return op.model_copy(update={"params": Params, "result": Params})


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"authenticated": False}, "UNAUTHENTICATED"),
        ({"effective_scopes": frozenset()}, "SCOPE_REQUIRED"),
    ],
)
def test_federated_query_denies_without_a_verified_session(
    changes: dict[str, object], code: str
) -> None:
    services, runtime, audit, _, _ = setup_services(_federated_op())
    outcome = run(services, supplied=caller(**changes), op_id=_OP_ID)
    assert outcome.code == code
    assert not runtime.calls and not audit.events
