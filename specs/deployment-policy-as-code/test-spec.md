# ARBORA-DEPLOY-POLICY test-spec

## Positive

- The manifest lint accepts a well-formed `deploy.yaml`. It accepts digest-pinned components.
  `plan` returns the typed diff against the live state. It mutates nothing.
  *(ARBORA-DEPLOY-POLICY-R001)*
- `apply` converges a fresh state to the manifest. It re-runs idempotently with an identical
  digest-bound receipt. *(ARBORA-DEPLOY-POLICY-R002)*
- Drift inspection reports a live change (replica count). It stays read-only.
  *(ARBORA-DEPLOY-POLICY-R003)*
- A compiled policy bundle enforces its scoped rules on a served mutation. Enforcement runs
  through the one access boundary. The same decision reaches the MCP and REST paths.
  *(ARBORA-DEPLOY-POLICY-R004)*
- A write backed by in-validity, high-confidence evidence admits under the epistemic condition.
  *(ARBORA-DEPLOY-POLICY-R005)*
- `llms.txt` and the MCP best-practice resources match the deployed digest. The skill export
  carries the release digest stamp. *(ARBORA-DEPLOY-POLICY-R006)*

## Negative

- An unknown manifest key fails the lint. A duplicate key fails the lint. A floating
  (non-digest) image reference fails the lint.
- A destructive `apply` without an approval receipt refuses. A stale or expired receipt refuses.
  Only a fresh receipt admits the operation.
- A missing apply input names the gap and fails closed. Nothing fabricates. Nothing creates.
- Binding a second bundle to the same scope refuses. A grant under the proposed (not yet
  applied) policy refuses.
- The same write after its evidence expires refuses. A write with missing evidence refs
  refuses. The refusal carries a typed reason. The engine never defaults to allow.
- Agent-surface generation on a stale reconciliation receipt refuses. A missing receipt
  refuses. The last-known-good surface stays served.

## Integration

- Full deployment loop: lint, plan, apply, introduce drift, surface the drift in plan, converge
  the drift through an approved destructive change.
- Policy loop: compile a bundle, bind it to a tenant scope, admit a mutation under it, revise
  the bundle, re-authorize grants only under the newly applied policy.
- Served loop: deploy a version, generate the agent surface, bump the deployed digest,
  regenerate the surface or fail closed.

## Quality and release proof

- Pytest, mypy, ruff, vulture, and the dependency-order gate pass.
- The policy compiler passes the no-duplicate-authority check against the existing RBAC
  evaluator.
- Release proof follows the repo release workflow. Exact-head receipts record in this
  directory.

## Test bindings

Each row's acceptance test binds its ID where the test lands. The lines below record the planned
homes now. The spec-trailers gate resolves the spec PR's trailer. No delivery is claimed.

- `# spec: ARBORA-DEPLOY-POLICY-R001` — `tests/deployment/test_deploy_manifest.py`
- `# spec: ARBORA-DEPLOY-POLICY-R002` — `tests/deployment/test_apply_convergence.py`
- `# spec: ARBORA-DEPLOY-POLICY-R003` — `tests/deployment/test_drift_approval.py`
- `# spec: ARBORA-DEPLOY-POLICY-R004` — `tests/policy/test_policy_bundle.py`
- `# spec: ARBORA-DEPLOY-POLICY-R005` — `tests/policy/test_epistemic_conditions.py`
- `# spec: ARBORA-DEPLOY-POLICY-R006` — `tests/fleet/test_served_agent_surface.py`

## Row coverage

Every test below is planned; none exists yet.

| Row | Test |
|---|---|
| `ARBORA-DEPLOY-POLICY-R001` | `tests/api/test_deploy_manifest_served.py::test_plan_returns_typed_diff_and_makes_no_write_call` and `tests/deployment/test_manifest.py::test_unknown_key_duplicate_key_and_floating_image_are_rejected` |
| `ARBORA-DEPLOY-POLICY-R007` | `tests/api/test_deploy_manifest_served.py::test_rollout_records_one_deployment_and_none_on_failure` |
