# ARBORA-DEPLOY-POLICY plan

## Architecture

Three declarative surfaces. One convergence engine.

1. **Deployment manifest** — a versioned `deploy.yaml` describes the fleet. It lists engine
   shards (each a `portable-deployment` profile instance), the GraphOS gateway, connector fleet
   items, and the optional web UI. Every image is digest-pinned. This extends the existing
   digest-pinned worker-topology rule (ARBORA-DEPLOY-R001). The manifest is data. `plan` reads
   it plus live state. It returns a typed diff. `apply` drives that diff idempotently.
2. **Policy bundles** — versioned YAML bundles of scoped rules. A compiler lowers each rule to
   the existing RBAC/scope evaluator. The existing access service stays the single enforcement
   point for MCP and REST. This is the repo's non-negotiable. Bundles bind to one scope at a
   time. Scopes are tenant, graph, tenant-graph, and cluster-wide.
3. **Served agent surface** — `llms.txt`, MCP best-practice resources, and the skill export are
   generated artifacts of the deployed version. The existing reconciliation-receipt publication
   gate (ARBORA-MCP-RESOURCES-R001) guards them.

## Reuse (inventory before new components)

| Need | Existing owner |
|---|---|
| Deployment profiles (compose / k8s-helm) and first-boot verification | ARBORA-DEPLOY-R013 |
| Digest-pinned manifests | ARBORA-DEPLOY-R001 |
| Doctor, runbook, canary | ARBORA-DEPLOY-R002/R007 |
| Two-person, time-bound approval | existing elevation model (ARBORA-IDENTITY-R001 family) |
| RBAC/scope evaluator, single access boundary | existing access service; MCP/REST shared-decision invariant |
| Reconciliation receipts for published artifacts | ARBORA-MCP-RESOURCES-R001 |
| Scope registry | shared generated scope registry (ARBORA-IDENTITY-R002) |

No new authorization service. No second policy engine. No new publication path.

## Interfaces

- `graph_os.deployment.deploy_manifest` — the typed manifest model. It carries version,
  components, and pins.
- `plan(manifest)` returns a `DeploymentDiff`. It is read-only. `apply(diff)` returns an
  `ApplyReceipt`. It is idempotent.
- `graph_os.policy.bundle` — the typed policy bundle model. `compile(bundle)` returns scope
  rules for the existing evaluator. `bind(bundle, scope)` is a governed operation.
- Served surface: `GET /llms.txt`. MCP resources sit under the existing FastMCP bridge. The
  skill export command gains a `--digest-stamp` flag.

## Live wiring

`plan` and `apply` sit under the existing deployment doctor entry points. Policy binding
attaches to the existing policy administration surface. That surface is the admin console and
the A2A authority boundary (ARBORA-IDENTITY-R001). The served agent surface generates from the
existing fleet-catalog publication step. The reconciliation gate guards it.

## Decisions

- **The manifest describes components, not infrastructure primitives.** GraphOS already owns the
  profiles. The manifest composes them. One deployment story replaces two.
- **Approval reuse, not a new approval system.** Destructive changes consume the existing
  elevation and approval receipts.
- **Epistemic conditions evaluate through the engine access service.** The policy layer never
  re-derives evidence. It only asks.

## Quality gates

Standard repo gates apply. Pytest, mypy, ruff, vulture, dependency-order, public-specs. The
policy compiler passes the no-duplicate-authority check against the existing RBAC evaluator.
