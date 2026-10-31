# graph-os specifications

These specs follow the ecosystem [spec standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/spec-standard.md): one owning spec per capability, extend an existing spec before creating one, audits and reviews land in the owning spec, one ID and title form, one file set. The `spec-standard` gate enforces it.

This tracked `specs/` directory is the public build contract for **GRAPHOS** owned work. Every
spec must contain the behavior, architecture, interfaces, tests, quality gates, and acceptance
criteria needed to implement it from this repository. External program notes may inform a draft,
but no private document or local workspace path is required to build or verify a public spec.
Delivery status is recorded here with exact merged revision and test evidence.

## Structure

Create `specs/<stable-id>/` with `spec.md` (user outcome, requirements, acceptance), `plan.md`
(architecture, reuse, interfaces, live wiring, decisions), `test-spec.md` (positive, negative,
integration, quality and release proof), `tasks.md` (ordered implementation and verification),
`requirements.md` (the definition of every requirement ID the spec owns), and `status.json`
(machine-readable delivery, acceptance, and public receipts). `status.json`'s `requirements` array
carries one entry per requirement ID defined in `requirements.md`, each with its own
`delivery_state` and evidence; a requirement counts as delivered only with a merged-head commit on
the default branch.
Start from [`_template/`](_template/). Keep status and evidence explicit; a planned or tested item
is not a landed item. Put durable evidence links in the spec directory, never local scratch output.
This follows a specify/plan/tasks flow with an explicit test contract. The tracked [constitution](../.specify/memory/constitution.md) records this repository's governing principles.

Each spec must name its stable ID and sole owner. Cross-repository work links the other owners'
public specs by stable ID and GitHub URL; each repository documents the complete contracts and
acceptance it owns. Designs must inventory existing code and reuse the legal
owner and live wiring before proposing new components. Include CCCC, jscpd, dupehound, KISS,
language-native and repo release gates where applicable, with exact pass evidence and a
no-duplicate-authority check.

## States

