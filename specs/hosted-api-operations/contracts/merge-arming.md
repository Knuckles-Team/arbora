# Merge arming

Seam between `ARBORA-OPS`, Medulla and the code-host connectors. Owner rows: `MEDULLA-SDLC-R019.10` in medulla, the code-host connector rows that `MEDULLA-SDLC-R019` lists, `PIPE-GATES-R016.116.1` in pipelines and `ARBORA-OPS-R896.10` in this repository.

Provider: medulla MEDULLA-SDLC-R019.10
Consumer: arbora ARBORA-OPS-R896.10

## Wire shape

Arbora sends `Decide{question: MergeArming, delivery_id, proposal, head_sha, approval_ref?}`. The engine answers `{verdict: arm or disarm or hold, premises[], decision_id}`. On `arm` arbora calls the connector arm tool with `{proposal, expected_head_sha, approval_ref}`. On `disarm` it calls the disarm tool with `{proposal}`. The approval reference is a receipt of `ARBORA-OPS-R863` issued to a human owner and bound to the head commit digest.

## Refusal codes

- `MERGE_APPROVAL_REQUIRED`: no owner receipt names the head commit.
- `MERGE_HEAD_MISMATCH`: the head moved after the receipt.
- `MERGE_BY_PLATFORM_REFUSED`: the platform principal called a merge or arm tool directly.
- `ARMING_APPROVAL_MISSING`, `ARMING_HEAD_MISMATCH`: the engine refusals that arbora passes through.

## Authoritative types

- medulla `crates/medulla-source-types/src/delivery.rs`: `ArmingVerdict`.
- `arbora/api/ops/development.py`: `handle_delivery_arm`.

## Test vectors

- `tests/api/test_delivery_ops_served.py::test_arm_needs_owner_receipt_on_head_and_platform_never_merges`.
