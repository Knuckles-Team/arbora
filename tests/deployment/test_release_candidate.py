"""T-RL-01–04/10: candidate contracts and setup-config wiring, no target effects."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json

import pytest
from pydantic import ValidationError

from arbora.deployment.cli import main
from arbora.deployment.genesis_environments import BUILTIN_ENVIRONMENTS_DIR
from arbora.deployment.release_candidate import (
    CandidateError,
    ExitCriteriaError,
    LocalGateResult,
    _canonical_digest,
    gate_release,
    plan_candidate,
    read_candidate,
    read_exit_criteria_matrix,
)

_KEY = b"disposable-test-trust-key"


@pytest.fixture
def authority(monkeypatch):
    """Stand in for the signed-evidence verifier with independent fixture trust.

    The verifier that arbora will own is not bound yet, so production refuses
    every candidate. These consumer tests replace that seam with an HMAC check.
    """
    from arbora.deployment import release_candidate

    calls = []

    def verify(signed):
        calls.append("CERT_EVIDENCE_VERIFIER_COMMAND")
        unsigned = {key: value for key, value in signed.items() if key != "signature"}
        expected = hmac.new(
            _KEY, json.dumps(unsigned, sort_keys=True).encode(), "sha256"
        ).hexdigest()
        if not hmac.compare_digest(str(signed.get("signature", "")), expected):
            raise release_candidate.CandidateError("candidate_signature_unverified")
        return unsigned

    monkeypatch.setattr(release_candidate, "_verify_candidate", verify)
    return calls


@pytest.fixture
def candidate():
    import yaml

    path = BUILTIN_ENVIRONMENTS_DIR / "prod.yaml"
    image = yaml.safe_load(path.read_bytes())["release"]["revision"]
    ids = ("epistemic-graph", "arbora", "agent-webui", "connector.optional")
    artifacts = [
        {
            "component_id": key,
            "repository": "Knuckles-Team/" + key,
            "source_revision": "a" * 40,
            "digest": image,
            "kind": "image",
            "api": "api/v1",
            "schema": "schema/v1",
            "build_receipt": "https://github.com/Knuckles-Team/"
            + key
            + "/commit/"
            + "a" * 40,
        }
        for key in ids
    ]
    predecessors = {"arbora": ["epistemic-graph"], "agent-webui": ["arbora"]}
    stages = [
        {
            "component_id": key,
            "optional": key == "connector.optional",
            "enabled": key != "connector.optional",
            "probe_contract": "functional/v1",
            "predecessors": [
                {"component_id": edge, "api": "api/v1", "schema": "schema/v1"}
                for edge in predecessors.get(key, [])
            ],
        }
        for key in ids
    ]
    return {
        "schema_version": 1,
        "candidate_id": "fixture-release",
        "created_at": "2026-09-30T00:00:00Z",
        "artifacts": artifacts,
        "stages": stages,
        "profiles": [
            {
                "component_id": "arbora",
                "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "artifact_digest": image,
            }
        ],
    }


def _write(tmp_path, raw, *, sign=True):
    signed = copy.deepcopy(raw)
    if sign:
        signed["signature"] = hmac.new(
            _KEY, json.dumps(raw, sort_keys=True).encode(), "sha256"
        ).hexdigest()
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(signed))
    return path, _canonical_digest(raw)


def _plan(tmp_path, raw):
    path, digest = _write(tmp_path, raw)
    return plan_candidate(path, trusted_digest=digest, profile_name="prod")


def test_signed_candidate_order_and_immutable_reader(tmp_path, candidate, authority):
    path, digest = _write(tmp_path, candidate)
    parsed, actual = read_candidate(path, trusted_digest=digest)
    assert actual == digest
    assert isinstance(parsed.artifacts, tuple)
    with pytest.raises(ValidationError):
        parsed.candidate_id = "changed"
    plan = _plan(tmp_path, candidate)
    assert plan["order"] == ["epistemic-graph", "arbora", "agent-webui"]
    assert plan["manifest_digest"] == digest
    assert plan["executed"] is False
    assert set(authority) == {"CERT_EVIDENCE_VERIFIER_COMMAND"}


def test_deterministic_optional_order(tmp_path, candidate, authority):
    candidate["stages"][-1]["enabled"] = True
    first = _plan(tmp_path, candidate)["order"]
    candidate["stages"].reverse()
    candidate["artifacts"].reverse()
    assert _plan(tmp_path, candidate)["order"] == first
    assert first == ["connector.optional", "epistemic-graph", "arbora", "agent-webui"]


@pytest.mark.parametrize("sign", [False, True])
def test_attacker_self_consistent_hash_never_grants_trust(
    tmp_path, candidate, authority, sign
):
    path, digest = _write(tmp_path, candidate, sign=sign)
    if sign:
        raw = json.loads(path.read_text())
        raw["signature"] = "attacker-forged"
        path.write_text(json.dumps(raw))
    with pytest.raises(CandidateError, match="^candidate_signature_unverified$"):
        read_candidate(path, trusted_digest=digest)
    assert len(authority) == 1


def test_tamper_and_digest_selection(tmp_path, candidate, authority):
    path, digest = _write(tmp_path, candidate)
    with pytest.raises(CandidateError, match="candidate_trust_mismatch"):
        read_candidate(path, trusted_digest="sha256:" + "b" * 64)
    raw = json.loads(path.read_text())
    raw["artifacts"][0]["digest"] = "sha256:" + "b" * 64
    path.write_text(json.dumps(raw))
    with pytest.raises(CandidateError, match="candidate_signature_unverified"):
        read_candidate(
            path,
            trusted_digest=_canonical_digest(
                {k: v for k, v in raw.items() if k != "signature"}
            ),
        )


@pytest.mark.parametrize("digest", ["", "latest", "sha256:123", "sha256:" + "A" * 64])
def test_trust_selection_invalid(tmp_path, digest):
    with pytest.raises(CandidateError, match="candidate_trust_required"):
        read_candidate(tmp_path / "absent", trusted_digest=digest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("digest", "latest"),
        ("digest", "sha256:123"),
        ("source_revision", "main"),
        ("kind", "tag"),
        ("repository", "https://private.invalid"),
        (
            "build_receipt",
            "https://github.com/Knuckles-Team/epistemic-graph/actions/runs/1?token=fixture",
        ),
    ],
)
def test_artifact_refusal(tmp_path, candidate, authority, field, value):
    candidate["artifacts"][0][field] = value
    with pytest.raises(CandidateError):
        _plan(tmp_path, candidate)


def test_gate_release_blocks_on_any_red_local_gate(tmp_path, candidate, authority):
    """T-RL-09 (GRAPHOS-RELEASE-R002): a failing local gate blocks the push and
    names it; once every gate is green, the push proceeds."""
    plan = _plan(tmp_path, candidate)
    gates = (
        LocalGateResult(name="k8s-namespace-smoke", green=True),
        LocalGateResult(name="k8s-schema-probe", green=False),
    )
    blocked = gate_release(plan, local_gates=gates)
    assert blocked["allowed"] is False
    assert blocked["status"] == "blocked"
    assert blocked["failed_gates"] == ("k8s-schema-probe",)

    green_gates = tuple(LocalGateResult(name=g.name, green=True) for g in gates)
    allowed = gate_release(plan, local_gates=green_gates)
    assert allowed["allowed"] is True
    assert allowed["status"] == "allowed"
    assert allowed["failed_gates"] == ()


_EXIT_CRITERIA_OBLIGATIONS = (
    "ingestion-receipt",
    "sparql-query-proof",
    "natural-language-query-proof",
    "orchestration-graph",
    "connector-certification",
    "write-back-receipt",
    "browser-identity-login",
    "typed-abstention",
)


def _exit_criteria_rows():
    return [
        {
            "obligation_id": obligation,
            "description": f"{obligation} is proven by its mapped test",
            "test_reference": f"tests/release/test_{obligation.replace('-', '_')}.py",
        }
        for obligation in _EXIT_CRITERIA_OBLIGATIONS
    ]


def test_exit_criteria_matrix_one_row_per_readiness_obligation():
    """T-RL-10 (GRAPHOS-RELEASE-R003.1): a valid matrix carries one row per
    readiness obligation named in GRAPHOS-RELEASE-R003."""
    matrix = read_exit_criteria_matrix(_exit_criteria_rows())
    assert {row.obligation_id for row in matrix.rows} == set(_EXIT_CRITERIA_OBLIGATIONS)


def test_exit_criteria_matrix_refuses_empty():
    with pytest.raises(ExitCriteriaError, match="exit_criteria_empty"):
        read_exit_criteria_matrix([])


def test_exit_criteria_matrix_refuses_duplicate_obligation():
    rows = _exit_criteria_rows()
    rows.append(dict(rows[0]))
    with pytest.raises(ExitCriteriaError, match="exit_criteria_duplicate_obligation"):
        read_exit_criteria_matrix(rows)


def test_exit_criteria_matrix_refuses_malformed_test_reference():
    rows = _exit_criteria_rows()
    rows[0]["test_reference"] = "not a path; rm -rf /"
    with pytest.raises(ExitCriteriaError, match="exit_criteria_row_invalid"):
        read_exit_criteria_matrix(rows)


@pytest.mark.parametrize("field", ["artifacts", "stages"])
def test_duplicate_components(tmp_path, candidate, authority, field):
    candidate[field].append(copy.deepcopy(candidate[field][0]))
    with pytest.raises(CandidateError, match="candidate_duplicate"):
        _plan(tmp_path, candidate)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("created_at", "2026-09-30"),
        ("secret", "fixture-token"),
    ],
)
def test_closed_schema(tmp_path, candidate, authority, field, value):
    candidate[field] = value
    with pytest.raises(CandidateError, match="candidate_schema_invalid"):
        _plan(tmp_path, candidate)


def test_duplicate_json_keys_and_bounded_regular_inputs(tmp_path):
    path = tmp_path / "candidate.json"
    path.write_text('{"schema_version":1,"schema_version":2}')
    with pytest.raises(CandidateError, match="candidate_duplicate_key"):
        read_candidate(path, trusted_digest="sha256:" + "a" * 64)
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(CandidateError, match="candidate_input_too_large"):
        read_candidate(path, trusted_digest="sha256:" + "a" * 64)


@pytest.mark.parametrize("predecessor", ["unknown", "connector.optional"])
def test_missing_or_disabled_predecessor(tmp_path, candidate, authority, predecessor):
    candidate["stages"][1]["predecessors"][0]["component_id"] = predecessor
    with pytest.raises(
        CandidateError, match="candidate_missing_predecessor:arbora:" + predecessor
    ):
        _plan(tmp_path, candidate)


def test_cycle_and_compatibility(tmp_path, candidate, authority):
    candidate["stages"][0]["predecessors"] = [
        {"component_id": "agent-webui", "api": "api/v1", "schema": "schema/v1"}
    ]
    with pytest.raises(CandidateError, match="candidate_cycle"):
        _plan(tmp_path, candidate)
    candidate["stages"][0]["predecessors"] = []
    candidate["stages"][1]["predecessors"][0]["api"] = "api/v2"
    with pytest.raises(CandidateError, match="candidate_incompatible_edge"):
        _plan(tmp_path, candidate)


@pytest.mark.parametrize("field", ["digest", "artifact_digest"])
def test_profile_binding(tmp_path, candidate, authority, field):
    candidate["profiles"][0][field] = "sha256:" + "b" * 64
    with pytest.raises(CandidateError, match="candidate_profile"):
        _plan(tmp_path, candidate)


def test_real_cli_success_and_refusal_are_redacted(
    tmp_path, candidate, authority, capsys
):
    path, digest = _write(tmp_path, candidate)
    argv = ["release-plan", str(path), "--trusted-digest", digest, "--profile", "prod"]
    assert main(argv) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["redacted"] is True
    assert receipt["executed"] is False
    assert receipt["acceptance"] == "not_audited"
    assert "source_revision" in receipt["stages"][0]
    assert "probe" not in receipt
    # Unknown raw material is refused, never reflected in public errors.
    candidate["secret"] = "fixture-token:https://private.invalid"
    path, digest = _write(tmp_path, candidate)
    argv[argv.index("--trusted-digest") + 1] = digest
    assert main(argv) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["executed"] is False
    assert "fixture-token" not in output
    assert "private.invalid" not in output
    assert str(tmp_path) not in output


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_candidate_requires_regular_nonsymlink_file(tmp_path, kind):
    import os

    path = tmp_path / "input"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("{}")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    with pytest.raises(CandidateError, match="^candidate_input_invalid$"):
        read_candidate(path, trusted_digest="sha256:" + "a" * 64)
