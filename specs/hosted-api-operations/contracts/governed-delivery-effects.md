# Governed delivery effects

Seam between `ARBORA-OPS` and the scion-sdk governed write-back. Owner rows: `SCION-GOVERNED-WRITEBACK-R004.11` in scion-sdk and `ARBORA-OPS-R896.11` in this repository.

Provider: scion-sdk SCION-GOVERNED-WRITEBACK-R004.11
Consumer: arbora ARBORA-OPS-R896.11

## Wire shape

Each delivery write is one governed effect `{effect_kind, target, payload_digest, idempotency_key}`. The idempotency key is `delivery_id:assignment_id:step`. The effect kinds are push, open, comment, thread_reply, thread_resolve, close, arm and disarm. The apply returns an immutable receipt `{effect_id, provider_ref, applied_at}`. A restart lists effects without a receipt as `uncertain`.

## Refusal codes

- `EFFECT_UNCERTAIN`: a retry named an uncertain effect before its reconciliation.
- `EFFECT_OVERLAP`: two effects of one connector overlap on one target.

## Authoritative types

- `arbora/api/ops/write_back.py`: `WriteBackPreviewRequest` and `WriteBackPreviewResult`.

## Test vectors

- `tests/api/test_delivery_ops_served.py::test_delivery_writes_are_idempotent_and_uncertain_effects_reconcile`.
