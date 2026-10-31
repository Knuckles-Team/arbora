# Delivery tasks and evidence

- [x] Implement typed deployment profile, preflight, doctor, release-canary and production-ops modules. Evidence: `arbora/deployment/` and `tests/deployment/`. **Source implemented; independent install and first-boot acceptance pending.**
- [ ] Add and verify the fresh-checkout development path, including a disposable engine and clear optional component selection.
- [ ] Complete migration of deployment modules and scripts from agent-runtime internals onto supported public ports.
- [ ] Render and validate both Compose and Kubernetes profiles from one closed schema; reject plaintext secrets and floating production images.
- [ ] Add positive and negative first-boot/rollback tests using provisioned CI resources and reference-only credentials.
- [ ] Audit hook installation and required CI gates; make all ordinary PR gates reproducible in a cloud checkout.
- [ ] Publish usage to the Arbora GitHub Pages site and link the contribution guide to this spec and a verified setup command.
- [ ] Record exact commit, package hashes, CI runs, profile digests and redacted functional receipts. Then promote individual deliverables to Verified/Released/Accepted.

**Current evidence gap:** The existing source and test names establish implementation progress. They do not establish a public clean-install, profile matrix or first-boot acceptance receipt for this complete contract.

## R013 landed evidence and remaining acceptance

