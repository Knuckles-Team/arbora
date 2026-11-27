# ARBORA-DATA-MARKET — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md). One task per row, producer first. Rows that depend on a downstream contract wait for it; tasks of independent rows run in parallel.

Before a pull request, run `uvx pre-commit run --all-files` at both stages. Follow the repository gate and use local fixtures, never private services.

- [x] T001 [P] [US3] ARBORA-DATA-MARKET-R001.1 Finance receipts move through the engine machine.
- [x] T002 [US3] ARBORA-DATA-MARKET-R002 Scheduled finance backfill, scan, and explanation jobs.
- [x] T003 [P] [US3] ARBORA-DATA-MARKET-R003.1 Market list/search/get.
- [x] T004 [P] [US3] ARBORA-DATA-MARKET-R003.2 Event list/search/get.
- [x] T005 [P] [US3] ARBORA-DATA-MARKET-R003.3 Order list/search/get.
- [x] T006 [P] [US3] ARBORA-DATA-MARKET-R003.4 Stable JSON projections.
- [x] T007 [P] [US3] ARBORA-DATA-MARKET-R003.5 Authority before provider calls.
- [x] T008 [P] [US3] ARBORA-DATA-MARKET-R003.6 Producer port, registration, and read/effect separation.
- [x] T009 [P] [US3] ARBORA-DATA-MARKET-R003.7 Paper order submission.
- [x] T010 [P] [US3] ARBORA-DATA-MARKET-R003.8 Market signal compute operations.
- [x] T011 [P] [US3] ARBORA-DATA-MARKET-R003.9 Served market.size operation over RegimeSizingInput.
- [x] T012 [P] [US3] ARBORA-DATA-MARKET-R003.10 Served market.evaluate operation over medulla NumericEvaluate.
- [x] T013 [P] [US3] ARBORA-DATA-MARKET-R003.11 Served market.positions operation over FinanceAccountProject.
- [x] T014 [P] [US3] ARBORA-DATA-MARKET-R003.12 Research kernel compute operations.
- [x] T015 [P] [US3] ARBORA-DATA-MARKET-R003.13 Market compute port and sizing operations.
- [x] T016 [P] [US3] ARBORA-DATA-MARKET-R003.14 Banking compute operations.
- [x] T017 [P] [US3] ARBORA-DATA-MARKET-R003.15 Finance guide projection.
- [x] T018 [P] [US2] ARBORA-DATA-MARKET-R004 One-at-a-time application admission with rollback.
- [x] T019 [P] [US1] ARBORA-DATA-MARKET-R005 schema_context query surface for tables and ontology mappings.
- [x] T020 [P] [US3] ARBORA-DATA-MARKET-R006 Recurring DCA plans with deterministic, non-duplicating proposals.
- [ ] T021 [P] [US3] ARBORA-DATA-MARKET-R007.1 Model.
- [ ] T022 [P] [US3] ARBORA-DATA-MARKET-R007.2 Behavior.
- [ ] T023 [P] [US3] ARBORA-DATA-MARKET-R007.3 Wiring.
- [ ] T024 [P] [US3] ARBORA-DATA-MARKET-R007.4 Refusal.
- [ ] T025 [P] [US3] ARBORA-DATA-MARKET-R007.5 Evidence.
- [ ] T026 [P] [US3] ARBORA-DATA-MARKET-R007.6 Hot-path proof.
- [ ] T027 [P] [US3] ARBORA-DATA-MARKET-R007.7 Order stage and confirm operations call the proposal door.
- [ ] T028 [P] [US3] ARBORA-DATA-MARKET-R008 Served forecast operation `market.forecast.sample`.
- [ ] T029 [P] [US3] ARBORA-DATA-MARKET-R009 Served reads and imports for the Markets app (rollup).
- [x] T030 [P] [US3] ARBORA-DATA-MARKET-R009.1 Performance and benchmark card read.
- [x] T031 [P] [US3] ARBORA-DATA-MARKET-R009.2 Instrument detail: rates and dividends.
- [x] T032 [P] [US3] ARBORA-DATA-MARKET-R009.3 Strategy recommendation with evidence or an abstain reason.
- [x] T033 [P] [US3] ARBORA-DATA-MARKET-R009.4 DCA and alert delivery receipts read.
- [ ] T034 [P] [US3] ARBORA-DATA-MARKET-R009.5 CSV import preview.
- [ ] T035 [P] [US3] ARBORA-DATA-MARKET-R009.6 CSV import commit with an idempotent receipt.
- [ ] T036 [P] [US3] ARBORA-DATA-MARKET-R009.7 Hot-path proof: the Markets reads over MCP and HTTP.
