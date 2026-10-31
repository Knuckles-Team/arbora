# Architecture and implementation plan

## Existing components

`arbora.deployment.genesis_environments` already models ten closed profile sections and reference-only secrets. `config_generator` and `cli` provide configuration commands; `preflight` checks profile-specific host prerequisites; `doctor`, `doctor_coverage` and `doctor_certification` diagnose installed capabilities; `release_canary` checks installed entrypoints and engine binary without starting services; `production_ops` performs guarded operational actions. `deploy/environments/` contains sample profile data. Extend these owners and their tests instead of adding a second installer, configuration parser or doctor.

The package defines `arbora`, `setup-config`, `arbora-doctor`, `arbora-venv`, `arbora-release-canary` and `arbora-production-ops` scripts. Script existence is not functional proof; clean wheel installation and representative invocations are required.

## Configuration contract

The parser accepts one explicit profile with sections `environment`, `target`, `release`, `runtime`, `filesystem`, `configuration`, `secrets`, `network`, `identity`, `validation`. Each section has a closed schema; unknown and missing fields are errors. `secrets` contains only resolvable references such as environment, vault or Kubernetes secret references. Runtime path bindings are anchored to declared writable mounts. The resolved profile yields a deterministic digest, used in rendered resource annotations and validation receipts. A difference between rendered and applied digest is a refusal.

The environment adapter resolves the profile and validates host capabilities, then renders a reviewable plan with create/update/delete inventory. It must not read a secret value during a dry run. Apply requires explicit target confirmation and a validated identity mode. Post-apply probes use the same public Arbora API and observe readiness, authorization, engine connection and fleet catalog. They do not reconstruct success from the existence of a file.

## Profiles and dependency boundaries

| Profile | Required inputs | Guarantees |
|---|---|---|
| Local development | Python, `uv`, published graph engine and agent packages. Loopback binding | Unit/contract checks run without network services. Optional disposable served checks isolate state under a temporary directory. |
| Single-host container | Container runtime, pinned images, explicit writable volumes and secret references | One Arbora host, one graph authority, guarded identity and repeatable startup/recovery. |
| Kubernetes | Namespace/RBAC target, pinned image digest, secret-provider references, mounts and probes | Explicit service account, rollout/rollback, readiness and post-deploy functional receipts. |

The renderer can vary by target, but profile validation, config resolution, engine contract verification and functional check definitions are shared. Keep engine storage and graph semantics in `medulla`; Arbora owns only deployment composition and diagnostics. The WebUI is an optional co-service, not a required core import. Public sample profiles use placeholder domains and secret references only.

## Delivery order

1. Freeze the public profile schema and add a sample development profile whose requirements a contributor can meet from a fresh checkout.
2. Complete the Arbora-owned deployment port migration and command parity; delete agent-runtime duplicate entrypoints after an installed-wheel comparison.
3. Make Compose and Kubernetes renderers consume the same validated profile object; require digest-pinned images and sealed secret references.
4. Add disposable first-boot fixtures and CI service orchestration. Split required portable PR checks from optional production integration acceptance.
5. Document the exact commands on GitHub Pages and link this contract from the contribution guide; publish a verified package and run the external acceptance tier.

## ARBORA-DEPLOY-R016 plan

There is no separate daemon container: its `arbora-daemon` entry point was never served (it raised a not-served error), so the sidecar could only crash-loop, and `ARBORA-HOST-R004` retires the script. The serving container holds the host role whenever the engine is in the pod. In sidecar placement it reaches the engine over the shared Unix socket volume; a non-empty endpoint setting makes the engine resolver connect-only, so no second engine starts.

## ARBORA-DEPLOY-R017 plan: engine-routed deployment placement and assembly

The meristem planner (deleted by commit `db29b2da6`, MERISTEM-BOUNDARY-R002.2) is not re-created. Arbora holds no plan type, no backend-selection logic and no renderer.

### Why this is not a separate planner

