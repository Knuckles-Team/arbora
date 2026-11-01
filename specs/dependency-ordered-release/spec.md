# ARBORA-RELEASE — Dependency-ordered, digest-pinned Arbora rollout

**Owner:** arbora. **Requirement ID:** ARBORA-RELEASE-R001 (Arbora deployment-consumer partition).

## Purpose and user stories

An operator can promote a qualified Arbora release only after its engine, agent runtime, optional WebUI, and enabled connector dependencies have compatible immutable artifacts. A contributor can validate the ordering, digest and rollback contract with local fixtures; no cloud account or private cluster is required for ordinary PR checks.

P0: A release operator supplies a candidate manifest with exact source revisions, image digests, package versions, dependency edges and profile digest. The Arbora deployment consumer verifies it before apply. P0: The operator observes each dependency's readiness and functional contract before advancing, and receives a bounded receipt or a rollback decision. P1: A failed later stage never silently marks the whole release accepted.

## User Scenarios & Testing

### User Story 1 - Promote a qualified release in dependency order (Priority: P1)

A release operator supplies a candidate manifest of exact revisions and image digests, and Arbora promotes each component only after its predecessors are ready.

**Acceptance Scenarios**:

1. **Given** a manifest with a cycle, an unknown component or a mutable image tag, **When** the operator plans the release, **Then** the plan refuses and names the defect.
2. **Given** a valid manifest, **When** the operator plans the release, **Then** the plan lists one deterministic order with every digest.

### User Story 2 - Stop on a failed stage (Priority: P1)

An operator observes each stage and receives a bounded receipt or a rollback decision.

**Acceptance Scenarios**:

1. **Given** a stale digest at one stage, **When** the stage is compared, **Then** dependents do not start and the failed edge is recorded.
2. **Given** a failed later stage, **When** the run ends, **Then** the release is never marked accepted.

### User Story 3 - Run served task plans with typed tool steps (Priority: P2)

A caller runs a dependency plan whose steps call connector tools with bound arguments.

**Acceptance Scenarios**:

1. **Given** an engine that serves `TaskPlan`, **When** the caller invokes `agents.plan_run`, **Then** the harness runs each step and returns a record per step.
2. **Given** an engine without `TaskPlan`, **When** the caller invokes `agents.plan_run`, **Then** the call refuses with `UNAVAILABLE`.

### Edge Cases

- A disabled optional component has no dependency edge.
- A missing signature or trust root rejects the manifest before any apply.
- A rollback that cannot restore the prior pair stays visibly pending and is never reported as success.

## Requirements

- **FR-001**: Arbora validates digest-pinned candidates and promotes them in dependency order. Rows: `ARBORA-RELEASE-R001`.
- **FR-002**: Every push is gated on green local gates, and static architecture tests run on every pull request. Rows: `ARBORA-RELEASE-R002`.
- **FR-003**: Served task plans refuse with `UNAVAILABLE` against an engine without `TaskPlan`. Rows: `ARBORA-RELEASE-R003.4.3`.

## Success Criteria

- **SC-001**: A candidate with a cycle, unknown component or mutable image is refused. Rows: `ARBORA-RELEASE-R001`.
- **SC-002**: The pull-request workflow runs the static architecture tests when the mapped scope is empty. Rows: `ARBORA-RELEASE-R002`.

## Assumptions

- Each owner publishes the evidence for its own component.
- Pull-request checks use published package fixtures and a disposable local engine.

## Functional requirements and acceptance

