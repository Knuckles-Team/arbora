# ARBORA-HOST data model

## ActionOperationMapping

Frozen dataclass in `arbora/mcp_server/legacy_action_mapping.py`. It maps one retired legacy MCP action onto one registry operation.

| Field | Type | Meaning |
|---|---|---|
| `action` | `str` | The legacy MCP action name. |
| `operation` | `str` | The registry operation id that serves the action. |

`build_approved_action_mapping` turns a tuple of entries into a read-only lookup table. It raises `DuplicateActionMappingError` for a repeated action. `validate_served_actions` raises `UnmappedActionError` for an action with no entry.
The planned `retired_by` field (`ARBORA-HOST-R005.2.9`) names the owner row of an action that Arbora no longer serves. Exactly one of `operation` and `retired_by` is set.

## DataMigration

Engine node with label `DataMigration` and id `data_migration:<migration_id>`. `arbora/data_migrations.py` writes it through `create_record` once a migration leaves no rows behind.

| Field | Meaning |
|---|---|
| `migration_id` | Ordered, unique migration id. |
| `label` | The record label the migration repairs. |
| `summary` | One-line description. |
| `shape_digest` | SHA-256 over the label and the sorted field names. |
| `found`, `changed`, `remaining` | Row counts. |
| `arbora_version`, `applied_at`, `applied_unix` | Version and time of the run. |
