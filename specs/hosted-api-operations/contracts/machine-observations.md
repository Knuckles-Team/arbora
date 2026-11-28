# Machine pressure observations

Seam between the arbora inventory build and the engine machine vocabulary for pressure readings.

Provider: medulla MEDULLA-MACHINE-R010
Consumer: arbora ARBORA-OPS-R959.13

## Wire shape

The build sends a second `ChangeSet` with mode `APPEND` over the binding `IngestBinding(connector="inventory-records", stream="machine-observations")`. Each entity is one `MachineObservation` with `observesMachine` (the canonical machine IRI), `observedAt`, and at least one of `diskUsedFraction` (0 to 1), `fanRpm` (a list of integers), `temperatureC` and `load1m`.

## Refusal codes

- `INGEST_REFUSED` with the engine code `SHAPE_VIOLATION`: an observation failed `:MachineObservationShape`.
- `MAPPING_UNDECLARED`: the captured declaration lacks the `MachineObservation` resource.

## Authoritative types

- `crates/medulla-core/ontology/machine-v1.ttl` and `machine-v1.shapes.ttl` in medulla.
- `arbora/inventory/observations.py` (planned by R959.13).

## Test vectors

- `tests/test_machine_inventory_ingest_served.py::test_the_newest_observation_answers_each_machine` in medulla.
- `tests/inventory/test_observations.py`.
