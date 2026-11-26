"""The ``arbora-identity`` lifecycle commands over a recorded engine (ARBORA-IDENTITY-R013.1, R013.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arbora.identity import cli


class Admin:
    """A recorded engine ``Identity`` port: answers by family and op."""

    def __init__(self, answers: dict[tuple[str, str], Any]) -> None:
        self.answers = answers
        self.sent: list[tuple[str, str, dict[str, Any] | None]] = []

    async def identity(
        self,
        client: Any,
        claims: Any,
        family: str,
        op: str,
        request: Any = None,
        *,
        expect: str,
    ) -> Any:
        self.sent.append((family, op, request))
        answer = self.answers[(family, op)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Refused(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class Clock:
    now = 10_000.0

    def __call__(self) -> float:
        return self.now


def _run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    answers: dict[tuple[str, str], Any],
    clock: Clock | None = None,
) -> tuple[int, dict[str, Any], Admin, list[str], list[tuple[str, bool, str]]]:
    admin = Admin(answers)
    shown: list[str] = []
    audits: list[tuple[str, bool, str]] = []
    env = cli.Environment(
        admin=admin,
        client=object(),
        claims={},
        ring=cli.KeyRing(tmp_path / "ring", clock or Clock()),
        prompt_password=lambda: "typed-at-the-prompt",
        show_once=shown.append,
        audit=lambda command, ok, code: audits.append((command, ok, code)),
    )
    code = cli.main(argv, environment=lambda: env)
    out = json.loads(capsys.readouterr().out)
    return code, out, admin, shown, audits


@pytest.mark.spec("ARBORA-IDENTITY-R013.1")
def test_claim_and_link_claim_typed_results(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = {
        ("config", "get"): Refused("IDENTITY_NOT_INITIALIZED"),
        ("user", "list"): Refused("IDENTITY_NOT_INITIALIZED"),
    }
    code, out, admin, _, audits = _run(
        tmp_path,
        capsys,
        ["claim", "--username", "root"],
        {**fresh, ("config", "initialize"): {"principal_id": cli.BOOTSTRAP_PRINCIPAL}},
    )
    assert code == 0 and out["ok"] is True and out["command"] == "claim"
    initialize = [r for f, o, r in admin.sent if o == "initialize"][0]
    assert initialize["admin_password"] == "typed-at-the-prompt"
    assert "typed-at-the-prompt" not in json.dumps(out)
    assert audits == [("claim", True, "")]

    # The wire: ``config.get`` answers the IdentityConfig record; ``user.list``
    # answers the bare list of views and takes ``{limit}`` (``after`` optional).
    done = {("config", "get"): {"epoch": 1, "mode": "local"}}
    code, out, *_ = _run(tmp_path, capsys, ["claim", "--username", "root"], done)
    assert code == 2 and out["detail"]["code"] == "CLAIM_ALREADY_DONE"

    used = {
        ("config", "get"): Refused("IDENTITY_NOT_INITIALIZED"),
        ("user", "list"): [{"principal_id": "usr:u"}],
    }
    code, out, admin, *_ = _run(tmp_path, capsys, ["claim", "--username", "root"], used)
    assert code == 2 and out["detail"]["code"] == "STORE_NOT_FRESH"
    assert [r for f, o, r in admin.sent if o == "list"] == [{"limit": 1}]

    # PIN-LAG: the pinned engine has no ``idp.link`` dry run, so link-claim
    # refuses and sends nothing.
    link = ["link-claim", "--provider", "idp-1", "--subject", "s-1"]
    monkeypatch.setattr(cli, "_link_dry_run_served", lambda: False)
    code, out, admin, *_ = _run(tmp_path, capsys, link, {})
    assert code == 2 and out["detail"]["code"] == "LINK_DRY_RUN_UNAVAILABLE"
    assert admin.sent == []

    # Engine main: the dry run answers the ownership changes, then the link runs.
    monkeypatch.setattr(cli, "_link_dry_run_served", lambda: True)
    clean = {
        ("idp", "link"): {"ownership_changes": []},
    }
    code, out, admin, *_ = _run(tmp_path, capsys, link, clean)
    assert code == 0 and out["ok"] is True
    assert [r.get("dry_run") for _, _, r in admin.sent] == [True, None]

    moved = {("idp", "link"): {"ownership_changes": [{"principal_id": "usr:other"}]}}
    code, out, admin, *_ = _run(tmp_path, capsys, link, moved)
    assert code == 2 and out["detail"]["code"] == "LINK_OWNERSHIP_CHANGES"
    assert len(admin.sent) == 1


@pytest.mark.spec("ARBORA-IDENTITY-R013.2")
def test_transition_reset_and_rotate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transition = {
        ("config", "get"): {"epoch": 4},
        ("token", "read_issuer_keys"): [
            {"kid": "kid-1", "state": "active"},
            {"kid": "kid-0", "state": "retired"},
        ],
        ("config", "transition"): {},
        ("user", "list"): [{"principal_id": "u1"}, {"principal_id": "u2"}],
        ("session", "revoke_all"): {},
    }
    code, out, admin, *_ = _run(
        tmp_path, capsys, ["transition", "--to", "external"], transition
    )
    assert code == 0 and out["detail"]["sessions_revoked"] == 2
    sent = {op: r for _, op, r in admin.sent if op == "transition"}["transition"]
    assert sent == {"expected_epoch": 4, "issuer_kid": "kid-1", "to": "external"}
    assert [r for _, op, r in admin.sent if op == "revoke_all"] == [
        {"id": "u1"},
        {"id": "u2"},
    ]

    stale = {**transition, ("config", "transition"): Refused("IDENTITY_EPOCH_CONFLICT")}
    code, out, *_ = _run(tmp_path, capsys, ["transition", "--to", "local"], stale)
    assert code == 2 and out["detail"]["code"] == "TRANSITION_PRECONDITION_FAILED"

    code, out, admin, shown, _ = _run(
        tmp_path,
        capsys,
        ["reset-admin"],
        {("token", "issue_admin_reset"): {"email": None}},
    )
    assert code == 0 and len(shown) == 1 and shown[0] not in json.dumps(out)
    issued = admin.sent[0][2]
    assert (
        issued["token"] == shown[0]
        and issued["principal_id"] == cli.BOOTSTRAP_PRINCIPAL
    )

    clock = Clock()
    rotate = {("token", "rotate_issuer_key"): {}}
    code, out, admin, *_ = _run(tmp_path, capsys, ["rotate"], rotate, clock)
    assert code == 0 and (tmp_path / "ring" / f"{out['detail']['kid']}.pem").is_file()
    assert (
        admin.sent[0][2]["algorithm"] == "EdDSA"
        and len(admin.sent[0][2]["public_key_hex"]) == 64
    )

    clock.now += 10  # inside the overlap window: the previous key is still valid
    code, out, admin, *_ = _run(tmp_path, capsys, ["rotate"], rotate, clock)
    assert code == 2 and out["detail"]["code"] == "ROTATION_OVERLAP_ACTIVE"
    assert admin.sent == []
