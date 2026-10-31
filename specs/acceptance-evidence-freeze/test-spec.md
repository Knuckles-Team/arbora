# ARBORA-ACCEPTANCE — Test specification

| Test ID | Requirement | Level | Setup and input | Expected observation | Evidence |
|---|---|---|---|---|---|
| T-001 | ARBORA-ACCEPTANCE-R001, ARBORA-ACCEPTANCE-R001.1 | Unit | A fixture `status.json` with one requirement carrying `merged_head`, `test`, and `consumer` evidence all at the same commit. | `gaps_for_status` returns an empty list for that requirement. | PENDING |
| T-002 | ARBORA-ACCEPTANCE-R001, ARBORA-ACCEPTANCE-R001.1 | Unit | A fixture `status.json` with a `LANDED` requirement carrying only `merged_head` evidence. | `gaps_for_status` returns a `RequirementGap` naming that requirement with `missing_kinds` containing `test` and `operational`. | PENDING |
| T-003 | ARBORA-ACCEPTANCE-R003 | Unit | A fixture `status.json` with a requirement whose `consumer` receipt is recorded at an older commit than its current `merged_head`. | `gaps_for_status` reports `operational` as missing even though a `consumer` entry exists in the array. | PENDING |
| T-004 | ARBORA-ACCEPTANCE-R001.1 | Refusal | `load_status` called against a path that does not exist, and against a path containing invalid JSON. | Both calls raise `AcceptanceEvidenceError`; neither returns an empty or default-passing result. | PENDING |
| T-005 | ARBORA-ACCEPTANCE-R004 | CLI | `main()` invoked against the fixture directory from T-001/T-002 combined. | Process exits `1`, prints exactly the T-002 requirement's ID and missing kinds, and running it twice in a row produces byte-identical stdout. | PENDING |

## Negative and boundary cases

A requirement absent from `status.json`'s `requirements` array is out of this checker's scope (that absence is already a `check_public_specs.py` structural error). A requirement in `SPECIFIED` or `BUILDING` state is never reported as a gap — only `LANDED`/`CLOSED` rows are candidates for `ACCEPTED`. A `status.json` that is syntactically valid JSON but not an object (for example, a bare array) refuses via `AcceptanceEvidenceError` rather than raising an unrelated `AttributeError`.

## Quality and release proof

Run `python3 -m pytest tests/test_check_acceptance_evidence.py -q` from the repository root. Run `python3 scripts/check_public_specs.py` to confirm this spec's own four documents and `status.json` satisfy the existing structural gate unchanged. Record the exact commit and pass/fail in this spec's `status.json` evidence array before any requirement here moves past `SPECIFIED`.

## Entries added for rows without a prior entry

| Test ID | Requirement | Expected observation |
|---|---|---|
| T-ARBORA-ACCEPTANCE-R006.5 | ARBORA-ACCEPTANCE-R006.5 | Test: `tests/scenarios/test_scenario_stack.py::test_stack_serves_mcp_and_http_over_one_engine`. |
| T-ARBORA-ACCEPTANCE-R006.6 | ARBORA-ACCEPTANCE-R006.6 | Test: `tests/scenarios/test_ephemeral_stack.py::test_compose_stack_ready_within_budget` runs in the nightly job. |
| T-ARBORA-ACCEPTANCE-R006.7 | ARBORA-ACCEPTANCE-R006.7 | Test: `tests/scenarios/test_p5_cross_source.py::test_answers_join_connectors_and_virtual_graphs_with_coverage` runs in the nightly job against the ephemeral stack. |

## Entries added for rows without a prior entry

| Test ID | Requirement | Expected observation |
|---|---|---|
| T-ARBORA-ACCEPTANCE-R006.1 | ARBORA-ACCEPTANCE-R006.1 | Test: `tests/scenarios/test_p1_closed_loop.py::test_write_retrieve_under_policy_and_trace` runs arbora, a loopback medulla server and an in-process meristem run. |
| T-ARBORA-ACCEPTANCE-R006.2 | ARBORA-ACCEPTANCE-R006.2 | Test: `tests/scenarios/test_p2_reliable_loop.py::test_lineage_conflict_and_memory_lift`. |
| T-ARBORA-ACCEPTANCE-R006.3 | ARBORA-ACCEPTANCE-R006.3 | Test: `tests/scenarios/test_p3_multi_agent_boundaries.py::test_shared_state_under_policy_with_contradiction`. |
| T-ARBORA-ACCEPTANCE-R006.4 | ARBORA-ACCEPTANCE-R006.4 | Test: `tests/scenarios/test_p4_cross_modal_and_maintenance.py::test_one_plan_validation_and_decay`. |
