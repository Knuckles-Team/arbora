"""Explicit policy source evidence cannot be supplied by fallback defaults."""

import json

import pytest

from arbora.access.action_policy import ActionPolicy

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.6")


class _Rules:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def query_cypher(self, query):
        assert "governance_rule" in query
        return self.rows


def _policy(tmp_path, content=None, engine=None):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(content or {"rules": [{"kind": "observe", "tier": "auto"}]})
    )
    return ActionPolicy(engine or _Rules(), policy_path=path)


def test_unconfigured_default_cannot_supply_qualified_revision(monkeypatch):
    monkeypatch.delenv("ACTION_POLICY_PATH", raising=False)
    with pytest.raises(RuntimeError, match="configured"):
        ActionPolicy(_Rules()).qualified_source_revision()


@pytest.mark.parametrize("content", ["", "rules: invalid", "[invalid", "{}"])
def test_invalid_configured_policy_does_not_use_defaults(tmp_path, content):
    policy = _policy(tmp_path)
    policy.policy_path.write_text(content)
    with pytest.raises(RuntimeError):
        policy.qualified_source_revision()


def test_revision_is_normalized_and_content_bound(tmp_path):
    policy = _policy(tmp_path)
    first = policy.qualified_source_revision()
    assert first.startswith("sha256:") and len(first) == 71
    policy.policy_path.write_text("rules:\n  - tier: auto\n    kind: observe\n")
    assert policy.qualified_source_revision() == first
    policy.policy_path.write_text("rules:\n  - tier: forbidden\n    kind: observe\n")
    assert policy.qualified_source_revision() != first


def test_effective_graph_override_is_part_of_revision(tmp_path):
    engine = _Rules()
    policy = _policy(tmp_path, engine=engine)
    first = policy.qualified_source_revision()
    engine.rows.append({"r": {"kind": "observe", "tier": "forbidden"}})
    assert policy.qualified_source_revision() != first


def test_unavailable_override_authority_refuses(tmp_path):
    class Unavailable:
        def query_cypher(self, query):
            raise RuntimeError("offline")

    with pytest.raises(RuntimeError):
        _policy(tmp_path, engine=Unavailable()).qualified_source_revision()


def test_truncated_override_authority_refuses(tmp_path):
    with pytest.raises(RuntimeError):
        _policy(
            tmp_path, engine=_Rules([{"r": {"kind": "observe"}}] * 101)
        ).qualified_source_revision()


def _request(revision):
    from arbora.access.action_policy import ActionRequest

    return ActionRequest(
        kind="observe",
        target="browser:read",
        source="browser_control",
        params={"policy_source_revision": revision},
    )


class _Ledger(_Rules):
    def __init__(self):
        super().__init__()
        self.records = []

    def create_node(self, node_id, label, props):
        self.records.append(props)
        return True


def test_qualified_decision_uses_canonical_evaluator_and_binds_revision(tmp_path):
    policy = _policy(tmp_path, engine=_Ledger())
    revision = policy.qualified_source_revision()
    request = _request(revision)
    decision = policy.decide_qualified(request, expected_revision=revision)
    assert decision.allowed
    assert decision.receipt.request_digest == request.digest()
    assert policy.engine.records[-1]["request_digest"] == request.digest()


def test_missing_or_wrong_request_revision_refuses(tmp_path):
    policy = _policy(tmp_path, engine=_Ledger())
    revision = policy.qualified_source_revision()
    decision = policy.decide_qualified(
        _request("sha256:" + "0" * 64), expected_revision=revision
    )
    assert not decision.allowed


def test_source_change_during_evaluation_refuses(tmp_path, monkeypatch):
    policy = _policy(tmp_path, engine=_Ledger())
    revision = policy.qualified_source_revision()
    original = policy._verify

    def change(request, **kwargs):
        policy.policy_path.write_text("rules: [{kind: observe, tier: forbidden}]")
        return original(request, **kwargs)

    monkeypatch.setattr(policy, "_verify", change)
    decision = policy.decide_qualified(_request(revision), expected_revision=revision)
    assert not decision.allowed


def test_policy_source_secrets_are_not_in_revision_or_repr(tmp_path):
    from arbora.access.action_policy_source import qualified_policy_source

    policy = _policy(
        tmp_path, content={"rules": [], "options": {"private": "private-value"}}
    )
    source = qualified_policy_source(policy)
    assert "private-value" not in repr(source)
    assert "private-value" not in source.revision


def test_override_failure_cannot_be_mistaken_for_empty_rules(tmp_path):
    policy = _policy(tmp_path, engine=_Rules([{"unexpected": "malformed"}]))
    with pytest.raises(RuntimeError):
        policy.qualified_source_revision()


