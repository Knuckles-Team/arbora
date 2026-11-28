# Tool-call page

Seam between the engine harness statement `tool_call_page` and the arbora activity read.

Provider: medulla MEDULLA-ORCH-VOCAB-R818
Consumer: arbora ARBORA-OPS-R915
Consumer: arbora ARBORA-OPS-R916
Consumer: arbora ARBORA-OPS-R916.1

## Wire shape

Arbora sends `Method::Uql` with the statement `tool_call_page { after?, limit? }`. Each row is `{ cursor, run, step, tool, server, outcome, duration_ms, sandboxed, observed_at }`, newest first. `limit` is 1 to 200, default 100. `after` is the opaque cursor of the last row. No row holds an argument or result value.

## Refusal codes

- `INVALID_ARGUMENT`: the cursor is unknown or malformed. Arbora checks the cursor pattern first.
- `SUBJECT_ACCESS_DENIED`: the caller lacks run read access on the tenant's runs.

## Authoritative types

- `HARNESS_QUERIES` in `crates/medulla-mutation-types/src/harness_metrics.rs` in medulla.
- `ActivityStepRecord` in `arbora/api/ops/activity.py`.

## Test vectors

- `tests/orchestration/test_harness_metrics_served.py` in medulla.
- `tests/api/test_activity_ops_served.py`.
