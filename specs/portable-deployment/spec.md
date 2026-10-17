# ARBORA-DEPLOY — Portable Arbora development and deployment

**Owner:** arbora. **Requirement IDs:** ARBORA-DEPLOY-R001, ARBORA-DEPLOY-R002, ARBORA-DEPLOY-R003, ARBORA-DEPLOY-R004, ARBORA-DEPLOY-R005, ARBORA-DEPLOY-R006, ARBORA-DEPLOY-R007, ARBORA-DEPLOY-R008, ARBORA-DEPLOY-R009, ARBORA-DEPLOY-R010, ARBORA-DEPLOY-R011, ARBORA-DEPLOY-R012, ARBORA-DEPLOY-R013, ARBORA-DEPLOY-R014. **Delivery:** implemented in part; **acceptance:** pending. The existing deployment utilities are real source, but a clean contributor install, all profile variants and first-boot proof are not accepted here.

## State legend

`Proposed` = contract ready to build; `Implemented` = source exists; `Verified` = identified commit passes the specified tests; `Released` = that exact commit and package are published; `Accepted` = an independently provisioned environment passes first boot, identity and recovery checks. Status is per deliverable, and failed or unavailable evidence cannot be inferred as passing.

## Contributor outcome

An external contributor can clone only Arbora, install published dependencies, run deterministic unit and contract gates, and provision a disposable development instance using documented commands. A maintainer can apply an explicit, reviewable configuration to a single-host container environment or Kubernetes. Neither path assumes a pre-existing private cluster, credentials, namespace, sibling checkout, or local inventory. The public GitHub Pages site explains operator usage; this repository's `specs/` holds the build contract.

## Required behavior

1. **Profiles.** A versioned profile explicitly selects target, release, runtime, writable paths, configuration, secret references, network, identity and validation. Unknown/missing keys fail with exact section/key diagnostics. Named environments are data, not a code enum. No plaintext secret enters a profile, log or receipt.
2. **Development path.** Core source tests use installed package APIs and in-process fakes. A disposable development profile can launch Arbora and the graph engine with local or disabled identity only under loopback and explicit acknowledgement, followed by creation of the first administrator where the mode requires one. Optional WebUI, connector and external identity dependencies are selected explicitly.
3. **Production path.** A single-host Compose profile and Kubernetes profile use pinned image digests, writable mounts, read-only root where supported, reference-based secrets and readiness/functional checks. Identity mode and policy defaults are explicit; a missing secret or unsupported posture blocks apply. An engine sidecar or separately declared engine endpoint supplies the durable authority. No second graph or mirrored database is implied.
4. **One installer and doctor.** `setup-config`, `agent-utilities-doctor`, `arbora-release-canary` and `arbora-production-ops` are the current Arbora-owned entrypoints. They share configuration rules and produce bounded, privacy-safe diagnostic results. Former agent-runtime deployment copies and duplicate scripts are retired after behavior parity.
5. **Release evidence.** Build a wheel, install it in a fresh environment and validate entrypoints, graph engine binary/client compatibility, exact package versions, configuration schema, and sample first-boot operations. A source-only success is distinct from a package or runtime success.
6. **Cloud PR compatibility.** Required PR checks provision everything they need from published packages, fixture data or disposable local containers. A live private environment is a separate post-release acceptance tier, never a prerequisite for a remote contributor's ordinary PR. Hook execution must be real and discoverable; a missing hook or pointing to a nonexistent config fails a CI audit.

## Acceptance examples

- A fresh clone on a supported Python version runs `uv sync --extra test`, unit/contract checks, package build and wheel-import smoke with no sibling checkout.
- The disposable profile reaches readiness and serves one authorized read, one denied read and one idempotent governed action. Resulting receipts name release and contract digests.
- A missing secret, unrecognized profile key, non-writable runtime path, unpinned production image, absent engine contract, unsafe identity mode or failed functional check stops apply before claiming success.
- Recovery from failed startup and rollback to a prior pinned release preserve durable state and report the exact reason. No action log includes a token or private endpoint.
- Every required gate has a portable CI execution path. External-integration probes remain available in a separate opt-in workflow with recorded environment identity.

Requirement IDs are defined in [requirements.md](requirements.md); delivery state per ID is in `status.json`.

## Confirmed deployment choices and guidance

R013 adds explicit per-component deploy or reuse choices and a complete target and privilege matrix.
R018 keeps reused engines connect-only across startup, retry and shutdown.
R010 binds deployment guidance to supported profiles and both actual MCP transport entrypoints.
Existing profile, first-boot and engine lifecycle acceptance remains required in full.
The new target and no-effect reuse scenarios have not been run.

## Additional owner requirements

- `ARBORA-DEPLOY-R009.2`: Owner scope and complete acceptance are defined in requirements.md.
- `ARBORA-DEPLOY-R009.4`: Owner scope and complete acceptance are defined in requirements.md.
- `ARBORA-DEPLOY-R009.3`: Owner scope and complete acceptance are defined in requirements.md.
- `ARBORA-DEPLOY-R013.7`: Owner scope and complete acceptance are defined in requirements.md.
- `ARBORA-DEPLOY-R010.4`: Owner scope and complete acceptance are defined in requirements.md.
