# Architecture and implementation plan

## Existing wiring to reuse

| Existing seam | Role in this design | Change boundary |
|---|---|---|
| `arbora/fleet/catalog_reader.py` (`FleetCatalogIntegrityError`, `ReadReceipt`, `_verify_receipt`) | Prior art for a typed, fail-closed receipt check against an exact context/provenance match | Pattern only. Not modified or replaced by this spec |
| `specs/fleet-catalog-and-tools/` (ARBORA-FLEET-001 R022, PA-12) | Owns the atomic generation-publish state machine this gate will be called from | This spec adds the one pre-swap gate call for the four MCP families (`ARBORA-MCP-RESOURCES-R003`); it does not redesign the reload state machine |
| epistemic-graph `specs/repository-index-and-ingestion` (EG-REPO-INGEST-001) | Produces the durable commit and the reconciliation receipt | Consumed by ID only; this repository defines no EG-side behavior |
| `docs/status.md`, `docs/fleet.md` | Existing public description of the `reingestion-unreconciled` refusal | Kept truthful by `ARBORA-MCP-RESOURCES-R004`; not restructured |

This is a contract spec, not a new store: arbora holds no second copy of the four-family set
and no second durability record. The only new code in this repository is the typed receipt model
and the pure gate function. Everything else is a wiring obligation on an existing seam.

## Data model

```text
epistemic-graph durable commit (EG-REPO-INGEST-001)
    -> ReconciliationReceipt {tenant_id, catalog_generation, snapshot_digest,
                               acknowledged_families, issued_at_ms}
    -> gate_mcp_resource_publication(receipt, candidate, now, max_age)
    -> PublicationGateResult {status: reconciled | reingestion-unreconciled,
                               reason: missing | stale | invalid | None,
                               receipt}
    -> catalog publish step (ARBORA-FLEET-001 R022) reads .status before swap
```

`ReconciliationReceipt` and `PublicationGateResult` are frozen dataclasses (`slots=True`); the gate
is a pure function with no I/O, no caching, and no local persistence — the receipt is supplied by
the caller, who is responsible for having actually asked epistemic-graph for it.

## Sequence

1. `ARBORA-MCP-RESOURCES-R001.1` (this PR): typed model + pure gate + refusal/acceptance tests.
2. `ARBORA-MCP-RESOURCES-R002`: confirm the EG producing contract (ID/link only, no code in this
   repository).
3. `ARBORA-MCP-RESOURCES-R003`: wire the gate into the ARBORA-FLEET-001 publish step so a
   candidate generation cannot swap in for the four MCP families without a `reconciled` result.
4. `ARBORA-MCP-RESOURCES-R004`: confirm `docs/status.md` / `docs/fleet.md` match the gate's actual
   status/reason vocabulary.

## Development environment

No new runtime dependency, service, or fixture. `arbora/fleet/mcp_resource_reconciliation.py`
and its tests run in the existing local test environment with no network, DNS, secret, or live EG
tenant required — the gate takes a receipt as a plain argument.
