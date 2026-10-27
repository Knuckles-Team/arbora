# Exact provider document-digest review

This record approves **only 567 unchanged expanded-schema identities** in
`schema-digest-reviews.json`. It is not a runtime qualification, release approval,
or waiver for any changed wire contract.

## Bound inputs

- Arbora baseline: `09efd52d96b9f25a90dbfb95a88abf758ade404c`.
- Genuine Medulla canonical artifact: `12753804526`, workflow `40550257423`.
- Producer merge source: `fcf35d0f05b63c3fdd1a9008471de7de214729c9`.
- Producer source tree: `903639bea68bf308613e7ce0d1de17532db4aa4f`.
- Artifact ZIP SHA-256:
  `89c3f930a0ae4533f1a592f8bdfb94242dce725abab6eb703aa460a24de43fb9`.
- Independently reviewed candidate manifest SHA-256:
  `b1a54693042bf60b5f0bea8b2c3ca1f5e32de68b36717fc4bbc7a0408a6ffb91`.

The independent contract-review agent `review_history_snapshot_contract` recomputed all old/new document hashes and full selected-schema hashes.
It compared the expanded schema objects for equality and checked unique identities.
It checked complete coverage of all 589 changed document-digest pairs.
It matched the four candidate schema documents to the actual generated producer documents.
It approved precisely the 567 equal pairs on 2026-10-27. The other **22 pairs
have unequal expanded schemas and are excluded**. The single added operation,
`GetConversationHead`, is not an existing-pair digest waiver.

## Reproduction and limits

Run the existing canonical generator with the exact producer artifacts, then
`python scripts/gen_api.py --check-compat 09efd52d96b9f25a90dbfb95a88abf758ade404c`.
The ledger validates exact operation/field and old/new document and expanded
schema hashes. Stale, unused, duplicate or malformed entries refuse validation;
structural findings cannot be waived. With this record the remaining 22
changed-schema pairs must still refuse compatibility. A green generator source
test is not proof of native legacy-wire parity or completed-text continuation.

Native explicit-opt-in response behavior, caller isolation, no-effect refusal,
and installed paired restart remain separate acceptance gates. No changed
reachable schema is accepted by this metadata-only review.