The engine decision-engine spec makes `graph.assemble()` the sole authority for composing an agent graph from typed tasks and capabilities (`MEDULLA-DECISION-ENGINE-R036`), routes tasks through the decision ladder (`MEDULLA-DECISION-ENGINE-R044`), and exposes topology and placement selection as one typed Decide question (`MEDULLA-DECISION-ENGINE-R046`, `R111`). A arbora planner would be a second path to the same decisions. Arbora only maps a request to the engine, presents the answer and gets human confirmation.

### Design

1. **Placement.** One typed Decide request/response mapping (R017.1). Engine prerequisite: a deployment-placement question kind under `MEDULLA-DECISION-ENGINE-R046`/`R111` (ID to be assigned by the engine spec; not invented here).
2. **Sequencing.** Ordered fleet-tool steps are an `AgentAssemble` graph (R017.2). Present it read-only (R017.3), confirm through `arbora.plan/confirm` (R017.4, rows `ARBORA-A2A-R005.1`/`R006.1`, with `decide.commit`). Typed refusal while unavailable (R017.5). Engine prerequisite: the `AgentAssemble` handler in `arbora/a2a/routing.py` and `admission.py` is a refusal stub and must be replaced. The confirm surface is also a fail-closed stub.
3. **Artifacts.** Existing files only (R017.6): `deploy/compose/compose.yaml`, `deploy/helm/arbora`, `deploy/swarm/stack.yml`, selected by `deploy/environments/{dev,test,prod}.yaml`. No systemd artifact exists in `deploy/`; systemd is explicitly unsupported (decision D5 below).
4. **In-process.** Unchanged `start_composed_services`, composition from `arbora.mcp_server.composition` (R017.7).
5. **Command.** `setup-config deploy` is a thin subcommand that asks the engine and prints the decision and graph (R017.8).

### Parity table

| Old entry point | Now | Status |
|---|---|---|
| `DeploymentPlan`, `DeploymentStep`, `FleetToolCall`, `rendered_summary` | engine assembly (`AgentAssemble`, `MEDULLA-DECISION-ENGINE-R036`) | dropped as parallel |
| `DeploymentBackend`, `get_backend`, `PlanOnlyBackendError` | engine decision (placement Decide, `R046`/`R111`) | dropped as parallel |
| `detect_composition` / `CompositionPlan` | `arbora.mcp_server.composition` | exists |
| `InProcessBackend` | `start_composed_services` | exists (R017.7 verifies) |
| `render_compose`, `ContainerBackend` | `deploy/compose` | existing artifact |
| `render_k8s_manifest`, `KubernetesBackend` | `deploy/helm/arbora` | existing artifact |
| Swarm (never covered by old planner) | `deploy/swarm/stack.yml` | existing artifact |
| `render_systemd_unit`, `NativeShellBackend` | none | not supported (no artifact in `deploy/`) |
| `deploy-plan` CLI | `setup-config deploy` asking the engine | to build (R017.8) |
| Floating `latest` image default | none | dropped (digest-pinned only) |

### Out of scope

Executing any plan-only fleet call; container-manager, tunnel-manager and systems-manager tool contracts (owned by those repositories); replacing `deploy/helm` or the profile renderers required by ARBORA-DEPLOY-R003/R013; Swarm planning (the old planner never covered it).

## R017 caveat resolutions (design, 2026-10-10)

### Problem and evidence

`scripts/check_deployment_image_digests.py` walks Kubernetes workload kinds (`_POD_PATHS`) in rendered YAML and accepts only `repository@sha256:<64 hex>`; its docstring states that tags and unrendered templates are unsupported. A Compose or Swarm file has no `kind`, so `_check_resource` raises "kind: expected a nonempty string" on it. R017.6 and `tests/deployment/test_deploy_artifacts.py` therefore cannot run the one checker over all three artifacts today. The only tests of the script are `tests/deployment/test_image_digests.py` (Kubernetes fixtures); `tests/deployment/test_arbora_deploy_artifacts.py` checks only that the Compose and Swarm `image` starts with `${ARBORA_IMAGE:?` (lines 83 and 126).

### Architecture

