# ARBORA-DEPLOY-POLICY — Deployment and Policy as Code, with a Served Agent Surface

Owner: arbora (`ARBORA`). Sole owner.

## User outcome

An operator declares a whole deployment as one versioned manifest. The manifest covers engine
shards, the gateway, connectors, and the optional web UI. The operator previews the change with
`plan`. The operator converges it with an idempotent `apply`. Drift inspection is read-only.
Destructive changes carry explicit approval gates. Policy is declared the same way. Bundles are
versioned. Rules are scoped. A compiler lowers them into the existing RBAC/scope evaluator.
Enforcement is server-side on every mutation. It runs through the one access boundary. The
served deployment publishes its agent surface. That surface carries `llms.txt`, MCP
best-practice resources, and a version-matched product skill export. The agent tooling always
describes the deployed version.

This answers a declarative cluster plan/apply posture. It answers a policy-as-code posture.
Both are scoped to a single engine cluster. Its policy language is static-attribute. The
stated advantage is scope and depth. GraphOS governs a whole fleet deployment. Its policy
carries epistemic conditions. Rules gate mutations on live record evidence. Validity
windows, provenance confidence, and evidence counts all qualify. A static-attribute policy
language cannot express those.

## Requirements

Every requirement ID this spec owns lives in [requirements.md](requirements.md). The IDs are
`ARBORA-DEPLOY-POLICY-R001`–`R006`. Architecture and live wiring live in [plan.md](plan.md).
The test contract lives in [test-spec.md](test-spec.md). Ordered work lives in
[tasks.md](tasks.md). Machine-readable delivery state lives in the computed status (`pipelines-hook spec-status`).

## Acceptance

The spec accepts when every row reaches `VERIFIED` in the computed status (`pipelines-hook spec-status`). Each row
carries its bound test evidence.

- A deployment manifest lints. It plans a typed diff. It applies idempotently with a
  digest-bound receipt (R001, R002).
- Drift surfaces read-only. Destructive operations require an approval receipt (R003).
- A policy bundle compiles into the existing evaluator. It enforces on every mutation through
  the one access boundary. Grants never authorize under the proposed policy (R004).
- Epistemic conditions gate mutations on live evidence. Missing evidence fails closed (R005).
- The served agent surface matches the deployed digest. It fails closed on an unreconciled fleet
  state (R006).

States and evidence follow the ecosystem spec standard. A planned or tested item is not a landed
item.