Evidence reconciled against [main `937d38569cef9f07abf46fd064aff98c24a33c85`](https://github.com/Knuckles-Team/arbora/commit/937d38569cef9f07abf46fd064aff98c24a33c85).
Each merge below is an ancestor of that revision. These receipts establish
bounded source and CI progress. **ARBORA-DEPLOY-R013 remains BUILDING; the
spec remains NOT_AUDITED.** The other requirements retain their existing states;
the R005 WebUI rename dependency remains separate and open.

- [x] Land Kubernetes preflight client checks:
  [PR #23](https://github.com/Knuckles-Team/arbora/pull/23),
  merge `2abb9ebb3905e2daedc3e4c5e83dc5682c13f2bd`,
  [CI 37174546666](https://github.com/Knuckles-Team/arbora/actions/runs/37174546666)
  at `7694c6eb891a23186805bc8cb62c531fc642ee5f`.
  The [preflight tests](../../tests/deployment/test_preflight.py) cover
  `dev`, `test`, `prod`, `enterprise`, validated named targets, missing
  kubectl/Helm, failed/timed-out probes and invalid profiles. Successful client
  probes leave cluster access and first boot unverified.
- [x] Land the [Kubernetes observation verifier](../../arbora/deployment/kubernetes_first_boot.py)
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
- [x] Land extension pull-secret references and configured TCP-port rules:
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

- [ ] **Profile matrix:** independently provision both profiles required by
  [R013](requirements.md): development on single-host Compose with none/local
  identity, and production on Kubernetes/Helm with external/local identity,
  proving each claimed mode once its owning contract is available. Record
  profile name/version and digest, chart/Compose revision, resolved render
  digest, exact release/package hashes, image digests, engine sidecar contract
  and selected secrets backend. The Kubernetes preflight names and the
  child/shared render fixtures do not substitute for this R013 profile matrix
  or its sidecar requirement.
- [ ] **Readiness before traffic:** prove real engine and application readiness,
  the required first administrator/bootstrap authority and the selected
  secrets backend before admission. Missing/unhealthy engine, missing admin,
  unresolved secret reference or a failing functional probe must keep traffic
  unaccepted. Record one durable engine writer and the actual persistent volume;
  pod/TCP readiness alone is insufficient.
- [ ] **Identity and default policy:** qualify the selected mode through the
  served authority described by
  [ARBORA-IDENTITY-001](../identity-access/spec.md) and its
  [requirements](../identity-access/requirements.md).
  Prove none-mode loopback/Host/Origin and exposure guards, local first-admin
  setup where selected, and external-provider sign-in and principal/scope
  mapping where selected. Prove the same default-policy allow/deny decision
  through MCP and API, with no administrative or approver authority granted to
  a service identity. Missing identity authority remains blocked; chart values,
  process health and verifier input flags cannot qualify an identity mode.
- [ ] **Selected secrets:** demonstrate runtime resolution through the declared
  backend using references only, plus refusal for an absent/unresolvable
  required reference or unavailable backend. Keep values, tokens and private
  endpoints out of receipts, configuration, logs and traces.
- [ ] **Real image pulls:** on the selected Kubernetes release, demonstrate a
  fresh authenticated registry pull of each selected connector/component's
  pinned image using the configured pull-secret references, and match observed
  imageIDs to the intended digests. In a disposable negative case, missing or
  invalid credentials must prevent readiness; a cached image or rendered
  Secret name is insufficient proof.
- [ ] **Live CNI enforcement:** record the enforcing CNI and applied policy,
  then prove same-namespace access to configured extension TCP ports (including
  9123), denial from an unallowed namespace, and denial of unconfigured ports.
  Exercise the rule's actual release-wide pod selector. Confirm that configured
  external ingress peers retain only core-port access and that only the
  declared same-namespace, DNS and explicit extra egress paths are permitted.
  A rendered NetworkPolicy does not prove enforcement.
- [ ] **Functional and recovery receipts:** follow the applicable
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

- [x] **ARBORA-DEPLOY-R007.1** — `arbora-doctor` console-script collision. arbora already
  declares `arbora-doctor = arbora.deployment.doctor:main` on main
  ([merge `fd761d59a171c148b7f2d7d952369c3b2b1a8628`](https://github.com/Knuckles-Team/arbora/commit/fd761d59a171c148b7f2d7d952369c3b2b1a8628)).
  meristem side:
  [PR #118](https://github.com/Knuckles-Team/agent-utilities/pull/118) removes its duplicate
  `arbora-doctor = agent_utilities.deployment.doctor:main` registration (module itself
  stays; it still has real non-test importers). Open, not yet merged.
- [ ] Three more of the same literal collision, found while landing R007.1, each its own child:
  `setup-config` (meristem: `agent_utilities.deployment.cli:main` vs arbora:
  `arbora.deployment.cli:main`), `arbora-venv` (`agent_utilities.deployment.venv_sync:main`
  vs `arbora.deployment.venv_sync:main`), `arbora-release-canary`
  (`agent_utilities.deployment.release_canary:main` vs `arbora.deployment.release_canary:main`),
  and `arbora-daemon` (`agent_utilities.gateway.daemon:main` vs `arbora.gateway.daemon:main`).
- [ ] Full module retirement of `agent_utilities.deployment.doctor` and the rest of the agent
  runtime's deployment package remains separate, larger work (migrate its remaining non-test
  importers first): the unresolved core of ARBORA-DEPLOY-R007 proper.
owns the entry point). ARBORA-DEPLOY-R007.1 (`arbora-doctor`) is tracked separately
([meristem PR #118](https://github.com/Knuckles-Team/agent-utilities/pull/118)).

- [x] **ARBORA-DEPLOY-R007.2** — `setup-config` console-script collision.
- [x] **ARBORA-DEPLOY-R007.3** — `arbora-daemon` console script retired (ARBORA-HOST-R004).
- [x] **ARBORA-DEPLOY-R007.4** — `arbora-venv` console-script collision.
- [x] **ARBORA-DEPLOY-R007.5** — `arbora-release-canary` console-script collision.
- [x] **ARBORA-DEPLOY-R007.6** — engine_domains and graph_connections doctor checks. Evidence: `tests/deployment/test_doctor_graph.py`.

All four: arbora already declares the script on main
([merge `fd761d59a171c148b7f2d7d952369c3b2b1a8628`](https://github.com/Knuckles-Team/arbora/commit/fd761d59a171c148b7f2d7d952369c3b2b1a8628)).
meristem side: one PR removes all four duplicate registrations at once (same mechanical
change, kept to one PR pair to keep CI load down) —
[meristem PR #120](https://github.com/Knuckles-Team/agent-utilities/pull/120). Open, not
yet merged. None of the four underlying meristem modules is deleted; each still has a real
non-test importer (or, for `agent_utilities.deployment.cli`, a direct test importer and a
`python -m` invocation path), so full module retirement remains separate, larger work.

- [ ] Full module retirement of `agent_utilities.deployment.cli`, `.venv_sync`,
  `.release_canary`, and `agent_utilities.gateway.daemon` remains separate, larger work (migrate
  their remaining non-test importers first): the unresolved core of ARBORA-DEPLOY-R007 proper,
  alongside `agent_utilities.deployment.doctor`'s retirement noted under R007.1.

## ARBORA-DEPLOY-R016 serving container is the in-pod host

- [x] Retire the separate daemon container and its values/schema keys; the serving container holds the host role in child and sidecar placement.
- [x] Contract test `tests/deployment/test_helm_daemon_sidecar.py` renders every placement.
- [ ] Apply the manifest and record the serving container as host-lock holder.

## Decomposition children (tracked)

- [ ] **ARBORA-DEPLOY-R009:** Arbora owns hosting/deployment configuration settings
- [ ] **ARBORA-DEPLOY-R009.1:** Remaining scope of ARBORA-DEPLOY-R009 (slice .1): Arbora owns hosting/deployment configuration settings
- [x] **ARBORA-DEPLOY-R009.2.1:** Small-profile import footprint. Evidence: `tests/deployment/test_small_profile_footprint.py::test_cold_import_of_the_served_surface_loads_no_external_driver`.
- [x] **ARBORA-DEPLOY-R009.4:** graph_config operation core in Arbora. Evidence: `tests/core/test_agent_config_admin.py`.

## ARBORA-DEPLOY-R017 engine-routed deployment placement and assembly

Order: R017.1 (placement), R017.2 to R017.5 (assembly, view, confirm, refusal), R017.6 and R017.7 (existing artifacts, in-process), R017.8 (command). Engine prerequisites: a deployment-placement question kind for `MEDULLA-DECISION-ENGINE-R046`/`R111` (name to be assigned by the engine spec) and a real `AgentAssemble` handler replacing the refusal stub. Until then R017.1 to R017.5 land only as typed refusals.

- [ ] **ARBORA-DEPLOY-R017:** Deployment placement and sequencing go through the engine
- [ ] **ARBORA-DEPLOY-R017.1:** Placement Decide mapping
- [ ] **ARBORA-DEPLOY-R017.2:** Request assembly for a deployment task
- [ ] **ARBORA-DEPLOY-R017.3:** Present the assembled graph read-only
- [ ] **ARBORA-DEPLOY-R017.4:** Confirm through plan/confirm
- [ ] **ARBORA-DEPLOY-R017.5:** Typed refusal while unavailable
- [ ] **ARBORA-DEPLOY-R017.6:** Existing artifact per target
- [ ] **ARBORA-DEPLOY-R017.7:** In-process unchanged
- [ ] **ARBORA-DEPLOY-R017.8:** Thin setup-config subcommand
- [ ] **ARBORA-DEPLOY-R017.9:** Checker dispatches on document shape.
- [ ] **ARBORA-DEPLOY-R017.10:** Compose/Swarm literal images use the existing grammar.
- [ ] **ARBORA-DEPLOY-R017.11:** Template mode accepts the required-variable form.
- [ ] **ARBORA-DEPLOY-R017.12:** Resolve mode checks the supplied value.
- [ ] **ARBORA-DEPLOY-R017.13:** Run the checker on the shipped Compose and Swarm files.
- [ ] **ARBORA-DEPLOY-R017.14:** Helm pinned and default renders.
- [ ] **ARBORA-DEPLOY-R017.15:** systemd stays refused.
- [ ] **ARBORA-DEPLOY-R017.16:** Environment policy drives the check.
- [ ] **ARBORA-DEPLOY-R018:** Small deployment shares one engine per host (rollup).
- [ ] **ARBORA-DEPLOY-R018.1:** Engine endpoint type.
- [ ] **ARBORA-DEPLOY-R018.2:** Probe before start.
- [ ] **ARBORA-DEPLOY-R018.3:** Start only when absent.
- [ ] **ARBORA-DEPLOY-R018.4:** Second process attaches.
- [ ] **ARBORA-DEPLOY-R018.5:** Owner stands down the engine.
- [ ] **ARBORA-DEPLOY-R018.6:** Remote engine skips local start.

- [ ] Define and prove preserve development skill coverage (`ARBORA-DEPLOY-R010.1`) through the owning production entrypoint.

- [ ] Define and prove keep deployment guidance executable and synchronized (`ARBORA-DEPLOY-R010.2`) through the owning production entrypoint.

- [ ] Define and prove publish complete supported-profile instructions (`ARBORA-DEPLOY-R010.3`) through the owning production entrypoint.

- [ ] Define and prove preserve original profile and first-boot obligations (`ARBORA-DEPLOY-R013.1`) through the owning production entrypoint.

- [ ] Define and prove record explicit component deploy or reuse choices (`ARBORA-DEPLOY-R013.2`) through the owning production entrypoint.

- [ ] Define and prove make reused components connect-only (`ARBORA-DEPLOY-R013.3`) through the owning production entrypoint.

- [ ] Define and prove define the complete deployment target matrix (`ARBORA-DEPLOY-R013.4`) through the owning production entrypoint.

- [ ] Define and prove bind confirmed profiles to governed deployment (`ARBORA-DEPLOY-R013.5`) through the owning production entrypoint.

- [ ] Define and prove prove component choices and target profiles (`ARBORA-DEPLOY-R013.6`) through the owning production entrypoint.

- [ ] Define and prove preserve reuse through engine attachment (`ARBORA-DEPLOY-R018.7`) through the owning production entrypoint.

- [ ] Define and prove keep reused resources alive across host shutdown (`ARBORA-DEPLOY-R018.8`) through the owning production entrypoint.

## Additional owner requirements

- [ ] `ARBORA-DEPLOY-R009.2`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-DEPLOY-R009.3`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-DEPLOY-R013.7`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-DEPLOY-R010.4`: Deliver every requirement in requirements.md.

## Medulla companion rows (2026-10-28)

- [ ] `ARBORA-DEPLOY-R017.17`: Deliver the requirement in requirements.md.