```
artifact path --> checker.check_file
                    |-- document has `kind`            --> existing Kubernetes walker (unchanged)
                    |-- document has `services` map    --> new compose/swarm walker
                    |        image literal --> same _valid_image
                    |        image ${VAR:?msg} --> template mode: accepted, counted as "pin-required"
                    |                              resolve mode (--set VAR=ref): ref checked by _valid_image
                    |-- helm: `helm template -f <pinned-values>` output --> Kubernetes walker
```

One script, one `_valid_image`, one exit contract (0 pass, 1 error, at least one image checked). The only additions are a second document walker and the interpolation rule. No second checker file is created.

### D1 Digest pinning by target (CONFIRMED by reading the artifacts)

| Target | Image references in the artifact | Pinned in the file today |
|---|---|---|
| `deploy/compose/compose.yaml` | One service, `arbora`: `image: ${ARBORA_IMAGE:?set ARBORA_IMAGE ...@sha256:<digest>}` (line 21). The engine runs inside the same container; no other service. | No literal digest. Compose refuses to start when the variable is unset; nothing checks that its value carries a digest. |
| `deploy/swarm/stack.yml` | One service, `arbora`: the same `${ARBORA_IMAGE:?...}` form (line 31). | Same: fail-closed on unset, digest not checked. |
| `deploy/helm/arbora` | `arbora.image` serves the `arbora` container (`templates/arbora.yaml`, via `arbora.image`); the engine container uses `arbora.engineImage` (lines 34 and `engine.yaml` 26), which falls back to `arbora.image` when `engine.image.repository` is empty; every `connectors[]` and `components[]` entry renders its own `image` (`extensions.yaml` line 37). | No. `values.yaml` defaults are `tag: replace-me`, `digest: ""`; `_helpers.tpl` renders `repository@digest` only when `digest` is set, otherwise `repository:tag`. Default rendering is therefore tag-based and would fail the checker (the intended fail-safe). |

Environment profiles: `prod.yaml` and `test.yaml` declare `tag_policy: digest-pinned`; `dev.yaml` declares `floating-tag` (non-prod only). The profile policy and the artifact check are separate; this design makes the checker enforce it per target.

### D1 Decision (DECIDED)

Extend the existing script rather than add a checker. Compose/Swarm `${NAME:?...}` with no default is accepted in template mode because the runtime itself refuses an unset value; `${NAME:-default}` and literal images must be digest-pinned; `--set NAME=value` resolves the variable and the value must pass `_valid_image`. For Helm, CI renders the chart with a pinned values fixture and pipes the output to the same script; a second CI step renders defaults and expects exit 1. Alternative rejected: a separate compose linter (a second parser and a second pin grammar to keep in sync).

### D2 to D4 and D5

Dashboard parity, artifact authentication and the meristem export are resolved in `specs/hosted-api-operations/plan.md` (ARBORA-OPS-R039 to R041).

### D5 systemd target (DECIDED: keep unsupported)

Evidence: all three profiles under `deploy/environments/` set `target.orchestrator: kubernetes` (`dev.yaml` line 25, `test.yaml` 26, `prod.yaml` 32), `genesis_environments.py` validates the orchestrator against `genesis.yaml` `run_plan.orchestrators`, and the production profile is Helm. The only systemd material in the repository is guidance: `arbora/skills/arbora-genesis/references/bare-metal.md` and a Quadlet note in `compose-and-podman.md`; neither is a rendered artifact under `deploy/`. Decision: keep systemd unsupported as a placement artifact. A new artifact would need its own renderer or hand-kept unit file, a drift gate and a first-boot test with no profile that uses it, which is the parallel functionality the lane rules forbid. The reference stays operator guidance. Reopen condition: an `environments/*.yaml` profile with a non-kubernetes bare-metal orchestrator appears. Alternative: add `deploy/systemd/arbora.service` with a unit-lint test; rejected for lack of a consumer.

R017.6 therefore refuses `systemd` with a typed `unsupported_target` carrying the pointer to the bare-metal reference, and R017.15 tests that.

## ARBORA-DEPLOY-R018 small deployment shares one engine

