# City state

Seam between the engine city-state facts and the arbora presence read and heartbeat write.

Provider: medulla MEDULLA-WORLD-VIEW-R004
Consumer: arbora ARBORA-OPS-R915.1
Consumer: arbora ARBORA-OPS-R915.2

## Wire shape

- `CityStateReader.snapshot(tenant)` returns `CityStateSnapshot { revision, simulated_at, layout_revision, entities }`. Each entity is `{ id, kind, x, y, heading, activity_state, target_ref, observed_at, provenance, expires_at }`. `kind` is `npc`, `ai` or `human`.
- `CityStateWriter.heartbeat(subject, expected_revision, x, y, heading, activity_state, target_ref)` returns `{ revision }`. `activity_state` and `target_ref` are the new arguments that `ARBORA-OPS-R915.2` passes; until the provider accepts them, arbora refuses them with `UNAVAILABLE`. It writes the subject's one `human` node through `CompareAndSetNodeFields` on `:revision`.

## Refusal codes

- `REVISION_CONFLICT`: `expected_revision` is stale.
- `SUBJECT_ACCESS_DENIED`: the write names another subject.
- `UNAVAILABLE`: the engine is not bound.

## Authoritative types

- `medulla/world_view.py` in medulla.
- `CityStateSnapshot`, `CityPresenceEntity` and `HeartbeatParams` in `arbora/api/ops/presence.py`.

## Test vectors

- `tests/test_world_view_city_state.py` in medulla.
- `tests/api/test_city_presence_state_served.py`.
