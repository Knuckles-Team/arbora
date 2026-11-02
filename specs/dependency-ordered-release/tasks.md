# ARBORA-RELEASE — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md). One task per row, producer first.

- [x] T001 [US1] ARBORA-RELEASE-R001.1 A version tag publishes the wheel and the sdist to PyPI with no manual approval.
- [x] T002 [US1] ARBORA-RELEASE-R002 Gate every push on green local gates and run the static architecture tests on every pull request (`gate_release`, `scripts/pr_static_scope.py`); wire `gate_release` into `release-plan`.
- [x] T003 [US2] ARBORA-RELEASE-R003.1 Typed `ExitCriterionRow` and `ExitCriteriaMatrix` with `read_exit_criteria_matrix` and `audit_exit_criteria`, with refusal tests.
- [ ] T004 [US2] ARBORA-RELEASE-R003.2 Create `deploy/release/exit-criteria.json` with seven rows and the ingestion-receipts served test.
- [ ] T005 [US2] ARBORA-RELEASE-R003.3 Add the SPARQL and natural-language query-proof served test and matrix row.
- [x] T006 [US3] ARBORA-RELEASE-R003.4.1 Serve `agents.plan_run`: engine `TaskPlan` decision, checked receipt, harness `DependencyPlan` execution.
- [x] T007 [US3] ARBORA-RELEASE-R003.4.2 Publish the plan to the work item through `PublishCurrentPlan` for replay.
- [ ] T008 [US3] ARBORA-RELEASE-R003.4.3 Keep the `meristem` and `medulla` floors in `pyproject.toml` and `uv.lock` at the first releases that serve `TaskPlan`.
- [ ] T009 [US3] ARBORA-RELEASE-R003.4.4 Pin the exported task-plan schema body, check it at start-up and publish it once per tenant.
- [ ] T010 [US3] ARBORA-RELEASE-R003.4.5 Governed fleet tools and caller in plan steps; supply the governed `ToolCaller`, keyed by record, task and call.
- [ ] T011 [US3] ARBORA-RELEASE-R003.4.6 Accept typed tool steps in `agents.plan_run` and pass engine refusals through.
- [ ] T012 [US3] ARBORA-RELEASE-R003.4.8 Return resolved arguments, results and outcome per step in the plan-run reply.
- [ ] T013 [US3] ARBORA-RELEASE-R003.4.9 Wire `handle_plan_run` to the engine calls, the governed caller and the step records.
- [ ] T014 [US3] ARBORA-RELEASE-R003.4.10 Prove cross-connector data passing through the served operation.
- [x] T015 [US3] ARBORA-RELEASE-R003.4.11 Give served plan runs a replay owner (step effects keyed from the bound request identity).
- [ ] T016 [US4] ARBORA-RELEASE-R003.4.12 Bind idempotent active-child input to real owner targets, lifecycle and durable delivery receipts.
- [ ] T017 [US4] ARBORA-RELEASE-R003.4.13 Admit completed-child continuation with authorized history, fresh turn budgets and installed end-to-end evidence.
- [ ] T018 [US2] ARBORA-RELEASE-R003.5 Add the connector-certification served test and matrix row.
- [ ] T019 [US2] ARBORA-RELEASE-R003.6 Add the write-back receipt served test and matrix row.
- [ ] T020 [US2] ARBORA-RELEASE-R003.7 Add the browser-and-identity-provider login probe test and matrix row.
- [ ] T021 [US2] ARBORA-RELEASE-R003.8 Add the typed-abstention response test and matrix row.
- [ ] T022 [US2] ARBORA-RELEASE-R003.9 Add the fleet acceptance row: four checks per connector, reasoned skips only, one FAIL blocks promotion.
- [ ] T023 [US2] ARBORA-RELEASE-R003.10 Wire `plan_candidate` to audit the exit-criteria matrix against a junit report (`release-plan --junit`).