Data flow: server start → `detect_composition` probe → `start_composed_services` starts or attaches → shared engine.
Attach point: arbora/mcp_server/composition.py:43 (`detect_composition`) and :230 (`start_composed_services`).
Reused: the existing composition detection and the in-process path of `ARBORA-DEPLOY-R017.7`. The single-binary engine build is an engine row.
Decision: probe the endpoint, then start. Rejected alternative: a lock file per host, which goes stale after a crash.

## Confirmed component choices (ARBORA-DEPLOY-R013/R018)

Existing code: `deployment/cli.py:134` owns configuration commands; `genesis_environments.py` owns typed profile parsing.
`config_generator.py:336` generates configuration; `:420` exposes its reference; `:1035` provides diagnostic checks.
`mcp_server/composition.py:43` detects optional co-services; `:230` starts them.
Its current `CompositionPlan` has messaging and web flags, not a complete deploy/reuse component inventory.
Reachable today: profile parsing and existing composition are callable; full choice enforcement and target qualification are missing.

Extend the existing R017 `setup-config deploy` design, not a second planner, renderer, backend registry or authority store.
`ComponentDeploymentChoice` version 1 has `component_id`, `action`, `endpoint_ref`, `profile`, `tunings` and `confirmation_ref`.
Actions are exactly `deploy` and `reuse`. Omit optional unselected components from the confirmed inventory.
Required components cannot be omitted. Reuse requires an endpoint and a compatibility probe; deploy requires an admitted target.
Sort choices by component ID and reject duplicates. Bind their canonical digest to the existing confirmed engine request.
Operator choices constrain engine placement and assembly; they never independently authorize an effect.
Keep R017's missing deployment-question, assembly and confirmation producers as explicit prerequisites.
Use the existing SDK governed effect contract for source changes; Arbora checks the approved bindings and returned receipts.

Reuse permits read-only connectivity and compatibility probes plus local reference configuration.
It forbids remote configuration, installation, replacement, restart, deletion, credential rotation and deployment effects.
Reject an entire plan containing any such reused-component effect before applying its first step.
An unavailable reused endpoint refuses; never reinterpret unavailability as permission to deploy.
Do not stop reused resources on rollback or shutdown. Reconcile uncertain effects from actual producer receipts.

### Target and privilege matrix

| Cell ID | Target | Runtime | Privilege | Existing basis and prerequisite |
|---|---|---|---|---|
| `native-user` | `native_package` | `installed_python` | `unprivileged_user` | Existing bare-metal guidance; public package or explicit editable installation and first-boot proof. |
| `container-managed-rootful` | `standalone_container` | `daemon_managed` | `rootful` | Existing image; require admitted runtime and served startup proof. |
| `container-managed-rootless` | `standalone_container` | `daemon_managed` | `rootless` | Existing image; require admitted runtime and served startup proof. |
| `container-daemonless-rootful` | `standalone_container` | `daemonless` | `rootful` | Existing image; require admitted runtime and served startup proof. |
| `container-daemonless-rootless` | `standalone_container` | `daemonless` | `rootless` | Existing image; require admitted runtime and served startup proof. |
| `compose-managed-rootful` | `compose` | `daemon_managed` | `rootful` | Reuse `deploy/compose/compose.yaml`; qualify the declared runtime and privilege. |
| `compose-managed-rootless` | `compose` | `daemon_managed` | `rootless` | Reuse `deploy/compose/compose.yaml`; qualify the declared runtime and privilege. |
| `compose-daemonless-rootful` | `compose` | `daemonless` | `rootful` | Reuse `deploy/compose/compose.yaml`; qualify the declared runtime and privilege. |
| `compose-daemonless-rootless` | `compose` | `daemonless` | `rootless` | Reuse `deploy/compose/compose.yaml`; qualify the declared runtime and privilege. |
| `swarm-managed-rootful` | `swarm` | `daemon_managed_cluster` | `rootful` | Reuse `deploy/swarm/stack.yml`; require scoped cluster authority and admitted deployment. |
| `kubernetes-namespace` | `kubernetes` | `kubernetes_api` | `namespace_service_account` | Reuse `deploy/helm/arbora`; preserve identity, secrets, mounts and first-boot acceptance. |

