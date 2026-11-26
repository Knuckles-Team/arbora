"""Generated code materializes only under a committed admission (ARBORA-OPS-R869)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _decide, source
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context

_BODY = {
    "repository": "r",
    "branch": "b",
    "message": "m",
    "paths": [{"path": "a.py", "op": "delete", "mode": 0}],
}


def _op():
    return next(o for o in source.operations() if o.id == "source.commit")


@pytest.mark.spec("ARBORA-OPS-R869")
async def test_write_refuses_without_matching_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = source.SourceCommitParams.model_validate(_BODY).model_dump(
        mode="json", exclude_none=True
    )
    content["repo"] = content.pop("repository")
    content.pop("admission_record_digest", None)
    good = {
        "kind": "GeneratedCodeAdmission",
        "verdict": "admit",
        "tenant": "tenant:one",
        "artifact_digest": _decide.record_digest(content),
    }
    records = {
        "ok": good,
        "other-tenant": {**good, "tenant": "tenant:two"},
        "wrong-artifact": {**good, "artifact_digest": "0" * 64},
        "refused": {**good, "verdict": "refuse"},
    }
    engine = EngineDouble(monkeypatch)
    engine.serve("DecisionLog", lambda p: records.get(p["op"]["record_id"], {}))
    engine.serve("SubmitChangeset", lambda p: {"committed": True})
    engine.install(_decide)
    engine.install(source)
    context = engine_context()
    context.caller.principal_kind = "agent"

    def commit(digest):
        return source.handle_source_commit(
            context, {**_BODY, "admission_record_digest": digest}, _op()
        )

    for digest, reason in (
        (None, "admission_record_missing"),
        ("absent", "admission_record_missing"),
        ("other-tenant", "admission_record_missing"),
        ("refused", "admission_record_missing"),
        ("wrong-artifact", "admission_artifact_mismatch"),
    ):
        with pytest.raises(OperationRefused) as refused:
            await commit(digest)
        assert refused.value.details["reason"] == reason
    assert all(m != "SubmitChangeset" for m, _, _ in engine.sent)
    assert (await commit("ok"))["value"] == {"committed": True}
    submitted = [p for m, p, _ in engine.sent if m == "SubmitChangeset"]
    assert (
        len(submitted) == 1 and "admission_record_digest" not in submitted[0]["request"]
    )
    human = engine_context()
    human.caller.principal_kind = "human"
    await source.require_admission_record(human, content, None)
    assert SimpleNamespace  # human callers need no admission record
