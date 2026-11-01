# ARBORA-DATA-MARKET — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md). One task per row, producer first. Rows that depend on a downstream contract wait for it; tasks of independent rows run in parallel.

Before a pull request, run `uvx pre-commit run --all-files` at both stages. Follow the repository gate and use local fixtures, never private services.

- [ ] T001 [P] [US3] ARBORA-DATA-MARKET-R001.1 Durable finance receipt store.
- [ ] T002 [US3] ARBORA-DATA-MARKET-R002 Scheduled finance backfill, scan, and explanation jobs.
- [ ] T003 [P] [US3] ARBORA-DATA-MARKET-R003.1 Market list/search/get.
- [ ] T004 [P] [US3] ARBORA-DATA-MARKET-R003.2 Event list/search/get.
- [ ] T005 [P] [US3] ARBORA-DATA-MARKET-R003.3 Order list/search/get.
- [ ] T006 [P] [US3] ARBORA-DATA-MARKET-R003.4 Stable JSON projections.
- [ ] T007 [P] [US3] ARBORA-DATA-MARKET-R003.5 Authority before provider calls.
- [ ] T008 [P] [US3] ARBORA-DATA-MARKET-R003.6 Producer port, registration, and read/effect separation.
- [ ] T009 [P] [US3] ARBORA-DATA-MARKET-R003.7 Paper order submission.
- [ ] T010 [P] [US3] ARBORA-DATA-MARKET-R003.8 Market signal compute operations.
- [ ] T011 [P] [US3] ARBORA-DATA-MARKET-R003.9 Served market.size operation over RegimeSizingInput.
- [ ] T012 [P] [US3] ARBORA-DATA-MARKET-R003.10 Served market.evaluate operation over medulla NumericEvaluate.
- [ ] T013 [P] [US3] ARBORA-DATA-MARKET-R003.11 Served market.positions operation over FinanceAccountProject.
- [ ] T014 [P] [US3] ARBORA-DATA-MARKET-R003.12 Research kernel compute operations.
- [ ] T015 [P] [US3] ARBORA-DATA-MARKET-R003.13 Market compute port and sizing operations.
- [ ] T016 [P] [US3] ARBORA-DATA-MARKET-R003.14 Banking compute operations.
- [ ] T017 [P] [US3] ARBORA-DATA-MARKET-R003.15 Finance guide projection.
- [ ] T018 [P] [US2] ARBORA-DATA-MARKET-R004 One-at-a-time application admission with rollback.
- [ ] T019 [P] [US1] ARBORA-DATA-MARKET-R005 schema_context query surface for tables and ontology mappings.
- [ ] T020 [P] [US3] ARBORA-DATA-MARKET-R006 Recurring DCA plans with deterministic, non-duplicating proposals.
- [ ] T021 [P] [US3] ARBORA-DATA-MARKET-R007.1 Model.
- [ ] T022 [P] [US3] ARBORA-DATA-MARKET-R007.2 Behavior.
- [ ] T023 [P] [US3] ARBORA-DATA-MARKET-R007.3 Wiring.
- [ ] T024 [P] [US3] ARBORA-DATA-MARKET-R007.4 Refusal.
- [ ] T025 [P] [US3] ARBORA-DATA-MARKET-R007.5 Evidence.
- [ ] T026 [P] [US3] ARBORA-DATA-MARKET-R007.6 Hot-path proof.
- [ ] T027 [P] [US3] ARBORA-DATA-MARKET-R007.7 Order stage and confirm operations call the proposal door.
- [ ] T028 [P] [US3] ARBORA-DATA-MARKET-R008 Served forecast operation `market.forecast.sample`.
