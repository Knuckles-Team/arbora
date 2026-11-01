# ARBORA-DEPLOY-POLICY — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md). Rows: [requirements.md](requirements.md).

Each task line reads `- [ ] T001 [P] [US1] <ID>-R012.4 description`. Tasks run producer first.

- [ ] T001 [US1] ARBORA-DEPLOY-POLICY-R001 Add the typed `deploy.yaml` manifest model, the strict lint and the read-only `deploy.manifest.plan` diff.
- [ ] T002 [US1] ARBORA-DEPLOY-POLICY-R002 Add idempotent `deploy.manifest.apply` convergence with the digest-bound receipt and fail-closed missing inputs.
- [ ] T003 [US2] ARBORA-DEPLOY-POLICY-R003 Add read-only drift inspection and the destructive-change approval gate on the existing elevation model.
- [ ] T004 [P] [US3] ARBORA-DEPLOY-POLICY-R004 Add `policy.bundle.validate` and `policy.bundle.bind` over the engine bundle methods.
- [ ] T005 [P] [US3] ARBORA-DEPLOY-POLICY-R005 Map the engine epistemic refusals to typed refusals in the invoke pipeline.
- [ ] T006 [US3] ARBORA-DEPLOY-POLICY-R006 Serve `llms.txt`, the MCP best-practice resources and the digest-stamped skill export behind the reconciliation gate.
- [ ] T007 [US1] ARBORA-DEPLOY-POLICY-R007 Record each successful rollout through `deployment.record`.