Each profile binds the exact `(target, runtime, privilege)` triple and its cell ID; no field may be implicit.
For local containers, privilege describes the runtime's rootful or rootless mode, not merely the workload user ID.
The cluster API cell uses a namespace-bound service account; it does not claim local daemon privilege.
Test both published-package and explicitly selected editable installation within the native cell.
These cells define required coverage, not existing implementations.
Derive acceptance cases from the matrix's unique triples; never encode a separate expected case count.
Compare covered triples with declared triples and fail on omitted, duplicate or unrecognized cells.
Also derive the standalone/compose cross-product of both runtime families and both privilege modes.
Require that complete cross-product in the declared matrix and coverage, so an omitted cell retains its obligation.
Every declared cell needs a real served positive case and a no-effect negative case before claiming support.
An unavailable adapter retains its cell and missing positive evidence; a refusal alone does not qualify it.
Validate runtime capability, privilege, writable mounts, network binding and identity before applying a profile.
Missing adapters return `unsupported_target` without a substitute runtime. Rootless failures never trigger privilege escalation.
The native package target does not add a systemd artifact; R017's existing unsupported-systemd decision remains unchanged.
Use production package releases or digest-pinned images. Development editable installations require explicit development selection.
R013.1 retains all original identity modes, administrator, policy, secrets, sidecar and SDK-federation requirements.
The original provisioned development and production profiles retain their sidecar topology.
Reuse configurations are separate declared variants; their evidence cannot satisfy the original sidecar first-boot acceptance.
R018.1 through R018.6 remain unchanged; R018.7 and R018.8 add stricter confirmed reuse behavior.

## Synchronized deployment guidance (ARBORA-DEPLOY-R010)

Existing code: packaged deployment and development skills, README and documentation already exist.
Reachable today: documentation exists; complete example extraction and served command parity are missing.
Each `DeploymentExample` binds profile ID, configuration digest, prerequisite IDs, argument vector and expected readiness probe.
Include installation, both MCP transports, identity, secret references, component choices, logs and shutdown for each supported profile.
Extract examples from the skill, README and documentation and validate against the same parser and environment manifest.
Reject stale flags and omitted consumed names; examples use placeholders, never live secrets or operator endpoints.
Keep unavailable adapters explicit and refuse their example operation. Do not imply successful deployment from rendered files.
R010.1 preserves the original development-skill topics and AGENTS navigation acceptance.

## Additional owner requirements

- `ARBORA-DEPLOY-R009.2`: Plan the owner work and dependencies defined in requirements.md.
- `ARBORA-DEPLOY-R009.3`: Plan the owner work and dependencies defined in requirements.md.
- `ARBORA-DEPLOY-R013.7`: Plan the owner work and dependencies defined in requirements.md.
- `ARBORA-DEPLOY-R010.4`: Plan the owner work and dependencies defined in requirements.md.

### 2026-10-17 — R004 frozen-environment fixture contracts

Repair the locked-environment type gate with concrete messaging, assembler, and catalog test fixtures that implement their existing public contracts. Narrow fixture-specific state before inspection and annotate the validated RLM request. Preserve production behavior and protocol requirements; add no suppressions or dependency changes.

### 2026-10-18 release version registration (ARBORA-DEPLOY-R002)

The version bump updates the package source and changelog through the configured bump tool. README uses a dynamic PyPI badge and has no static version string. Delete its stale bump registration. Use TOML so the changelog heading remains replacement text rather than an INI comment. Generate the matching lock and validate the candidate before creating a new release tag. Never replace an existing tag.

### 2026-10-18 atomic-create patch release (ARBORA-DEPLOY-R002)

Publish the reviewed unique-record correction as a new patch version using the configured bump tool, preserving existing releases and dependency pins. Require exact-candidate source gates and installed-wheel qualification, including the real headless TUI test. Keep the older-store producer refusal and fleet onboarding acceptance separate from this correction.

## Medulla companion rows (2026-10-28)

- `ARBORA-DEPLOY-R017.17`: Plan the Arbora-owned part and its link to the Medulla row in requirements.md.
