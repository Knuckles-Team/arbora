# Delivery tasks and evidence

- Done: Implement typed deployment profile, preflight, doctor, release-canary and production-ops modules. Evidence: `arbora/deployment/` and `tests/deployment/`. **Source implemented; independent install and first-boot acceptance pending.**
- Open: Add and verify the fresh-checkout development path, including a disposable engine and clear optional component selection.
- Open: Complete migration of deployment modules and scripts from agent-runtime internals onto supported public ports.
- Open: Render and validate both Compose and Kubernetes profiles from one closed schema; reject plaintext secrets and floating production images.
- Open: Add positive and negative first-boot/rollback tests using provisioned CI resources and reference-only credentials.
- Open: Audit hook installation and required CI gates; make all ordinary PR gates reproducible in a cloud checkout.
- Open: Publish usage to the Arbora GitHub Pages site and link the contribution guide to this spec and a verified setup command.
- Open: Record exact commit, package hashes, CI runs, profile digests and redacted functional receipts. Then promote individual deliverables to Verified/Released/Accepted.

**Current evidence gap:** The existing source and test names establish implementation progress. They do not establish a public clean-install, profile matrix or first-boot acceptance receipt for this complete contract.

## R013 landed evidence and remaining acceptance

Evidence reconciled against [main `937d38569cef9f07abf46fd064aff98c24a33c85`](https://github.com/Knuckles-Team/arbora/commit/937d38569cef9f07abf46fd064aff98c24a33c85).
Each merge below is an ancestor of that revision. These receipts establish
bounded source and CI progress. **ARBORA-DEPLOY-R013 remains BUILDING; the
spec remains NOT_AUDITED.** The other requirements retain their existing states;
the R005 WebUI rename dependency remains separate and open.

- Done: Land Kubernetes preflight client checks:
  [PR #23](https://github.com/Knuckles-Team/arbora/pull/23),
  merge `2abb9ebb3905e2daedc3e4c5e83dc5682c13f2bd`,
  [CI 37174546666](https://github.com/Knuckles-Team/arbora/actions/runs/37174546666)
  at `7694c6eb891a23186805bc8cb62c531fc642ee5f`.
  The [preflight tests](../../tests/deployment/test_preflight.py) cover
  `dev`, `test`, `prod`, `enterprise`, validated named targets, missing
  kubectl/Helm, failed/timed-out probes and invalid profiles. Successful client
  probes leave cluster access and first boot unverified.
- Done: Land the [Kubernetes observation verifier](../../arbora/deployment/kubernetes_first_boot.py)
  and [offline tests](../../tests/deployment/test_kubernetes_first_boot.py):
  [PR #24](https://github.com/Knuckles-Team/arbora/pull/24),
  merge `0da5f7440603cd3112bf25dc091d84a9a902fb1d`,
  [CI 37175887401](https://github.com/Knuckles-Team/arbora/actions/runs/37175887401)
  at `80a4c66e071862f1335f2e3487090005f728d62e`.
  It checks ownership, rendered/observed configuration, image digests, pod
  readiness and PVCs with bounded read-only collection. Missing or mismatched
  engine/identity/administrator/secrets evidence stays blocked. Supplied
  snapshot-bound flags are not authenticated application proof:
  `acceptance` is always `not_qualified`, including when the CLI exits zero.
- Done: Land extension pull-secret references and configured TCP-port rules:
  [PR #25](https://github.com/Knuckles-Team/arbora/pull/25),
  merge `e9d99298003518d7b54f33a9854b48e471f6926c`,
  [CI 37173473826](https://github.com/Knuckles-Team/arbora/actions/runs/37173473826)
  at `7d700fc7cefca8c680bb5879cca71a1d1ff3a5e3`.
  The [Helm fixtures](../../tests/deployment/test_helm_extensions.py) cover
  unified sidecar/child and shared-engine renders, absent/configured pull
  secrets, custom/shared extension ports (including 9123), and disabled
  NetworkPolicy. The extension-port rule selects all release pods and admits
  those ports from the same namespace; external ingress peers retain only
  core ports and egress rules are unchanged. CI provisions SHA-256-verified
  Helm 4.3.0. This is render proof, not registry-pull or CNI proof.

All three cited runs passed gates (normal commit/push hooks, locked-environment
mypy and full pytest), scanner quality and wheel build. Release-tag dependency
readiness and publication were skipped. A built wheel is not an independent
installed-package or deployment acceptance receipt.

The following checks remain open and require a separately authorized
qualification environment; this documentation phase performs none of them.

- Open: **Profile matrix:** independently provision both profiles required by
  [R013](requirements.md): development on single-host Compose with none/local
  identity, and production on Kubernetes/Helm with external/local identity,
  proving each claimed mode once its owning contract is available. Record
  profile name/version and digest, chart/Compose revision, resolved render
  digest, exact release/package hashes, image digests, engine sidecar contract
  and selected secrets backend. The Kubernetes preflight names and the
  child/shared render fixtures do not substitute for this R013 profile matrix
  or its sidecar requirement.
- Open: **Readiness before traffic:** prove real engine and application readiness,
  the required first administrator/bootstrap authority and the selected
  secrets backend before admission. Missing/unhealthy engine, missing admin,
  unresolved secret reference or a failing functional probe must keep traffic
  unaccepted. Record one durable engine writer and the actual persistent volume;
  pod/TCP readiness alone is insufficient.
- Open: **Identity and default policy:** qualify the selected mode through the
  served authority described by
  [ARBORA-IDENTITY-001](../identity-access/spec.md) and its
  [requirements](../identity-access/requirements.md).
  Prove none-mode loopback/Host/Origin and exposure guards, local first-admin
  setup where selected, and external-provider sign-in and principal/scope
  mapping where selected. Prove the same default-policy allow/deny decision
  through MCP and API, with no administrative or approver authority granted to
  a service identity. Missing identity authority remains blocked; chart values,
  process health and verifier input flags cannot qualify an identity mode.
- Open: **Selected secrets:** demonstrate runtime resolution through the declared
  backend using references only, plus refusal for an absent/unresolvable
  required reference or unavailable backend. Keep values, tokens and private
  endpoints out of receipts, configuration, logs and traces.
- Open: **Real image pulls:** on the selected Kubernetes release, demonstrate a
  fresh authenticated registry pull of each selected connector/component's
  pinned image using the configured pull-secret references, and match observed
  imageIDs to the intended digests. In a disposable negative case, missing or
  invalid credentials must prevent readiness; a cached image or rendered
  Secret name is insufficient proof.
- Open: **Live CNI enforcement:** record the enforcing CNI and applied policy,
  then prove same-namespace access to configured extension TCP ports (including
  9123), denial from an unallowed namespace, and denial of unconfigured ports.
  Exercise the rule's actual release-wide pod selector. Confirm that configured
  external ingress peers retain only core-port access and that only the
  declared same-namespace, DNS and explicit extra egress paths are permitted.
  A rendered NetworkPolicy does not prove enforcement.
- Open: **Functional and recovery receipts:** follow the applicable
  [first-boot verification steps](../../arbora/skills/arbora-deployment/references/first-boot-verification.md)
  with the selected components: authenticated MCP initialization/discovery and
  an allowed call, the same API result and an unauthorized refusal, and a real
  browser sign-in/sign-out when WebUI is selected. Preserve the test contract's
  idempotent governed-action, engine persistence across restart, isolated
  restore and failed-startup/rollback evidence. Bind every redacted receipt to
  the exact profile/render/release and leave unsupported capability checks open.

## ARBORA-DEPLOY-R007 split: one console-script collision per child

ARBORA-DEPLOY-R007 is a rollup; split by code root/script per SPEC-SIZING-AND-DEPENDENCIES.md §3,
producer (meristem, which deletes its duplicate) before consumer (arbora, which already
owns the entry point).

- Done: **ARBORA-DEPLOY-R007.1** — `arbora-doctor` console-script collision. arbora already
  declares `arbora-doctor = arbora.deployment.doctor:main` on main
  ([merge `fd761d59a171c148b7f2d7d952369c3b2b1a8628`](https://github.com/Knuckles-Team/arbora/commit/fd761d59a171c148b7f2d7d952369c3b2b1a8628)).
  meristem side:
  [PR #118](https://github.com/Knuckles-Team/agent-utilities/pull/118) removes its duplicate
  `arbora-doctor = agent_utilities.deployment.doctor:main` registration (module itself
  stays; it still has real non-test importers). Open, not yet merged.
- Open: Three more of the same literal collision, found while landing R007.1, each its own child:
  `setup-config` (meristem: `agent_utilities.deployment.cli:main` vs arbora:
  `arbora.deployment.cli:main`), `arbora-venv` (`agent_utilities.deployment.venv_sync:main`
  vs `arbora.deployment.venv_sync:main`), `arbora-release-canary`
  (`agent_utilities.deployment.release_canary:main` vs `arbora.deployment.release_canary:main`),
  and `arbora-daemon` (`agent_utilities.gateway.daemon:main` vs `arbora.gateway.daemon:main`).
- Open: Full module retirement of `agent_utilities.deployment.doctor` and the rest of the agent
  runtime's deployment package remains separate, larger work (migrate its remaining non-test
  importers first): the unresolved core of ARBORA-DEPLOY-R007 proper.
owns the entry point). ARBORA-DEPLOY-R007.1 (`arbora-doctor`) is tracked separately
([meristem PR #118](https://github.com/Knuckles-Team/agent-utilities/pull/118)).

- Done: **ARBORA-DEPLOY-R007.2** — `setup-config` console-script collision.
- Done: **ARBORA-DEPLOY-R007.3** — `arbora-daemon` console script retired (ARBORA-HOST-R004).
- Done: **ARBORA-DEPLOY-R007.4** — `arbora-venv` console-script collision.
- Done: **ARBORA-DEPLOY-R007.5** — `arbora-release-canary` console-script collision.
- Done: **ARBORA-DEPLOY-R007.6** — engine_domains and graph_connections doctor checks. Evidence: `tests/deployment/test_doctor_graph.py`.

All four: arbora already declares the script on main
([merge `fd761d59a171c148b7f2d7d952369c3b2b1a8628`](https://github.com/Knuckles-Team/arbora/commit/fd761d59a171c148b7f2d7d952369c3b2b1a8628)).
meristem side: one PR removes all four duplicate registrations at once (same mechanical
change, kept to one PR pair to keep CI load down) —
[meristem PR #120](https://github.com/Knuckles-Team/agent-utilities/pull/120). Open, not
yet merged. None of the four underlying meristem modules is deleted; each still has a real
non-test importer (or, for `agent_utilities.deployment.cli`, a direct test importer and a
`python -m` invocation path), so full module retirement remains separate, larger work.

- Open: Full module retirement of `agent_utilities.deployment.cli`, `.venv_sync`,
  `.release_canary`, and `agent_utilities.gateway.daemon` remains separate, larger work (migrate
  their remaining non-test importers first): the unresolved core of ARBORA-DEPLOY-R007 proper,
  alongside `agent_utilities.deployment.doctor`'s retirement noted under R007.1.

## ARBORA-DEPLOY-R016 serving container is the in-pod host

- Done: Retire the separate daemon container and its values/schema keys; the serving container holds the host role in child and sidecar placement.
- Done: Contract test `tests/deployment/test_helm_daemon_sidecar.py` renders every placement.
- Open: Apply the manifest and record the serving container as host-lock holder.

## Decomposition children (tracked)

- Open: **ARBORA-DEPLOY-R009:** Arbora owns hosting/deployment configuration settings
- Open: **ARBORA-DEPLOY-R009.1:** Remaining scope of ARBORA-DEPLOY-R009 (slice .1): Arbora owns hosting/deployment configuration settings
- Done: **ARBORA-DEPLOY-R009.2.1:** Small-profile import footprint. Evidence: `tests/deployment/test_small_profile_footprint.py::test_cold_import_of_the_served_surface_loads_no_external_driver`.
- Done: **ARBORA-DEPLOY-R009.4:** graph_config operation core in Arbora. Evidence: `tests/core/test_agent_config_admin.py`.

## ARBORA-DEPLOY-R017 engine-routed deployment placement and assembly

Order: R017.1 (placement), R017.2 to R017.5 (assembly, view, confirm, refusal), R017.6 and R017.7 (existing artifacts, in-process), R017.8 (command). Engine prerequisites: a deployment-placement question kind for `MEDULLA-DECISION-ENGINE-R046`/`R111` (name to be assigned by the engine spec) and a real `AgentAssemble` handler replacing the refusal stub. Until then R017.1 to R017.5 land only as typed refusals.

- Open: **ARBORA-DEPLOY-R017:** Deployment placement and sequencing go through the engine
- Open: **ARBORA-DEPLOY-R017.1:** Placement Decide mapping
- Open: **ARBORA-DEPLOY-R017.2:** Request assembly for a deployment task
- Open: **ARBORA-DEPLOY-R017.3:** Present the assembled graph read-only
- Open: **ARBORA-DEPLOY-R017.4:** Confirm through plan/confirm
- Open: **ARBORA-DEPLOY-R017.5:** Typed refusal while unavailable
- Open: **ARBORA-DEPLOY-R017.6:** Existing artifact per target
- Open: **ARBORA-DEPLOY-R017.7:** In-process unchanged
- Open: **ARBORA-DEPLOY-R017.8:** Thin setup-config subcommand
- Open: **ARBORA-DEPLOY-R017.9:** Checker dispatches on document shape.
- Open: **ARBORA-DEPLOY-R017.10:** Compose/Swarm literal images use the existing grammar.
- Open: **ARBORA-DEPLOY-R017.11:** Template mode accepts the required-variable form.
- Open: **ARBORA-DEPLOY-R017.12:** Resolve mode checks the supplied value.
- Open: **ARBORA-DEPLOY-R017.13:** Run the checker on the shipped Compose and Swarm files.
- Open: **ARBORA-DEPLOY-R017.14:** Helm pinned and default renders.
- Open: **ARBORA-DEPLOY-R017.15:** systemd stays refused.
- Open: **ARBORA-DEPLOY-R017.16:** Environment policy drives the check.
- Open: **ARBORA-DEPLOY-R018:** Small deployment shares one engine per host (rollup).
- Open: **ARBORA-DEPLOY-R018.1:** Engine endpoint type.
- Open: **ARBORA-DEPLOY-R018.2:** Probe before start.
- Open: **ARBORA-DEPLOY-R018.3:** Start only when absent.
- Open: **ARBORA-DEPLOY-R018.4:** Second process attaches.
- Open: **ARBORA-DEPLOY-R018.5:** Owner stands down the engine.
- Open: **ARBORA-DEPLOY-R018.6:** Remote engine skips local start.

- Open: Define and prove preserve development skill coverage (`ARBORA-DEPLOY-R010.1`) through the owning production entrypoint.

- Open: Define and prove keep deployment guidance executable and synchronized (`ARBORA-DEPLOY-R010.2`) through the owning production entrypoint.

- Open: Define and prove publish complete supported-profile instructions (`ARBORA-DEPLOY-R010.3`) through the owning production entrypoint.

- Open: Define and prove preserve original profile and first-boot obligations (`ARBORA-DEPLOY-R013.1`) through the owning production entrypoint.

- Open: Define and prove record explicit component deploy or reuse choices (`ARBORA-DEPLOY-R013.2`) through the owning production entrypoint.

- Open: Define and prove make reused components connect-only (`ARBORA-DEPLOY-R013.3`) through the owning production entrypoint.

- Open: Define and prove define the complete deployment target matrix (`ARBORA-DEPLOY-R013.4`) through the owning production entrypoint.

- Open: Define and prove bind confirmed profiles to governed deployment (`ARBORA-DEPLOY-R013.5`) through the owning production entrypoint.

- Open: Define and prove prove component choices and target profiles (`ARBORA-DEPLOY-R013.6`) through the owning production entrypoint.

- Open: Define and prove preserve reuse through engine attachment (`ARBORA-DEPLOY-R018.7`) through the owning production entrypoint.

- Open: Define and prove keep reused resources alive across host shutdown (`ARBORA-DEPLOY-R018.8`) through the owning production entrypoint.

## Additional owner requirements

- Open: `ARBORA-DEPLOY-R009.2`: Deliver every requirement in requirements.md.
- Open: `ARBORA-DEPLOY-R009.3`: Deliver every requirement in requirements.md.
- Open: `ARBORA-DEPLOY-R013.7`: Deliver every requirement in requirements.md.
- Open: `ARBORA-DEPLOY-R010.4`: Deliver every requirement in requirements.md.

## Medulla companion rows (2026-10-28)

- Open: `ARBORA-DEPLOY-R017.17`: Deliver the requirement in requirements.md.

## Row tasks

One task per active leaf row, in requirement order. A rollup parent is covered by its child rows.

- [ ] T001 ARBORA-DEPLOY-R001 Rendered workload images are digest-pinned.
- [ ] T002 ARBORA-DEPLOY-R002 Executable release-qualification runbook.
- [ ] T003 ARBORA-DEPLOY-R003 Pre-push hooks always run the full gate suite.
- [ ] T004 ARBORA-DEPLOY-R004 mypy hook type-checks the test suite.
- [ ] T005 ARBORA-DEPLOY-R005 Web UI keeps its component package name.
- [ ] T006 ARBORA-DEPLOY-R006 The base dependency pins a compatible connector-SDK floor.
- [ ] T007 ARBORA-DEPLOY-R007.1 arbora-doctor console-script collision resolved.
- [ ] T008 ARBORA-DEPLOY-R007.2 setup-config console-script collision resolved.
- [ ] T009 ARBORA-DEPLOY-R007.3 arbora-daemon console script retired.
- [ ] T010 ARBORA-DEPLOY-R007.4 arbora-venv console-script collision resolved.
- [ ] T011 ARBORA-DEPLOY-R007.5 arbora-release-canary is declared only by arbora.
- [ ] T012 ARBORA-DEPLOY-R007.6 The Arbora doctor checks the served engine families and graph connections.
- [ ] T013 ARBORA-DEPLOY-R008 Arbora hosts its own boundary modules.
- [ ] T014 ARBORA-DEPLOY-R009.1 Arbora hosts a typed arbora.core.config_admin hosting/deployment settings module.
- [ ] T015 ARBORA-DEPLOY-R009.4 Arbora owns the config administration core.
- [ ] T016 ARBORA-DEPLOY-R011 Deployment test suite passes in full.
- [ ] T017 ARBORA-DEPLOY-R012 Pipelines dependency is referenced at main, never pinned.
- [ ] T018 ARBORA-DEPLOY-R014 Split packages are consumed by published version range.
- [ ] T019 ARBORA-DEPLOY-R015 AVX2 guard test runs on any hosted runner.
- [ ] T020 ARBORA-DEPLOY-R016 The serving container is the one in-pod host process.
- [ ] T021 ARBORA-DEPLOY-R017.1 Placement Decide mapping.
- [ ] T022 ARBORA-DEPLOY-R017.2 Request assembly for a deployment task.
- [ ] T023 ARBORA-DEPLOY-R017.3 Present the assembled graph read-only.
- [ ] T024 ARBORA-DEPLOY-R017.4 Confirm through plan confirmation.
- [ ] T025 ARBORA-DEPLOY-R017.5 Typed refusal while a producer is unavailable.
- [ ] T026 ARBORA-DEPLOY-R017.6 Select the existing artifact per target.
- [ ] T027 ARBORA-DEPLOY-R017.7 In-process start stays unchanged.
- [ ] T028 ARBORA-DEPLOY-R017.8 Thin deploy subcommand.
- [ ] T029 ARBORA-DEPLOY-R017.9 Checker dispatches on document shape.
- [ ] T030 ARBORA-DEPLOY-R017.10 Compose and swarm literal images use the existing grammar.
- [ ] T031 ARBORA-DEPLOY-R017.11 Template mode accepts the required-variable form.
- [ ] T032 ARBORA-DEPLOY-R017.12 Resolve mode validates the given value.
- [ ] T033 ARBORA-DEPLOY-R017.13 Run the checker on the shipped Compose and swarm files.
- [ ] T034 ARBORA-DEPLOY-R017.14 Helm pinned and default renders.
- [ ] T035 ARBORA-DEPLOY-R017.15 systemd stays refused.
- [ ] T036 ARBORA-DEPLOY-R017.16 Environment policy drives the digest check.
- [ ] T037 ARBORA-DEPLOY-R018.1 Engine endpoint type.
- [ ] T038 ARBORA-DEPLOY-R018.2 Probe before start.
- [ ] T039 ARBORA-DEPLOY-R018.3 Start only when absent.
- [ ] T040 ARBORA-DEPLOY-R018.4 Second process attaches.
- [ ] T041 ARBORA-DEPLOY-R018.5 Last client out stops the engine arbora started.
- [ ] T042 ARBORA-DEPLOY-R018.6 Remote engine skips local start.
- [ ] T043 ARBORA-DEPLOY-R010.1 Development skill keeps every original topic.
- [ ] T044 ARBORA-DEPLOY-R010.2 Deployment examples stay executable and synchronized.
- [ ] T045 ARBORA-DEPLOY-R010.3 Supported-profile instructions are complete.
- [ ] T046 ARBORA-DEPLOY-R013.1 Profile and first-boot obligations are retained.
- [ ] T047 ARBORA-DEPLOY-R013.2 Record explicit deploy or reuse choice per component.
- [ ] T048 ARBORA-DEPLOY-R013.3 Reused components are connect-only.
- [ ] T049 ARBORA-DEPLOY-R013.4 Deployment target matrix is complete.
- [ ] T050 ARBORA-DEPLOY-R013.5 Confirmed profiles bind to governed deployment.
- [ ] T051 ARBORA-DEPLOY-R013.6 Component choices and target profiles are proven served.
- [ ] T052 ARBORA-DEPLOY-R018.7 A reused engine only attaches.
- [ ] T053 ARBORA-DEPLOY-R018.8 A reused engine survives host shutdown.
- [ ] T054 ARBORA-DEPLOY-R009.2.1 Small-profile import footprint.
- [ ] T055 ARBORA-DEPLOY-R009.2.2 Single image serves a capability and refusal parity matrix.
- [ ] T056 ARBORA-DEPLOY-R009.3 Deployment selection imports the approved ontology once.
- [ ] T057 ARBORA-DEPLOY-R013.7 Secrets default to the vault and the trust bundle reaches every component.
- [ ] T058 ARBORA-DEPLOY-R010.4 Complexity gate adds argument-count and try-statement rules.
- [ ] T059 ARBORA-DEPLOY-R600.1 Lock model.
- [ ] T060 ARBORA-DEPLOY-R600.2 Spawn guard behavior.
- [ ] T061 ARBORA-DEPLOY-R600.3 Lock wiring.
- [ ] T062 ARBORA-DEPLOY-R600.4 Lock refusal.
- [ ] T063 ARBORA-DEPLOY-R600.5 Lock evidence.
- [ ] T064 ARBORA-DEPLOY-R600.6 Lock hot-path proof.
- [ ] T065 ARBORA-DEPLOY-R017.17 Capacity-adjustment actuation and applied-state receipt.
- [ ] T066 ARBORA-DEPLOY-R017.18 Digest checker lives in the package.
- [ ] T067 ARBORA-DEPLOY-R017.19 Serve the placement and apply operations.
- [ ] T068 ARBORA-DEPLOY-R017.20 Publish the deployment template and its tool components.
- [ ] T069 ARBORA-DEPLOY-R017.21 Run the confirmed step graph through governed fleet calls.
- [ ] T070 ARBORA-DEPLOY-R018.9 Engine pool wires the local lifecycle.
- [ ] T071 ARBORA-DEPLOY-R601 Publish deployment templates and tool components.
