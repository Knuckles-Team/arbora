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

`AcquireCapacity` takes `cell_id`, `priority` (`land_gate`, `train` or `lane` for a `build_slot` cell), `amount` (1), `work_item_id`, `ttl_ms` and an idempotency key; it returns the lease: `lease_id`, `fence_token`, `state` (`granted` or `queued`), `expires_at_ms`, `queue_position`. `StartCapacity { lease_id, fence_token }` moves a granted lease to running. `RenewCapacity { lease_id, fence_token }` extends the deadline by `ttl_ms`. `ReleaseCapacity { lease_id, fence_token }` frees the slot. `RevokeCapacity { lease_id, reason }` is administrative. `CapacityStatus { lease_id }` returns the lease with its newest 50 transitions. The holder identity is the verified caller's digest; it is never a request field.

## Refusal codes

`TRANSITION_REFUSED` (fields `machine`, `from`, `event`, `reason`; reasons include `guard_failed` and `terminal_state`), `PRIORITY_NOT_ALLOWED_FOR_RESOURCE`, `CAPACITY_UNAVAILABLE` (not used for a queued request), and the existing `ACCESS_DENIED` for a missing scope.

## Authoritative types

`CapacityLease`, `CapacityLeaseState`, `CapacityLeaseEvent`, `LeasePriority`, `CapacityResourceClass` in `crates/medulla-mutation-types/src/capacity_lease.rs`; `CapacityStatusResult` in `crates/medulla-work-types/src/native_control.rs`.

## Test vectors

`crates/medulla-protocol-types/tests/vectors/capacity_lease_wire.json` (planned: one vector per method and per refusal code); the engine side asserts the replies, arbora's `tests/api/test_capacity_lease_ops.py` replays the same recorded replies.
