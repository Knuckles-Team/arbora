# ARBORA-HARNESS-PLUGINS — Coding harness plugins generated from the registry

Owner: arbora (`ARBORA`). Sole owner of the bundle, its install lifecycle, its auth handshake and the workstream operations.

Component: Coding harness plugins

Depends-on: ARBORA-IDENTITY-R030.4, ARBORA-OPS-R897.4, ARBORA-OPS-R844.1, ARBORA-DEPLOY-POLICY-R006, [MEDULLA-IDENTITY-R723](https://github.com/Knuckles-Team/medulla/blob/main/specs/engine-identity-and-scope/requirements.md), [US-CONTENT-R007](https://github.com/Knuckles-Team/universal-skills/blob/main/specs/catalog-content/requirements.md)

References: [PIPE-RELEASE-R028](https://github.com/Knuckles-Team/pipelines/blob/main/specs/ordered-digest-release/requirements.md), [TUIG-RUNTIME-R013](https://github.com/Knuckles-Team/tuig/blob/main/specs/terminal-runtime/requirements.md), [MEDULLA-CQ-ORCH-R820](https://github.com/Knuckles-Team/medulla/blob/main/specs/orchestration-competency/requirements.md)

## User outcome

A developer works in a coding harness. One install gives that harness the whole platform through the one door. The door is the arbora MCP server with the six intent tools: `find`, `ask`, `why`, `write`, `act` and `manage`. The fleet is reached through `fleet.call`. Skills, prompts and agents come from the registry, never from hand-kept copies.

Arbora generates the plugin on the developer's machine at install time. The bundle is never committed. One harness-neutral core feeds one renderer per harness. The first targets are `claude-code` and `opencode`. The next targets are `codex` and `grok`.

Two harnesses on one workspace share one workstream. A session in one harness leaves a checkpoint. A later session in another harness receives that checkpoint at attach. The state lives in the engine. Arbora serves it.

## User Scenarios & Testing

### User Story 1 - Install the platform into a coding harness (Priority: P1)

A developer runs one command and gets the door, the door skills and the agents in the harness.

**Acceptance Scenarios**:

1. **Given** a configured arbora endpoint, **When** the developer runs `arbora plugin install --harness claude-code`, **Then** the harness lists the arbora MCP server, the door skills and the registered agents.
2. **Given** the same endpoint, **When** the developer runs `arbora plugin install --harness opencode`, **Then** the harness loads the generated plugin with the same server, skills and commands.
3. **Given** a hand-written arbora MCP entry already in the harness config, **When** install runs, **Then** install refuses with `DUPLICATE_DOOR` and prints the removal steps.

### User Story 2 - Sign in once, with no credential in a file of the bundle (Priority: P1)

The harness signs in with the native public client. The bundle holds a client ID and a loopback callback only.

**Acceptance Scenarios**:

1. **Given** a fresh install, **When** the harness first connects, **Then** the harness runs the authorization code flow with PKCE and the fixed callback of its profile.
2. **Given** a generated bundle, **When** a test scans every file, **Then** the scan finds no token, secret or password value.

### User Story 3 - Continue work in another harness (Priority: P1)

A developer stops in one harness and continues in another on the same checkout.

**Acceptance Scenarios**:

1. **Given** a `claude-code` session that wrote a checkpoint, **When** an `opencode` session attaches on the same workspace, **Then** the attach result carries that checkpoint.
2. **Given** a second principal, **When** it reads the first principal's workstream, **Then** arbora returns `WORKSTREAM_NOT_FOUND`, the same as for an unknown workstream.

### User Story 4 - Stay current without manual edits (Priority: P2)

The bundle follows the registry. A changed registry gives a new bundle digest.

**Acceptance Scenarios**:

1. **Given** an installed bundle and a changed registry, **When** the developer runs `arbora plugin status`, **Then** the state is `stale`.
2. **Given** the `claude-code` marketplace entry, **When** a new session starts, **Then** the harness runs the bundle command and loads the current digest.

### Edge Cases

- The endpoint is unset: bundle generation refuses `ENDPOINT_UNSET` and writes no file.
- The content publication receipt is stale: generation refuses `reingestion-unreconciled` and writes no file.
- A hook runs without a credential: the hook returns `AUTH_REQUIRED` as a non-blocking note. The prompt still runs.
- The harness name is unknown: the command refuses `HARNESS_UNKNOWN`.

## Success Criteria

- `claude-code` and `opencode` bundles pass the harness validators in the release proof of `PIPE-RELEASE-R028`.
- An attach call returns in 150 ms at p95 against a loopback engine.
- The attach context is 4096 bytes or fewer.
- Two runs of the bundle command on one registry give one digest.

## Assumptions

- Each harness keeps its own OAuth token store. Arbora never reads it.
- A harness that cannot call an MCP tool from a hook runs `arbora plugin hook` instead.
- Harness facts come from the public harness documentation recorded in [research.md](research.md).

## Requirements

The rows are in [requirements.md](requirements.md). The architecture is in [plan.md](plan.md).

## Why a new spec

The search `git grep -n -i -E "plugin|marketplace|harness bundle" -- specs` found no owner. `ARBORA-DEPLOY-POLICY-R006` exports skills with a digest stamp; this spec reuses its gate. `ARBORA-IDENTITY-R030.4` owns the native client; this spec adds a child row there. `ARBORA-OPS-R897` owns turn records; the workstream checkpoint writes through it. No spec owns a harness bundle, its install lifecycle or the workstream operations.

## Non-goals

- The engine record of a workstream. `MEDULLA-IDENTITY-R723` owns it.
- Skill text. `US-CONTENT-R007` owns the door skills and the portable frontmatter.
- The release proof and the marketplace publication rule. `PIPE-RELEASE-R028` owns them.
- The terminal client. `TUIG-RUNTIME-R013` makes it a workstream peer.
- A selection of skills per task. The engine owns selection; the bundle carries only a declared set.