def test_same_snapshot_matching_does_not_reload_legacy_defaults(tmp_path, monkeypatch):
    policy = _policy(tmp_path, engine=_Ledger())
    revision = policy.qualified_source_revision()

    def forbidden_reload():
        raise AssertionError("qualified evaluation must use its captured source")

    monkeypatch.setattr(policy, "_load_file_policy", forbidden_reload)
    monkeypatch.setattr(policy, "_kg_rules", forbidden_reload)
    assert policy.decide_qualified(
        _request(revision), expected_revision=revision
    ).allowed


def test_source_change_during_audit_refuses(tmp_path):
    policy = _policy(tmp_path, engine=_Ledger())
    revision = policy.qualified_source_revision()
    original = policy.engine.create_node

    def change(node_id, label, props):
        policy.policy_path.write_text("rules: [{kind: observe, tier: forbidden}]")
        return original(node_id, label, props)

    policy.engine.create_node = change
    assert not policy.decide_qualified(
        _request(revision), expected_revision=revision
    ).allowed


@pytest.mark.asyncio
async def test_revision_change_across_caller_await_refuses(tmp_path):
    import asyncio

    policy = _policy(tmp_path, engine=_Ledger())
    revision = policy.qualified_source_revision()
    await asyncio.sleep(0)
    policy.policy_path.write_text("rules: [{kind: observe, tier: forbidden}]")
    decision = policy.decide_qualified(_request(revision), expected_revision=revision)
    assert not decision.allowed


def test_removed_operator_file_refuses(tmp_path):
    policy = _policy(tmp_path)
    policy.policy_path.unlink()
    with pytest.raises(RuntimeError):
        policy.qualified_source_revision()


def test_unselected_file_on_disk_does_not_opt_in(tmp_path, monkeypatch):
    _policy(tmp_path)
    monkeypatch.delenv("ACTION_POLICY_PATH", raising=False)
    with pytest.raises(RuntimeError):
        ActionPolicy(_Rules()).qualified_source_revision()


@pytest.mark.parametrize("source", ["yaml", "override", "unicode", "surrogate"])
def test_policy_failure_has_no_secret_exception_chain(tmp_path, source):
    import traceback

    secret = "private-policy-marker-91571"  # sanitizer:ignore - reviewed synthetic redaction fixture, not a credential
    policy = _policy(tmp_path)
    if source == "yaml":
        policy.policy_path.write_text("rules: [" + secret)
    elif source == "surrogate":
        policy.policy_path.write_text(
            json.dumps({"rules": [], "note": secret + "\ud800"})
        )
    elif source == "unicode":
        policy.policy_path.write_bytes(secret.encode() + b"\xff")
    else:

        class Unavailable:
            def query_cypher(self, query):
                raise RuntimeError(secret)

        policy.engine = Unavailable()
    with pytest.raises(RuntimeError) as caught:
        policy.qualified_source_revision()
    error = caught.value
    assert secret not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None


def test_policy_failure_chain_never_exposes_configured_path(tmp_path):
    import traceback

    policy = _policy(tmp_path)
    policy.policy_path = tmp_path / "private-path-marker-91572"
    with pytest.raises(RuntimeError) as caught:
        policy.qualified_source_revision()
    assert "private-path-marker-91572" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert caught.value.__context__ is None


def test_qualified_evaluation_uses_captured_irreversibility(tmp_path, monkeypatch):
    from arbora.access.action_policy import ActionRequest

    policy = _policy(
        tmp_path,
        content={"rules": [{"kind": "delete_test", "tier": "auto"}]},
        engine=_Ledger(),
    )
    policy._irreversibility_aversion = True
    revision = policy.qualified_source_revision()
    verify = policy._verify
    read_revision = policy.qualified_source_revision

    def change_during_verify(request, **kwargs):
        policy._irreversibility_aversion = False
        return verify(request, **kwargs)

    def restore_before_recheck():
        policy._irreversibility_aversion = True
        return read_revision()

    monkeypatch.setattr(policy, "_verify", change_during_verify)
    monkeypatch.setattr(policy, "qualified_source_revision", restore_before_recheck)
    request = ActionRequest(
        kind="delete_test",
        target="browser:read",
        source="browser_control",
        params={"policy_source_revision": revision},
    )
    assert not policy.decide_qualified(request, expected_revision=revision).allowed


def test_qualified_request_substitution_during_audit_refuses(tmp_path):
    policy = _policy(tmp_path, engine=_Ledger())
    revision = policy.qualified_source_revision()
    request = _request(revision)
    create = policy.engine.create_node

    def substitute(node_id, label, props):
        request.target = "forbidden-target"
        request.params["unevaluated"] = True
        return create(node_id, label, props)

    policy.engine.create_node = substitute
    assert not policy.decide_qualified(request, expected_revision=revision).allowed
