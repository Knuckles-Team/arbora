# Stored-query workspace

Seam between `ARBORA-OPS` and Medulla. Owner rows: `MEDULLA-FEDERATED-QUERY-R708.12` in the medulla repository and `ARBORA-OPS-R855.3` in this repository.

Provider: medulla MEDULLA-FEDERATED-QUERY-R708.12
Consumer: arbora ARBORA-OPS-R855.3

## Wire shape

| Operation | Engine method | Answer |
|---|---|---|
| `query.workspace.save` | `StoredQueryPut{query}` | the published version |
| `query.workspace.get` | `StoredQueryGet{name, version?}` | `{name, version, language, text, params, materialize, refresh, share, author, created_at_ms}` |
| `query.workspace.list` | `StoredQueryList{limit?, cursor?, author?, include_retired}` | `{queries[{name, latest_version, author, retired}], next_cursor}` |
| `query.workspace.delete` | `StoredQueryRetire{name, reason}` | `{name, retired, tombstone}` |

## Refusal codes

- `STORED_QUERY_NOT_FOUND`: the name is missing, retired, outside the caller's share scope, or (for a retire) another principal's. Arbora passes it through unchanged and never tells these cases apart.
- `STORED_QUERY_IMMUTABLE`: a put names a retired name.
- `STORED_QUERY_INVALID`: a malformed request or cursor.
- `UNAVAILABLE`: the pinned engine release does not carry the method; Arbora sends nothing.

## Authoritative types

- medulla `crates/medulla-source-types/src/stored_query.rs`: the stored-query refusal codes and list limits.
- `arbora/api/ops/governance.py`: the four workspace handlers.
- `arbora/api/engine_errors.py`: the status of each stored-query code.

## Test vectors

- `tests/api/test_query_workspace_ops.py::test_workspace_operations_map_one_to_one_onto_the_stored_query_methods`.
- `tests/api/test_query_workspace_ops.py::test_get_and_delete_pass_the_engines_not_found_through_unchanged`.
