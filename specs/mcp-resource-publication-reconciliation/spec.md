# ARBORA-MCP-RESOURCES — MCP resource/template publication reconciliation

**Spec ID:** ARBORA-MCP-RESOURCES

**Owner:** arbora

**State:** READY FOR IMPLEMENTATION — architecture and acceptance specified; no claim that the full surface is deployed or accepted.
**Scope IDs:** ARBORA-MCP-RESOURCES-R001, ARBORA-MCP-RESOURCES-R001.1, ARBORA-MCP-RESOURCES-R002, ARBORA-MCP-RESOURCES-R003, ARBORA-MCP-RESOURCES-R003.1, ARBORA-MCP-RESOURCES-R003.2, ARBORA-MCP-RESOURCES-R004.

## Outcome and state legend

arbora is the public MCP door for the durable four-family resource/template set (tools, prompts,
resources, resource templates); medulla owns the durable graph those families are
projected from. arbora never claims to have published a candidate generation of that set unless
medulla has handed back a receipt reconciling the exact candidate against the durable
record. Lacking that receipt, arbora keeps returning the typed `reingestion-unreconciled` result
documented in [`docs/status.md`](../../docs/status.md) and [`docs/fleet.md`](../../docs/fleet.md)
instead of guessing or standing up a second, process-local durability store.

| State | Meaning | Required evidence |
|---|---|---|
| PROPOSED | Contract drafted, awaiting review | None |
| READY FOR IMPLEMENTATION | Reviewed design, no code claim | None |
| BUILDING | Partial source present | Branch/PR evidence per requirement |
| LANDED | Merged to default branch | Merged-head commit URL |
| ACCEPTED | Landed and release-verified | Checked-in tests + release receipt |

This is a thin contract spec: it defines the reconciliation receipt arbora needs from
medulla before it publishes, and the typed refusal it returns until that receipt exists.
It does not define a new store or a new durability mechanism.
It does not replace the existing fleet catalog reload design.
That design lives in [`fleet-catalog-and-tools/spec.md`](../fleet-catalog-and-tools/spec.md).
It owns ARBORA-FLEET-R022 and ARBORA-FLEET-R051.
This spec narrows it for the MCP resource/template publication step only.

## Users and acceptance stories

1. **MCP client (P1):** calling a resource or resource-template listing method either sees the
   durable four-family set arbora has reconciled proof for, or a typed `reingestion-unreconciled`
   refusal — never a guess at what the durable graph currently holds.
2. **Operator (P1):** `docs/status.md`'s "Explicitly unavailable surfaces" row for this capability
   stays true because the same typed result that row names is what the gate actually returns, not
   separately maintained prose.
3. **medulla contributor (P2):** the exact shape of the reconciliation receipt arbora
   requires is public and stable, so MEDULLA-REPO-INGEST-001 work that produces it has a fixed target.

## Functional requirements

| ID | Requirement and acceptance rule |
|---|---|
| `ARBORA-MCP-RESOURCES-R001` | **Publication gate refuses without a valid receipt (rollup).** arbora's MCP resource/template publication path never claims a candidate generation is published unless a `ReconciliationReceipt` exists, matches that exact candidate, and is fresh; otherwise it returns the typed `reingestion-unreconciled` result. Delivered through its child requirement, producer first. |
| `ARBORA-MCP-RESOURCES-R001.1` | **Typed `ReconciliationReceipt` model and publication gate (this PR).** `arbora/fleet/mcp_resource_reconciliation.py` defines a frozen `ReconciliationReceipt` (tenant, candidate `catalog_generation`, `snapshot_digest`, acknowledged family set, issue time) and `gate_mcp_resource_publication()`, a pure function returning a typed `PublicationGateResult` whose `status` is `"reconciled"` only for a receipt matching the exact tenant, generation, digest, and all four families within a freshness bound, and `"reingestion-unreconciled"` (with a stable `reason` of `missing`, `stale`, or `invalid`) for every other case. No caller is wired to it yet (`ARBORA-MCP-RESOURCES-R003`, not yet specified in this slice). |
| `ARBORA-MCP-RESOURCES-R002` | **medulla is the sole admissible receipt source.** The only receipt the gate may treat as reconciling is one medulla issues once the durable graph has actually committed the candidate's four-family projection; arbora does not synthesize, cache past expiry, or otherwise locally fabricate a passing receipt. The producing contract is owned by medulla as [`MEDULLA-REPO-INGEST-001`](https://github.com/Knuckles-Team/medulla/blob/main/specs/repository-index-and-ingestion/spec.md) (`specs/repository-index-and-ingestion` in that repository); this spec consumes that ID and does not redefine it. |
| `ARBORA-MCP-RESOURCES-R003` | **Gate wired at the one catalog-publish entry point (rollup).** The fleet catalog's atomic generation-publish step (`fleet-catalog-and-tools` R022/ARBORA-FLEET-R051) calls `gate_mcp_resource_publication()` before swapping the active generation pointer for the four MCP families. Only an all-families `"reconciled"` result allows the swap. Split because arbora has not built the publish step yet. No entry point exists to wire (`fleet-catalog-and-tools` R022/ARBORA-FLEET-R051 is still BUILDING). Delivered through its children. |
| `ARBORA-MCP-RESOURCES-R003.1` | **Swap guard function.** `arbora/fleet/mcp_resource_reconciliation.py` adds `require_reconciled_for_swap()`, which wraps `gate_mcp_resource_publication()` and raises a typed `PublicationRefusedError` carrying the `PublicationGateResult` for any `reingestion-unreconciled` result, and returns the receipt only for `reconciled`. The publish step can then call one function that cannot be bypassed by ignoring a return value. Not built in this slice. |
| `ARBORA-MCP-RESOURCES-R003.2` | **Call the guard from the publish step (depends on `fleet-catalog-and-tools` R022/ARBORA-FLEET-R051 landing in arbora).** The atomic generation-publish step calls `require_reconciled_for_swap()` before swapping the active generation pointer for the four families, leaves the last-known-good generation active on refusal, and surfaces the typed result in the reload outcome. Not built in this slice. |
| `ARBORA-MCP-RESOURCES-R004` | **Status docs are the gate's result, not hand-maintained prose.** `docs/status.md`'s "Explicitly unavailable surfaces" row and `docs/fleet.md`'s reconciliation paragraph describe exactly the `reingestion-unreconciled` result this gate returns. A change to the gate's status/reason vocabulary requires updating both in the same change. Not built in this slice. |

## Reuse and non-goals

Reuse `arbora/fleet/catalog_reader.py`'s existing `FleetCatalogIntegrityError` / receipt-mismatch
pattern as prior art for a typed, fail-closed receipt check; this spec does not replace that reader
or its `ReadReceipt`, which cover live `:Server`/component reads, not four-family MCP publication.
This spec introduces no new durable store: the receipt is read from medulla, never written
or cached as a second source of truth inside arbora.
