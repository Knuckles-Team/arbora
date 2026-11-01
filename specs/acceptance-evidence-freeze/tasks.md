# ARBORA-ACCEPTANCE — Implementation tasks

Governing spec: [spec.md](spec.md). Design: [plan.md](plan.md).

- Inventory `scripts/check_public_specs.py`'s existing evidence schema and structural validation to avoid duplicating its authority.
- [x] T001 ARBORA-ACCEPTANCE-R001.1 implement the typed `EvidenceRecord`/`RequirementGap` model and `gaps_for_status`/`load_status` functions plus the standalone CLI entry point in `scripts/check_acceptance_evidence.py`.
- [x] T002 ARBORA-ACCEPTANCE-R001.1 add the refusal test for a missing/malformed `status.json` alongside the gap-detection unit tests in `tests/test_check_acceptance_evidence.py`.
- [ ] T003 ARBORA-ACCEPTANCE-R002 wire an auditor-identity check (reviewer distinct from the `merged_head` commit author) into the review/merge convention for an `acceptance_state` change.
- [ ] T004 ARBORA-ACCEPTANCE-R003 add the append-only regression test comparing two revisions of a `status.json`.
- [ ] T005 ARBORA-ACCEPTANCE-R004 extend the fixture suite with the stale-operational-evidence case once R001.1 lands.
- [ ] T006 ARBORA-ACCEPTANCE-R005 wire the checker into the repository's pre-commit/CI spec-check group as an informational (non-blocking) worklist report.
- Run focused tests and `scripts/check_public_specs.py`; capture exact revision and results in `status.json`.
- [ ] T007 ARBORA-ACCEPTANCE-R002 Review evidence and change `acceptance_state` only after required gates and the distinct-auditor review in .

## Decomposition children (tracked)

- **ARBORA-ACCEPTANCE-R001:** Typed acceptance-evidence model and freeze-gap checker
- **ARBORA-ACCEPTANCE-R001:** Typed acceptance-evidence model and freeze-gap checker
- [ ] T008 ARBORA-ACCEPTANCE-R001 Typed acceptance-evidence model and freeze-gap checker.
- [ ] T009 ARBORA-ACCEPTANCE-R006 Concept-proof scenarios define done for each delivery phase (rollup).
- [ ] T010 ARBORA-ACCEPTANCE-R006.1 P1 scenario: the minimal closed loop on one node.
- [ ] T018 ARBORA-ACCEPTANCE-R006.1.1 P1 counterfactual control: the retrieved claim changes the next action.
- [ ] T019 ARBORA-ACCEPTANCE-R006.1.2 P1 decision provenance: the run trace records the action and the claim that caused it.
- [ ] T020 ARBORA-ACCEPTANCE-R006.1.3 P1 is the gate: no later phase is delivered before P1 is VERIFIED.
- [ ] T011 ARBORA-ACCEPTANCE-R006.2 P2 scenario: the loop is reliable and inspectable.
- [ ] T012 ARBORA-ACCEPTANCE-R006.3 P3 scenario: boundaries hold with several agents.
- [ ] T013 ARBORA-ACCEPTANCE-R006.4 P4 scenario: the cross-modal bet and memory maintenance.
- [ ] T014 ARBORA-ACCEPTANCE-R006.5 Scenarios run in process on every pull request.
- [ ] T015 ARBORA-ACCEPTANCE-R006.6 An ephemeral deployed stack validates the scenarios nightly.
- [ ] T016 ARBORA-ACCEPTANCE-R006.7 P5 scenario: answers across connectors and across virtual graphs.
- [ ] T017 [US2] ARBORA-ACCEPTANCE-R006.8 Final program scenario: vision questions are answered through the web UI and a messaging channel.
