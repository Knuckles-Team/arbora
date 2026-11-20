"""A refusal shows its error code and message, never a secret (ARBORA-DEPLOY-R019.4)."""

from __future__ import annotations

import json

import pytest

from arbora.deployment import production_ops
from arbora.deployment.production_ops import refusal_detail

# Built at run time so the source holds no credential-shaped literal.
_KEY = "sk" + "-live-" + "0123456789abcdef"
_PASSWORD = "hunter" + "2hunter2"
_QUERY_VALUE = "Zm9v" + "YmFyYmF6cXV4"
_JWT = ".".join(("ey" + "JhbGciOiJIUzI1NiJ9", "ey" + "JzdWIiOiJ4In0", "c2lnbmF0dXJl"))
_PLANTED = (_KEY, _QUERY_VALUE, _PASSWORD, _JWT)


class _EngineRefusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@pytest.mark.spec("ARBORA-DEPLOY-R019.4")
def test_refusal_prints_code_and_message_and_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The code and message of an engine refusal are shown.
    detail = refusal_detail(
        _EngineRefusal("IMPORTER_MISMATCH", "connector is not bound to this principal")
    )
    assert detail["error_code"] == "IMPORTER_MISMATCH"
    assert "not bound to this principal" in detail["error_message"]
    assert detail["error_type"] == "_EngineRefusal"

    # A code is also read from the message when the exception carries none.
    assert refusal_detail(RuntimeError("REUSE_UNREACHABLE: gone"))["error_code"] == (
        "REUSE_UNREACHABLE"
    )
    assert "error_code" not in refusal_detail(ValueError("plain message"))

    # A planted secret never appears, in any common shape.
    nasty = RuntimeError(
        f"SERVER_REFUSED: call failed with Authorization: Bearer {_KEY}"
        f" at https://svc:{_PASSWORD}@host.example/x?api_key={_QUERY_VALUE}"
        f" token={_KEY} jwt {_JWT} ref secret://vault/path"
    )
    shown = json.dumps(refusal_detail(nasty))
    for secret in (*_PLANTED, "secret://vault/path"):
        assert secret not in shown
    assert "[redacted" in shown

    # The message is bounded.
    assert len(refusal_detail(RuntimeError("X" * 5000))["error_message"]) <= 400

    # The command prints the code and message, with a digest and without a secret.
    def refuse(_args: object) -> dict[str, object]:
        raise _EngineRefusal("PACK_REFUSED", f"password={_PLANTED[2]} not accepted")

    monkeypatch.setattr(production_ops, "_run", refuse)
    monkeypatch.setattr("arbora.core.settings.load_config", lambda: None)
    assert production_ops.main(["check-fleet-coverage"]) == 1
    printed = capsys.readouterr().out
    report = json.loads(printed.strip().splitlines()[-1])
    assert report["ok"] is False
    assert report["error_code"] == "PACK_REFUSED"
    assert "not accepted" in report["error_message"]
    assert report["error_digest"].startswith("sha256:")
    assert _PLANTED[2] not in printed
