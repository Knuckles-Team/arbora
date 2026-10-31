# ARBORA-RELEASE — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md).

- [x] Inventory existing release profile, doctor certification, canary, production operations and self-deploy interfaces. Agree on candidate schema with pipeline and repository-manager owners. `arbora.deployment.release_candidate` reuses `genesis_environments.load_environment_profile` for the profile binding rather than adding a second parser.
- [x] Add one validated candidate manifest reader and deterministic dependency graph to the existing deployment path. Reject untrusted, mutable, cyclic and incompatible candidates. Landed as `arbora.deployment.release_candidate` (`Candidate`/`Artifact`/`Stage`/`Profile` typed models, `read_candidate`, `_dependency_graph`, `_order`) with `tests/deployment/test_release_candidate.py` (32 cases). The cases cover deterministic topological order with a stable component-ID tie-break (`test_deterministic_optional_order`). They also cover cycle refusal (`test_cycle_and_compatibility`, `candidate_cycle`), missing/disabled-predecessor refusal (`test_missing_or_disabled_predecessor`), and closed-schema/duplicate/digest-tag/signature refusals. No rollout execution and no rollback yet — `plan_candidate` only plans (`executed: False`); see the next two tasks.
- [ ] Connect digest-pinned profile apply to stage readiness and meaningful functional probes; record exact revision and digest at each stage.
- [ ] Implement stop, retry and rollback decisions with observed post-rollback checks; expose partial recovery honestly.
- [ ] Run every T-RL positive/negative fixture in portable CI and release-only hosted qualification at the exact candidate digest.
- [ ] Publish public receipt and operator instructions; change status only when landed and acceptance evidence independently exists.

## ARBORA-RELEASE-R001

- [x] `ARBORA-RELEASE-R001.1` — a version tag publishes the wheel and the sdist to PyPI with no manual approval.

## ARBORA-RELEASE-R003 (split 2026-10-09: net-new, 7 readiness-obligation code roots)

- [ ] `ARBORA-RELEASE-R003.1` — typed `ExitCriterionRow`/`ExitCriteriaMatrix` model plus `read_exit_criteria_matrix` validation, with refusal tests for a duplicate obligation ID, an empty matrix, and a malformed test-ID reference.
- [ ] `ARBORA-RELEASE-R003.2` — the one entry point that loads a real matrix (committed fixture or CLI command) and the ingestion-receipt row's mapped test.
- [ ] `ARBORA-RELEASE-R003.3` — SPARQL and natural-language query-proof rows.
- [ ] `ARBORA-RELEASE-R003.4` — multi-agent orchestration-graph row.
- [x] `ARBORA-RELEASE-R003.4.1` — served `agents.plan_run`: engine `TaskPlan` decision, checked receipt, harness `DependencyPlan` execution, engine refusals unchanged (c22, c21).
- [x] `ARBORA-RELEASE-R003.4.2` — publish the plan to the work item through `PublishCurrentPlan` for replay.
- [ ] `ARBORA-RELEASE-R003.4.3` — raise the engine floor to `medulla[server]>=2.27.5` once it is on the package index (agent-utilities 3.1.0 floor done); then rerun the served tests on the real engine facade.
- [ ] `ARBORA-RELEASE-R003.4.4` — pin the exported schema body with `gen_api`, check it at start-up and publish it once per tenant. After EG 2.27.5, confirm the export module, raise the floor and regenerate.
- [ ] `ARBORA-RELEASE-R003.4.5` — governed fleet tools and caller in plan steps. Done: `fleet_tools`, `fleet_call`, `run.fleet_calls`. Open: supply the governed `ToolCaller` for typed tool steps. Key it by record, task and call. Write one audit event per call.
- [ ] `ARBORA-RELEASE-R003.4.6` — Accept typed tool steps in `agents.plan_run`; pass engine refusals through.
- [ ] `ARBORA-RELEASE-R003.4.8` — Return resolved arguments, results and outcome per step.
- [ ] `ARBORA-RELEASE-R003.4.9` — Wire `handle_plan_run` to the engine calls, the caller and the step records.
- [ ] `ARBORA-RELEASE-R003.4.10` — Prove cross-connector data passing through the served operation.
- [x] `ARBORA-RELEASE-R003.4.11` — served plan runs have a replay owner (step effects keyed from the bound request identity).
- [ ] `ARBORA-RELEASE-R003.5` — connector-certification row.
- [ ] `ARBORA-RELEASE-R003.6` — write-back receipt row.
- [ ] `ARBORA-RELEASE-R003.7` — browser-and-identity-provider login probe row.
- [ ] `ARBORA-RELEASE-R003.8` — typed-abstention response row.
- [ ] `ARBORA-RELEASE-R003.9` — fleet acceptance row: four checks per connector, reasoned skips only, one FAIL blocks promotion.
- [ ] A test-suite audit confirms every exit-criteria row resolves to a passing automated test before `ARBORA-RELEASE-R003` itself is marked LANDED.
## Decomposition children (tracked)

- [ ] **ARBORA-RELEASE-R003:** Exit-criteria matrix maps readiness to tests
- [ ] **ARBORA-RELEASE-R003.1:** Remaining scope of ARBORA-RELEASE-R003 (slice .1): Exit-criteria matrix maps readiness to tests
- [ ] **ARBORA-RELEASE-R003.2:** Remaining scope of ARBORA-RELEASE-R003 (slice .2): Exit-criteria matrix maps readiness to tests

## Explicit child interaction

- [ ] `ARBORA-RELEASE-R003.4.12` — Bind idempotent active-child input to real owner targets, lifecycle and durable delivery receipts; retain unavailable state until upstream support exists.
- [ ] `ARBORA-RELEASE-R003.4.13` — Admit completed-child continuation with authorized history, fresh turn budgets and installed end-to-end evidence.
