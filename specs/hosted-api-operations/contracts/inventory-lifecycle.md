# Inventory lifecycle functions

Seam between the medulla host lifecycle machine and the arbora lifecycle step of the inventory build.

Provider: medulla MEDULLA-MACHINE-R007
Consumer: arbora ARBORA-OPS-R959.5

## Wire shape

Arbora derives facts from probe payloads and calls two functions of module `medulla.engine`.

- `machine_lifecycle_derive(state, facts_json)` returns `{"event": "Ready"|"Drain"|"Drained"|"Resume"|"Retire"|null}`.
- `machine_lifecycle_apply(state, event, facts_json)` returns `{"state","edge_id"}`.

The facts object has six boolean keys: `reachable`, `ready`, `health_failing`, `drain_requested`, `workloads_evacuated` and `retire_requested`. A state is one of `commissioned`, `active`, `draining`, `maintenance`, `retired`. Arbora passes `commissioned` for a machine with no prior state.

## Refusal codes

- `TRANSITION_REFUSED`: `machine_lifecycle_apply` raises `ValueError` with the text `TRANSITION_REFUSED: MachineLifecycleMachine refused '<event>' from '<state>' (<reason>)`. Reasons are `illegal_transition`, `guard_failed`, `terminal_state` and `unknown_event`. Arbora passes the typed record through. Arbora applies only an event that `derive` returned, so a refusal signals a defect.

## Authoritative types

- `crates/medulla-source-types/src/machine/lifecycle.rs` in medulla: `MACHINE_LIFECYCLE_MACHINE`, `MachineLifecycleFacts`.
- `arbora/inventory/facts.py`: `lifecycle_facts`, `lifecycle_step`.

## Test vectors

- `crates/medulla-source-types/src/machine/lifecycle.rs::tests::derive_event_table_matches_the_row` in medulla.
- `tests/inventory/test_lifecycle_evidence.py::test_each_edge_fires_from_its_evidence` in arbora.
