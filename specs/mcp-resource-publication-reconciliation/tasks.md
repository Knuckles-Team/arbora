# Implementation tasks

Check a task only after its linked code and tests land. A checked source task is not acceptance;
the final release task records served evidence.

- [x] T001 ARBORA-MCP-RESOURCES-R001.1 Define frozen `ReconciliationReceipt` and `PublicationGateResult`, and the pure
  `gate_mcp_resource_publication()` function in `arbora/fleet/mcp_resource_reconciliation.py`.
  Cover `ARBORA-MCP-RESOURCES-R001.1`.
- [x] T002 ARBORA-MCP-RESOURCES-R001.1 Add `tests/fleet/test_mcp_resource_reconciliation.py` covering missing, stale, digest-
  mismatched, generation-mismatched, partial-family, and wrong-tenant receipts as
  `reingestion-unreconciled`, and a fully matching fresh receipt as `reconciled`. Cover
  `ARBORA-MCP-RESOURCES-R001.1`.
- [ ] T003 ARBORA-MCP-RESOURCES-R002 Confirm and link the medulla producing contract (`MEDULLA-REPO-INGEST-001`) in
  `spec.md`/`requirements.md`. Cover `ARBORA-MCP-RESOURCES-R002`. (Link added this PR; Medulla-side
  delivery is tracked in medulla's own spec, not here.)
- [ ] T004 ARBORA-MCP-RESOURCES-R003.1 Add `require_reconciled_for_swap()` and `PublicationRefusedError` with refusal tests.
  Cover `ARBORA-MCP-RESOURCES-R003.1`.
- [ ] T005 ARBORA-MCP-RESOURCES-R003.2 Call the guard from the ARBORA-FLEET-001 atomic generation-publish step once
  `fleet-catalog-and-tools` R022/ARBORA-FLEET-R051 lands. Cover `ARBORA-MCP-RESOURCES-R003.2`.
- [ ] T006 ARBORA-MCP-RESOURCES-R004 Reconcile `docs/status.md` / `docs/fleet.md` wording against the gate's actual
  status/reason vocabulary. Cover `ARBORA-MCP-RESOURCES-R004`.
- [ ] T007 ARBORA-MCP-RESOURCES-R001.1 Land reviewed code and record merge commits per slice in `spec.md`; do not mark
  ACCEPTED on source evidence alone.
