# Evolution proposal decide

Seam between the engine revision review write and the arbora proposal decision.

Provider: medulla MEDULLA-DECISION-ENGINE-R805.15
Provider: medulla MEDULLA-DECISION-ENGINE-R805.7
Consumer: arbora ARBORA-OPS-R962
Consumer: arbora ARBORA-OPS-R960.9

## Wire shape

- `evolution.proposals.list` returns each proposal's engine stage as `state`: `draft`, `evaluated`, `published`, `champion` or `retired`. A proposal under review is in stage `evaluated`.
- Arbora sends `DecisionLog` with the `review` arm: `RevisionReview { revision_id, decision: approve | veto, digest, reason }`. The reviewer is the verified principal.
- The engine returns `RevisionReviewReceipt { revision_id, stage, receipt_ref }`. `approve` gives stage `published` (edge V02); `veto` gives stage `retired` (edge V06).
- A repeat of the same request returns the stored receipt and writes nothing.

## Refusal codes

| Engine code | Arbora code | Meaning |
|---|---|---|
| `REVISION_NOT_FOUND` | `NOT_FOUND` | The proposal is absent |
| `REVISION_DIGEST_MISMATCH` | `PLAN_MISMATCH` | The digest differs from the one the reviewer saw |
| `TRANSITION_REFUSED` | `TRANSITION_REFUSED` | The proposal is not in stage `evaluated` |
| `REVIEW_SELF_APPROVAL` | `SELF_APPROVAL` | The reviewer is the author |
| (no review arm) | `UNAVAILABLE` | The engine cannot record reviews; no state changes |

## Authoritative types

- `RevisionReview`, `RevisionReviewReceipt` and `RevisionStage` in the medulla decision crates; `ReviewApproval` in `crates/medulla-decision-policy-types/src/decision/held_out.rs`.
- `handle_proposals_decide` in `arbora/api/ops/evolution.py`.

## Test vectors

- `tests/decision/test_revision_review_served.py` in medulla.
- `tests/api/test_evolution_proposals_decide.py`.