States are `SPECIFIED`, `LANDED`, and `VERIFIED`. The generator sets them. See the [spec standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/spec-standard.md#states).

## Specification index

| Directory | Spec ID | Owned requirement IDs |
|---|---|---|
| [`a2a/spec.md`](a2a/spec.md) | `ARBORA-A2A` | `ARBORA-A2A-R001`–`R021` (defined in [requirements.md](a2a/requirements.md)) |
| [`adaptive-capacity/spec.md`](adaptive-capacity/spec.md) | `GRAPHOS-CAPACITY-001` | `ARBORA-CAPACITY-R001`–`R002` |
| [`conversational-acp-gateway/spec.md`](conversational-acp-gateway/spec.md) | `GRAPHOS-ACP-001` | `ARBORA-ACP-R001` |
| [`data-and-market-projections/spec.md`](data-and-market-projections/spec.md) | `GRAPHOS-DATA-MARKET-001` | `ARBORA-DATA-MARKET-R001`–`R006` |
| [`dependency-ordered-release/spec.md`](dependency-ordered-release/spec.md) | `GRAPHOS-RELEASE-001` | `RL-01`–`RL-07` (defined in [requirements.md](dependency-ordered-release/requirements.md) as `ARBORA-RELEASE-R001`–`R003`) |
| [`deployment-policy-as-code/spec.md`](deployment-policy-as-code/spec.md) | `ARBORA-DEPLOY-POLICY` | `ARBORA-DEPLOY-POLICY-R001`–`R006` |
| [`fleet-catalog-and-tools/spec.md`](fleet-catalog-and-tools/spec.md) | `GRAPHOS-FLEET-001` | `ARBORA-FLEET-R001`–`R035`, `ARBORA-FLEET-R045`, `ARBORA-FLEET-R051`, `ARBORA-FLEET-R052` |
| [`host-composition-boundary/spec.md`](host-composition-boundary/spec.md) | `GRAPHOS-HOST-001` | `ARBORA-HOST-R001`–`R019` |
| [`hosted-api-operations/spec.md`](hosted-api-operations/spec.md) | `GRAPHOS-OPS-001` | `HO-01`–`HO-11` (defined in [requirements.md](hosted-api-operations/requirements.md) as `ARBORA-OPS-R001`–`R038`) |
| [`mcp-resource-publication-reconciliation/spec.md`](mcp-resource-publication-reconciliation/spec.md) | `GRAPHOS-MCP-RESOURCES-001` | `ARBORA-MCP-RESOURCES-R001`–`R004` |
| [`identity-access/spec.md`](identity-access/spec.md) | `GRAPHOS-IDENTITY-001` | `IA-01`–`IA-16` (defined in [requirements.md](identity-access/requirements.md) as `ARBORA-IDENTITY-R001`–`R024`) |
| [`messaging-channel-supervision/spec.md`](messaging-channel-supervision/spec.md) | `GRAPHOS-MESSAGING-001` | `MSG-01`–`MSG-06` (defined in [requirements.md](messaging-channel-supervision/requirements.md) as `ARBORA-MESSAGING-R001`–`R006`) |
| [`portable-deployment/spec.md`](portable-deployment/spec.md) | `GRAPHOS-DEPLOY-001` | `ARBORA-DEPLOY-R001`–`R015` |
| [`public-ingress-identity/spec.md`](public-ingress-identity/spec.md) | `GRAPHOS-INGRESS-001` | `IN-01`–`IN-06` (defined in [requirements.md](public-ingress-identity/requirements.md) as `ARBORA-INGRESS-R001`) |

Each directory's `requirements.md` is the authoritative definition of its requirement IDs; each
`status.json` records their current delivery state and evidence.

## Graph OS owner map

| Prefix | Repository specs | Responsibility |
|---|---|---|
| `EG` | [medulla](https://github.com/Knuckles-Team/medulla/tree/main/specs) | Rust database, graph compute, ontology, governed ingestion, durable records and clients |
| `SDK` | [agent-connector-sdk](https://github.com/Knuckles-Team/agent-connector-sdk/tree/main/specs) | connector control, transport, manifests, certification, governed effects |
| `AU` | [agent-utilities](https://github.com/Knuckles-Team/agent-utilities/tree/main/specs) | agent orchestration and control workflows |
| `GRAPHOS` | [graph-os](https://github.com/Knuckles-Team/graph-os/tree/main/specs) | serving composition, fleet, gateway, A2A, deployment operations |
| `WEBUI` | [agent-webui](https://github.com/Knuckles-Team/agent-webui/tree/main/specs) | browser presentation and interaction |
| `RM` | [repository-manager](https://github.com/Knuckles-Team/repository-manager/tree/main/specs) | repository discovery, worktrees, source control and execution |

## Contributions

Read this repository's `AGENTS.md` and any contribution guide. Propose `spec.md` first, resolve
architecture and test details in `plan.md` and `test-spec.md`, then implement `tasks.md` in a
dedicated branch or worktree. Link the PR to spec IDs and update evidence and status only when the
corresponding gates actually pass. Use the [universal-skills spec-generator](https://github.com/Knuckles-Team/universal-skills/tree/main/universal_skills/development/spec-generator),
[spec-verifier](https://github.com/Knuckles-Team/universal-skills/tree/main/universal_skills/development/spec-verifier),
and [task-planner](https://github.com/Knuckles-Team/universal-skills/tree/main/universal_skills/development/task-planner)
and the [graph-os-development](https://github.com/Knuckles-Team/graph-os/blob/main/graph_os/skills/graph-os-development/SKILL.md)
bootstrap skill, together with the [SDD full lifecycle](https://github.com/Knuckles-Team/universal-skills/tree/main/universal_skills/development-workflows/sdd-full-lifecycle) workflow.

- [`ARBORA-A2A`](a2a/spec.md) — A2A projection, streaming, history and tool-subset admission
- [`GRAPHOS-ACCEPTANCE`](acceptance-evidence-freeze/spec.md) — evidence acceptance freeze
- [`ARBORA-CAPACITY`](adaptive-capacity/spec.md) — Adaptive capacity control
- [`ARBORA-ACP`](conversational-acp-gateway/spec.md) — Conversational ACP gateway session projection and policy
- [`ARBORA-DATA-MARKET`](data-and-market-projections/spec.md) — Schema context, source admission, and finance projection
- [`ARBORA-RELEASE`](dependency-ordered-release/spec.md) — Dependency-ordered, digest-pinned GraphOS rollout
- [`ARBORA-FLEET`](fleet-catalog-and-tools/spec.md) — Fleet catalog, intent tools, and policy safe reload
- [`ARBORA-HOST`](host-composition-boundary/spec.md) — GraphOS host composition and ownership boundary
- [`ARBORA-OPS`](hosted-api-operations/spec.md) — Hosted API and intent operations
- [`ARBORA-IDENTITY`](identity-access/spec.md) — ACCESS — identity, authorization, and access operations
- [`ARBORA-MCP-RESOURCES`](mcp-resource-publication-reconciliation/spec.md) — MCP resource/template publication reconciliation
- [`ARBORA-MESSAGING`](messaging-channel-supervision/spec.md) — Messaging channel supervision lifecycle
- [`ARBORA-DEPLOY`](portable-deployment/spec.md) — Portable GraphOS development and deployment
- [`ARBORA-INGRESS`](public-ingress-identity/spec.md) — Portable public ingress and identity cutover
