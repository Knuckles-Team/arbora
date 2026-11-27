# Seam: capacity lease wire

Provider: medulla MEDULLA-PLACEMENT-R786
Provider: medulla MEDULLA-PLACEMENT-R786.2
Provider: medulla MEDULLA-PLACEMENT-R786.4
Provider: medulla MEDULLA-PLACEMENT-R786.7
Consumer: arbora ARBORA-CAPACITY-R754
Consumer: arbora ARBORA-CAPACITY-R754.1
Consumer: arbora ARBORA-CAPACITY-R754.2
Consumer: arbora ARBORA-CAPACITY-R754.4
Consumer: arbora ARBORA-CAPACITY-R754.5

## Wire shape

Every capacity request carries `schema_version` ("1") and `tenant_ref`. The tenant is the verified caller's tenant and the owner is the verified caller's persistence id (`principal:sha256:<hex>`); arbora derives both from the verified caller (ARBORA-CAPACITY-R754.2) and no served parameter sets either. A held lease is named by its fence, `{ lease_id, lease_epoch, fence_token }`.

- `AcquireCapacity` takes `work_item_id`, `owner_digest`, `idempotency_key`, `priority` (`land_gate`, `train` or `lane` for a `build_slot` cell), `demands` (`[{ cell_id, resource_class: "build_slot", amount: 1 }]`), `lease_id` (null), `ttl_ms`, `now_ms`, `cost_budget_micros` (null), `token_budget` (null) and `queue_ttl_ms` (optional). It returns `decision` (`accepted`), `leases` (the lease with `state` `granted` or `queued`; a queued lease also carries `queue_seq` and `queue_expires_at_ms`), `available` and `queue_position` (counted from 1, present only when the lease is queued).
- `StartCapacity`, `RenewCapacity` and `ReleaseCapacity` share one request shape, the lease mutation request: `owner_digest`, `leases` (a list of fences), `now_ms`, `ttl_ms` (optional; a renew extends the deadline by it) and `idempotency_key` (optional). They return `decision` (`started`, `renewed`, `released`) and `leases` carrying the new state (`running`, `running`, `released`). A request names exactly one fence for a build slot.
- `RevokeCapacity` takes `lease_id`, `reason` (at most 200 characters, no control character) and `now_ms`; it names no fence and no owner, because only `capacity:admin` may call it. It returns `decision` `revoked` and the lease in state `revoked`.
- `CapacityStatus` takes `cell_id` (optional), `lease_id` (optional), `max_count` and `cursor`; it returns `cells`, `leases`, `next_cursor` and `transitions` (per lease, its newest 50 transitions, oldest first).
- `CapacityLeaseState` is `requested`, `queued`, `granted`, `running`, `released`, `expired` or `revoked`. The retired texts `active` and `reclaimed` are not read: arbora treats any other text as an unknown state and refuses it.
- The lease record carries no `ttl_ms`; the holder knows the `ttl_ms` it asked for, and `expires_at_ms` is the deadline.
- The engine is the authority on every transition. Arbora holds no pool, queue or lease state.

## Refusal codes

`TRANSITION_REFUSED` (fields `machine`, `from`, `event`, `reason`; reasons are `guard_failed` for a non-owner, `terminal_state` for a lease already released, expired or revoked, and `illegal_transition` for an event the state does not accept), `PRIORITY_NOT_ALLOWED_FOR_RESOURCE`, and the existing `ACCESS_DENIED` for a missing scope. `CAPACITY_UNAVAILABLE` is not emitted for a build-slot cell: a request against a full cell is accepted and queued. The engine registers no such code, so arbora declares none.

## Authoritative types

`CapacityLease`, `CapacityLeaseState`, `CapacityLeaseEvent`, `LeasePriority`, `CapacityResourceClass` in `crates/medulla-mutation-types/src/capacity_lease.rs`; `CapacityStatusResult` in `crates/medulla-work-types/src/native_control.rs`.

## Test vectors

`crates/medulla-protocol-types/tests/vectors/capacity_lease_wire.json` (planned: one vector per method and per refusal code); the engine side asserts the replies, arbora's `tests/api/test_capacity_lease_ops.py` replays the same recorded replies.