| ID | Requirement | Observable success |
|---|---|---|
| RL-01 | Accept a versioned, signed or otherwise trusted candidate manifest of immutable artifacts and dependency edges. Every deployed image uses a `sha256` digest, never a floating tag. | A dry run reports the exact graph and digests; missing signature/trust, cycle, unknown component or mutable image is rejected. |
| RL-02 | Resolve a topological rollout order: graph engine authority and required storage first. Agent runtime/connector services next according to declared contracts. Arbora gateway after its required backends. Optional WebUI after its Arbora API. A disabled optional component has no edge. | A deterministic fixture yields one documented order and refuses a missing required predecessor. |
| RL-03 | At each stage, compare installed artifact digest, profile digest and compatibility matrix to the candidate. Wait for readiness plus a functional probe before releasing dependents. | A stale digest or incompatible API blocks the dependent rollout and records which edge failed. |
| RL-04 | Arbora itself validates installed entrypoints, engine binary/client compatibility, identity/ingress posture, catalog startup and authenticated MCP/REST behavior. A source-only canary is not a served proof. | `arbora-release-canary` and a served probe produce distinct receipts tied to the exact candidate revision. |
| RL-05 | On failure, stop promotion and restore the previous immutable Arbora profile/image pair where safe. Report any irreversible downstream effect and require operator decision. Never invent rollback success. | Fault injection shows no further stage starts, previous digest is restored or recovery remains visibly pending. |
| RL-06 | Required PR checks use published package fixtures and a disposable local engine/identity fixture. Hosted image build, registry publication and cluster/browser qualification are release gates with explicit evidence. | Remote contribution can run contract tests without private registry, cluster, secrets or live domain. |
| RL-07 | Store a privacy-safe release receipt: manifest digest, artifact digests, stage order, probe results, UTC timestamps, and exact commit/check URLs. Never store credentials, tokens or private endpoint details. | A reviewer can verify each stage from public evidence without access to local operator records. |

## Ownership and cross-repository contract

The [pipeline repository](https://github.com/Knuckles-Team/pipelines) owns build and publication of immutable images plus CI provenance. The [repository manager](https://github.com/Knuckles-Team/repository-manager/tree/main/specs) owns dependency-readiness metadata and release index policy. [Epistemic Graph](https://github.com/Knuckles-Team/medulla/tree/main/specs) owns engine API/storage compatibility and its own readiness. Arbora owns candidate consumption, deployment profile validation, local canary, served checks and Arbora rollback. [Portable deployment](../portable-deployment/spec.md) defines the common profile; [public ingress](../public-ingress-identity/spec.md) defines the served public identity check.

The shared contract is a candidate manifest with component ID, repository, exact source commit, artifact digest, API/schema compatibility range, profile digest, required predecessor IDs, and public check receipts. Each owner must publish the evidence for its own component. Arbora must refuse incomplete or untrusted inputs; it cannot promote another repository by assuming its source branch is green.

Out of scope: a particular CI vendor, image builder, registry, orchestrator, cloud, or private release index. Those are adapters behind the manifest contract.

## Success criteria and traceability

| Requirement | Requirement ID | Design | Tests | Acceptance evidence |
|---|---|---|---|---|
| RL-01 | ARBORA-RELEASE-R001 | [Manifest](plan.md#candidate-manifest-and-compatibility) | T-RL-01, T-RL-02 | signed manifest/digest |
| RL-02 | ARBORA-RELEASE-R001 | [Ordering](plan.md#rollout-state-machine) | T-RL-03, T-RL-04 | ordered stage receipt |
| RL-03 | ARBORA-RELEASE-R001 | [Ordering](plan.md#rollout-state-machine) | T-RL-05, T-RL-06 | live predecessor probes |
| RL-04 | ARBORA-RELEASE-R001 | [Existing path](plan.md#existing-system-and-reuse) | T-RL-07 | canary and served receipt |
| RL-05 | ARBORA-RELEASE-R001 | [Failure](plan.md#failure-and-rollback) | T-RL-08 | rollback or pending incident |
| RL-06 | ARBORA-RELEASE-R001 | [Gates](plan.md#quality-and-release-gates) | T-RL-09 | public CI and release checks |
| RL-07 | ARBORA-RELEASE-R001 | [Evidence](plan.md#candidate-manifest-and-compatibility) | T-RL-10 | redacted receipt |

Requirement IDs are defined in [requirements.md](requirements.md); delivery state per ID is in `status.json`.

## Child interaction boundary

`ARBORA-RELEASE-R003.4.12` and `.13` specify separate active-child messaging and
completed-child continuation after the shared read-only explorer. They do not
extend the delivery claim of the flat plan-run row R003.4.1. Missing native
identity, history or receipt contracts keep interaction unavailable.
