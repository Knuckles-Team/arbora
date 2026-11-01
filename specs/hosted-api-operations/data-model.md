# ARBORA-OPS data model

## OpSpec

Frozen pydantic model in `arbora/api/registry/spec.py`. It is one operation's stable wire identity and authority requirements.

| Field | Type | Meaning |
|---|---|---|
| `id` | `str` | Stable dotted operation name. |
| `verb` | `Verb` | The intent tool that serves it: find, ask, why, write, act or manage. |
| `binding` | `EgMethod` or `Composite` | Engine method or dotted Arbora service handler. |
| `scopes`, `executor_scopes` | `frozenset[str]` | Caller scopes, and the scopes of a service-executed call. |
| `effect`, `principals`, `audit`, `idempotency` | enums | Authority, principal rule, audit class and idempotency mode. |
| `surfaces`, `http` | `frozenset[Surface]`, `HttpShape` | Served transports and the REST method and path. |

`Registry` in `arbora/api/registry/registry.py` holds the specs, produces the canonical form and the SHA-256 registry digest.

## IngestSourceRecord

Frozen pydantic model in `arbora/ingest/service.py`. It is one ingestion source's identity and current state.

| Field | Meaning |
|---|---|
| `source_id`, `tenant`, `state` | Source identity (`<server>/<source>` for connector sources), owning tenant and state. |
| `connector`, `record_type`, `incremental`, `cursor`, `records`, `last_job_id` | Connector source declaration and progress. |
| `sync_state`, `sync_reason`, `sync_engine_code` | Outcome of the last sync. |

`ConnectorIngestRunner` in `arbora/ingest/connector_sources.py` produces these records for connector sources.
