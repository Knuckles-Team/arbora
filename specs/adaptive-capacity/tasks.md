# Delivery tasks and evidence

- [ ] T001 ARBORA-CAPACITY-R001 Identify the current fleet admission point and published engine CapacityCell/lease methods; record exact package versions and contract digest.
- [ ] T002 ARBORA-CAPACITY-R001 Add a pure, partitioned AIMD controller and deterministic budget-window fixtures.
- [x] T003 ARBORA-CAPACITY-R001.1 (child, producer): `arbora/fleet/error_budget.py` + `tests/fleet/test_error_budget.py`.
- [ ] T004 ARBORA-CAPACITY-R001 Connect one limiter to the existing fleet dispatch path; prove observe mode does not enforce.
- [ ] T005 ARBORA-CAPACITY-R002 Add `capacity.throttle.status` and `capacity.throttle.set_mode` to the shared hosted operation registry with exact scopes, audit reservation and parity tests.
- [ ] T006 ARBORA-CAPACITY-R001 Prove fail-closed stale premise, unavailable policy/audit, tenant isolation and restart behavior.
- [ ] T007 ARBORA-CAPACITY-R001 Run all portable source, scanner and package gates on an exact commit; attach CI and artifact identity.
- [ ] T008 ARBORA-CAPACITY-R001 Run disposable served fault injection for observe and enforce modes; attach redacted decision and refusal receipts.

**Evidence:** None recorded for this complete Arbora contract. Existing fleet/control-plane source is wiring inventory, not acceptance proof.

## Additional owner requirements

- [ ] T009 ARBORA-CAPACITY-R001.2 - Deliver every requirement in requirements.md.
- [ ] T010 ARBORA-CAPACITY-R003 - Deliver every requirement in requirements.md.
- [ ] T011 ARBORA-CAPACITY-R004 - Bind the AU driver and implement the served load profile, complete report, and threshold refusals in plan.md.
- [ ] T012 ARBORA-CAPACITY-R001.3 - Deliver every requirement in requirements.md.
- [ ] T013 ARBORA-CAPACITY-R001.4 - Add the outcome recorder and the controller loop that calls `decide` and stores the decision, as requirements.md defines.

## Row coverage

- [ ] T014 ARBORA-CAPACITY-R001.3.1 Add the capacity proposal builder in `arbora/fleet/capacity_proposals.py`.
- [ ] T015 ARBORA-CAPACITY-R001.3.2 Add the engine verdict call `judge` in `arbora/fleet/capacity_proposals.py`.
- [ ] T016 ARBORA-CAPACITY-R001.3.3 Add `apply` for allowed proposals, covering narrowing, a ceiling raise and a scaling intent.
- [ ] T017 ARBORA-CAPACITY-R001.3.4 Add applied-state `read_back` and the `capacity.throttle.receipt.get` operation.
- [ ] T018 ARBORA-CAPACITY-R001.5 Add `PartitionLimiter` at the fleet forward path, with `CAPACITY_THROTTLED` refusals.
- [ ] T019 ARBORA-CAPACITY-R750.750 Wire `validate_scale_intent` of `scaling_authority.py` into the controller apply step.
- [ ] T020 ARBORA-CAPACITY-R750.751 Wire `AuthenticatedSignalProvider` of `scaling_signal_authority.py` into the controller signal read.
- [ ] T021 ARBORA-CAPACITY-R750.752 Wire `ScaleIntentReconciler` into the controller apply step for replica changes.
- [ ] T022 ARBORA-CAPACITY-R750.753 Wire `evaluate_scale` of `service_scale_units.py` into the controller loop.
- [ ] T023 ARBORA-CAPACITY-R750.754 Wire `place` of `resource_pool_authority.py` into the controller placement step.
- [ ] T024 ARBORA-CAPACITY-R750.755 Wire the reservation and development-lane transports into the controller.
- [ ] T025 ARBORA-CAPACITY-R751 Gate every limit raise on an engine `allow` verdict and record the receipt.
- [ ] T026 ARBORA-CAPACITY-R752 Add `ModelCapacityGate` that applies the engine per-model decision with a fallback of 1.
- [ ] T027 ARBORA-CAPACITY-R753 Pass `max_working_bytes` from the host grant on each `IndexRepository` job.
