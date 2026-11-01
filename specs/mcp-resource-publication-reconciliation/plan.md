# ARBORA-MCP-RESOURCES — Implementation plan

## Summary

Delivery phase: P5

No scenario of P1 to P4 exercises these rows, so they serve P5. They wait for the P4 gate (`ARBORA-ACCEPTANCE-R006.4`).

This plan serves the rows of `ARBORA-MCP-RESOURCES` in requirements.md. It covers mCP resource/template publication reconciliation. The design record follows in the sections below.

## Technical Context

- **Language/Version**: Python 3.14, with a 3.12 floor
- **Primary Dependencies**: the arbora served registry and the Medulla client adapter from the SDK
- **Storage**: N/A, because the engine owns every fact
- **Testing**: pytest with one bound test per row, at the proof level the row names
- **Target Platform**: Linux service
- **Project Type**: service
- **Performance Goals**: N/A, because rows state their own limits
- **Constraints**: one door, and the engine owns every decision
- **Scale/Scope**: the rows of this spec only

### Development environment

No new runtime dependency, service, or fixture. `arbora/fleet/mcp_resource_reconciliation.py`
and its tests run in the existing local test environment with no network, DNS, secret, or live EG
tenant required — the gate takes a receipt as a plain argument.

## Constitution Check

- R-I One door, no policy table: pass. Every decision is read from the engine.
- R-II Every module is reachable from a production caller: pass. Each row names its caller chain.
- R-III Public specs and core skills are complete and self-contained: pass.

## Project Structure

- `specs/mcp-resource-publication-reconciliation/` (existing): spec, requirements, plan, tasks, test-spec and checklist.
- Code paths: each row names its files in its `Shape:` sentence. This plan adds no other path.

## Phase 0: Research

No unknowns remain. The sections below record the earlier research and decisions.

## Phase 1: Design

The sections below hold the architecture, ownership, data flow and failure modes of this spec.

### Existing wiring to reuse

| Existing seam | Role in this design | Change boundary |
|---|---|---|
| `arbora/fleet/catalog_reader.py` (`FleetCatalogIntegrityError`, `ReadReceipt`, `_verify_receipt`) | Prior art for a typed, fail-closed receipt check against an exact context/provenance match | Pattern only. Not modified or replaced by this spec |
| `specs/fleet-catalog-and-tools/` (`ARBORA-FLEET-R051`) | Owns the atomic generation-publish state machine this gate will be called from | One pre-swap gate call for four MCP families (`ARBORA-MCP-RESOURCES-R003`). Reload state machine unchanged |
| medulla `specs/repository-index-and-ingestion` (MEDULLA-REPO-INGEST-001) | Produces the durable commit and the reconciliation receipt | Consumed by ID only; this repository defines no Medulla-side behavior |
| `docs/status.md`, `docs/fleet.md` | Existing public description of the `reingestion-unreconciled` refusal | Kept truthful by `ARBORA-MCP-RESOURCES-R004`; not restructured |

This is a contract spec, not a new store: arbora holds no second copy of the four-family set
and no second durability record. The only new code in this repository is the typed receipt model
and the pure gate function. Everything else is a wiring obligation on an existing seam.

### Data model

```text
medulla durable commit (MEDULLA-REPO-INGEST-001)
    -> ReconciliationReceipt {tenant_id, catalog_generation, snapshot_digest,
                               acknowledged_families, issued_at_ms}
    -> gate_mcp_resource_publication(receipt, candidate, now, max_age)
    -> PublicationGateResult {status: reconciled | reingestion-unreconciled,
                               reason: missing | stale | invalid | None,
                               receipt}
    -> catalog publish step (`ARBORA-FLEET-R051`) reads .status before swap
```

`ReconciliationReceipt` and `PublicationGateResult` are frozen dataclasses (`slots=True`); the gate
is a pure function with no I/O, no caching, and no local persistence — the receipt is supplied by
the caller, who is responsible for having actually asked medulla for it.

### Sequence

1. `ARBORA-MCP-RESOURCES-R001.1` (this PR): typed model + pure gate + refusal/acceptance tests.
2. `ARBORA-MCP-RESOURCES-R002`: confirm the EG producing contract (ID/link only, no code in this
   repository).
3. `ARBORA-MCP-RESOURCES-R003`: wire the gate into the `ARBORA-FLEET-R051` publish step so a
   candidate generation cannot swap in for the four MCP families without a `reconciled` result.
4. `ARBORA-MCP-RESOURCES-R004`: confirm `docs/status.md` / `docs/fleet.md` match the gate's actual
   status/reason vocabulary.

## Phase 2: Task approach

tasks.md lists one task per row, producer first. Tasks of independent rows run in parallel.

## Complexity Tracking

None. The constitution check passed.

## Build contract

The change must pass the repository pre-commit gates at both stages: `uvx pre-commit run --all-files` and `uvx pre-commit run --all-files --hook-stage pre-push`.
The wiring gate (`scripts/check_wiring.py`) and the public-spec gate (`scripts/check_public_specs.py`) apply. A new module must have a production caller.

## Production caller chain

Each row names its entry point and caller chain in its own text. This plan adds no separate chain.

## Cross-repo seams

None owned here. The `Depends on` lines of requirements.md name each dependency by the row ID that owns it in the other repository.
