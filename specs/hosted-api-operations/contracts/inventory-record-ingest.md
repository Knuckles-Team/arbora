# Inventory record ingest

Seam between the arbora inventory build and the engine ingest path. The build submits one reconcile batch through `SourceIngest`. The engine validates every machine against its shapes.

Provider: medulla MEDULLA-MACHINE-R006.4
Consumer: arbora ARBORA-OPS-R959.7

The engine shapes of the probe snapshot and the transition record are part of the same path.

## Wire shape

The SDK facade `KnowledgeIngest.submit` sends one `ChangeSet` with mode `RECONCILE`. The binding is connector `inventory-records` and stream `machines`. `live_ids` lists every entity id of the record set. The engine method is `SourceIngest`. Entity types and relations are those of the `MEDULLA-MACHINE` data model: `Machine`, the seven part types, `ProbeSnapshot`, `MachineLifecycleTransitionRecord`, and the relations `hasProcessor`, `hasMemoryModule`, `hasStorageDevice`, `hasNetworkInterface`, `hasNetworkBond`, `hasAccelerator`, `hasManagementController`, `bondMember`, `observes` and `transitionOf`. Each `has...` relation is a sub-property of the core `hasComponent` with exactly one range, because a pack relation is keyed by its domain and property. A record states the specific relation of its part kind and never `hasComponent`.

The description reads the same facts back with one `Sparql` request. The query selects `?s ?p ?o` for the canonical machine, its parts, its observers and its transition records.

## Refusal codes

- `MAPPING_UNDECLARED`: the captured `inventory-records` declaration lacks a record type. Arbora refuses before the call.
- `INGEST_REFUSED`: the engine refused the batch. The detail carries the engine code.
- `SHAPE_VIOLATION` (engine code): a machine or part failed its shape. Arbora passes it through as the detail of `INGEST_REFUSED`.

## Authoritative types

- `scion_sdk.ingest`: `ChangeSet`, `Entity`, `Relationship`, `IngestBinding`.
- `crates/medulla-source-types/src/machine/records.rs` in medulla.

## Test vectors

- `tests/inventory/test_ingest_writer.py::test_one_reconcile_changeset_carries_every_entity_and_relationship`.
- `tests/test_machine_inventory_ingest_served.py::test_records_reconcile_into_the_tenant_graph` in medulla.
