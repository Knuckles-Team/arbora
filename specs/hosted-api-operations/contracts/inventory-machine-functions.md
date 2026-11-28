# Inventory machine functions

Seam between the medulla machine vocabulary and the arbora inventory build and description. Arbora calls the pure functions of the engine Python wheel and copies none of them.

Provider: medulla MEDULLA-MACHINE-R007
Consumer: arbora ARBORA-OPS-R959.2

The medulla row `MEDULLA-MACHINE-R007` exposes the identity, hosts-file and digest functions that the arbora row `ARBORA-OPS-R959.2` calls. The lifecycle functions have their own contract, `inventory-lifecycle.md`.

## Wire shape

Module `medulla.engine` holds seven functions. The identity, key anchor, hosts-file and digest functions are the subject here. Each takes and returns text. Structured values are canonical JSON.

| Function | Input | Output |
|---|---|---|
| `normalize_hosts_file(yaml)` | the hosts file text | `{"schema":"hosts-normalized/v1","hosts":[...]}` |
| `machine_normalize_key(kind, raw)` | kind in `uuid`, `serial`, `bmc`, `host` | `{"kind","value"}` |
| `machine_iri(tenant, keys_json)` | `[{"kind","value"}]` | `{"iri","tier","key"}` |
| `machine_key_ref(kind, raw)` | same as `machine_normalize_key` | `{"iri","key"}` with the key anchor id |
| `machine_lifecycle_derive(state, facts_json)` | stored state, six boolean facts | `{"event": name or null}` |
| `machine_lifecycle_apply(state, event, facts_json)` | stored state, event name, facts | `{"state","edge_id"}` |
| `canonical_digest(text)` | canonical JSON text | `sha256:<64 hex>` |

The facts object has the keys `reachable`, `ready`, `health_failing`, `drain_requested`, `workloads_evacuated` and `retire_requested`.

## Refusal codes

- `HOSTS_FILE_INVALID`: a `ValueError` whose text is the JSON record `{code, reason, pointer}`. Arbora returns it as the served refusal of the same code.
- A key refusal reason (`malformed`, `placeholder`, `empty`): arbora drops a `placeholder` key and refuses the host on the others with `HOSTS_FILE_INVALID`.
- `no_valid_key`: cannot occur, because the hostname key is always valid.
- `TRANSITION_REFUSED`: a `ValueError` whose text is `TRANSITION_REFUSED: MachineLifecycleMachine refused '<event>' from '<state>' (<reason>)`. Arbora passes the typed record through.

## Authoritative types

- `crates/medulla-pyengine/src/machine.rs` in medulla.
- `crates/medulla-source-types/src/machine/` in medulla.

## Test vectors

- `crates/medulla-source-types/tests/vectors/machine_iri.json` in medulla holds the identity vectors. The `data-model.md` of `MEDULLA-MACHINE` lists the seven values.
- `tests/inventory/test_build_records.py::test_same_hosts_and_snapshot_give_byte_identical_records` in arbora.
