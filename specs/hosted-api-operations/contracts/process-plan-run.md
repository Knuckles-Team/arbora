# Seam: Process plan served and run

Provider: medulla `MEDULLA-CQ-ORCH-R704.5` (plan) and `MEDULLA-DECISION-ENGINE-R180.4` (branch and table verdicts); meristem `MERISTEM-RUN-R018.5` (run loop).
Consumer: arbora `ARBORA-OPS-R898`.

## Wire shape

Arbora sends `Decide` with `QuestionKind::ProcessPlan` and `{workflow_id, version}`. The engine returns the sealed task plan, its digest and one `StepBinding` per step. Arbora maps agent steps to prompt text and passes control steps to `iterate_run` unchanged. During the run the decision port sends `Decide` `BranchSelect` with the plan digest and the branch task id. The approval waiter blocks on `process.approval.decide`.

## Refusal codes

Engine: `WORKFLOW_UNKNOWN`, `PROCESS_STEP_UNBOUND`, `PROCESS_PLAN_LOOP`, `PROCESS_PLAN_TOO_LARGE`, `BRANCH_NO_ARM`, `DMN_NO_RULE_MATCHED`. Arbora: `APPROVAL_NOT_PENDING`, `APPROVER_NOT_IN_ROLE`, `PROCESS_PROMPT_UNRESOLVED`.

## Authoritative types

Engine `StepBinding` and the control steps of the task plan; meristem `RunTask` control fields and the `ApprovalWaiter` port; arbora `ProcessRunParams` and `ProcessApprovalDecideParams` in `arbora/api/ops/processes.py`.

## Test vectors

The served tests named by `ARBORA-OPS-R898.8` and by the engine rows hold the vectors that both sides test against.
