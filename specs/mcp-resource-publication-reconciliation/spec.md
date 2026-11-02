# ARBORA-MCP-RESOURCES — MCP resource/template publication reconciliation

Owner: arbora. Cross-repo IDs: `MEDULLA-REPO-INGEST-001`.

## Outcome

Arbora is the public MCP door for the durable four-family set (tools, prompts, resources, resource templates). Medulla owns the durable graph from which those families are projected. Arbora never claims to have published a candidate generation unless medulla handed back a receipt that reconciles the exact candidate against the durable record. Without that receipt, Arbora returns the typed `reingestion-unreconciled` result named in [`docs/status.md`](../../docs/status.md) and [`docs/fleet.md`](../../docs/fleet.md). Arbora stands up no second, process-local durability store.

This spec defines the reconciliation receipt that Arbora needs from medulla before it publishes, and the typed refusal it returns until that receipt exists. It does not replace the fleet catalog reload design in [`fleet-catalog-and-tools/spec.md`](../fleet-catalog-and-tools/spec.md), which owns `ARBORA-FLEET-R022` and `ARBORA-FLEET-R051`. This spec narrows that design for the MCP resource and template publication step.

## Delivery mapping

Component: Fleet catalog publication

Code: ../../arbora/fleet/mcp_resource_reconciliation.py

## User Scenarios & Testing

### User Story 1 - List resources with proof (Priority: P1)

An MCP client lists resources or resource templates and sees either the reconciled four-family set or a typed refusal, never a guess at what the durable graph holds.

**Acceptance Scenarios**:

1. **Given** a fresh receipt that matches the candidate tenant, generation, digest and all four families, **When** the publish step runs, **Then** the gate returns `reconciled` and the swap proceeds.
2. **Given** a missing, stale or mismatched receipt, **When** the publish step runs, **Then** the gate returns `reingestion-unreconciled` with reason `missing`, `stale` or `invalid` and the last known good generation stays active.

### User Story 2 - Trust the status page (Priority: P1)

An operator reads the "Explicitly unavailable surfaces" row of `docs/status.md` and finds the same result that the gate returns.

**Acceptance Scenarios**:

1. **Given** the gate vocabulary, **When** the status docs are checked, **Then** each status and reason named by the gate appears in `docs/status.md` and `docs/fleet.md`.

### User Story 3 - Produce the receipt (Priority: P2)

A medulla contributor builds the producer of the receipt against a public, stable shape.

**Acceptance Scenarios**:

1. **Given** the `ReconciliationReceipt` fields, **When** medulla issues a receipt after a durable commit, **Then** Arbora accepts it only for the exact tenant, generation and digest.

### Edge Cases

- A receipt that acknowledges fewer than four families is invalid.
- A receipt older than the freshness bound is stale and never cached past expiry.
- Arbora never synthesizes a passing receipt.

## Requirements

- **FR-001**: The publication gate refuses without a valid receipt. Rows: `ARBORA-MCP-RESOURCES-R001`, `ARBORA-MCP-RESOURCES-R001.1`.
- **FR-002**: Only a receipt issued by medulla is admissible. Rows: `ARBORA-MCP-RESOURCES-R002`.
- **FR-003**: The swap guard is called at the one catalog publish step. Rows: `ARBORA-MCP-RESOURCES-R003`, `ARBORA-MCP-RESOURCES-R003.1`, `ARBORA-MCP-RESOURCES-R003.2`.
- **FR-004**: The status docs describe the gate result. Rows: `ARBORA-MCP-RESOURCES-R004`.

## Success Criteria

- **SC-001**: Every non-reconciled case returns `reingestion-unreconciled` with a stable reason. Rows: `ARBORA-MCP-RESOURCES-R001.1`.
- **SC-002**: A refused publish leaves the active generation unchanged. Rows: `ARBORA-MCP-RESOURCES-R003.2`.

## Assumptions

- Medulla issues the receipt through `MEDULLA-REPO-INGEST-001`; Arbora consumes that ID and does not redefine it.
- Arbora introduces no durable store; the receipt is read from medulla and never cached as a second source of truth.

## Scope and interfaces

Arbora reuses the typed, fail-closed receipt pattern of `arbora/fleet/catalog_reader.py` (`FleetCatalogIntegrityError`). That reader covers live `:Server` and component reads, not four-family MCP publication, and this spec does not replace it.
