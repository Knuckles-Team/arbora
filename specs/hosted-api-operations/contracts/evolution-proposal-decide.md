# Evolution proposal decide

Seam between the engine revision review and the arbora proposal decision.

Provider: medulla MEDULLA-DECISION-ENGINE-R805.10
Consumer: arbora ARBORA-OPS-R962

## Wire shape

- Arbora reads the proposal through `RevisionProposalList` and checks its digest.
- Arbora asks the engine to record one `ReviewApproval` receipt for the proposal: `{ proposal_id, decision: approve | veto, digest, reviewer, reason }`. The engine returns `{ receipt_ref, stage }`.

## Refusal codes

- `NOT_FOUND`: the proposal is absent.
- `PLAN_MISMATCH`: the digest differs from the one the reviewer saw.
- `TRANSITION_REFUSED`: the proposal is not under review.
- `SELF_APPROVAL`: the reviewer is the proposer.
- `UNAVAILABLE`: the engine has no review write; no state changes.

## Authoritative types

- `ReceiptKind::ReviewApproval` and `RevisionSubject` in the medulla decision crates.
- `handle_proposals_decide` in `arbora/api/ops/evolution.py`.

## Test vectors

- `tests/api/test_evolution_proposals_decide.py`.
