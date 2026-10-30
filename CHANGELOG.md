# Changelog

## [Unreleased]

## [0.1.8.dev7] - 2026-10-30

### Removed
- The configuration resolver no longer falls back to `GRAPH_OS_CONFIG_DIR`, `AGENT_UTILITIES_CONFIG_DIR`, `~/.config/graph-os` or `~/.config/agent-utilities`, and the deprecation warning is gone. Configuration lives under `ARBORA_CONFIG_DIR` / `~/.config/arbora` only; move any files still under the old directories. The multiplexer sandbox and child-environment allowlist pass `ARBORA_CONFIG_DIR`, the venv auto-sync queue lives under `~/.local/state/arbora`, and the Genesis seed paths and environment profiles point at `~/.config/arbora` and the `meristem` data directory.

### Fixed
- Versioned data migrations run at serving start under the process authority and are recorded as `DataMigration` nodes. Migration `0001-action-decision-node-type-label` labels the `ActionDecision` rows written type-only before 0.1.8.dev5, so the action-policy ledger reads again (ARBORA-HOST-R033.2).
- An unreadable action-policy ledger is denied as `ledger_unreadable` with its cause, never as "rate limit exceeded"; the connector-pack provisioning refusal carries the policy reason (ARBORA-FLEET-R015.3).
- One label model (`record_body`) for every served-engine write; a persisted-shape gate ties writer changes to a migration (ARBORA-HOST-R033.1, ARBORA-HOST-R033.3).

## [0.1.8.dev6] - 2026-10-29

- Fleet catalog engine reads run under the Arbora process authority, so operator callers see the admitted fleet and `fleet.call` resolves its tools (ARBORA-FLEET-R026.2).
- Depends on medulla, meristem and scion-sdk 0.5.9 in place of epistemic-graph, agent-utilities and agent-connector-sdk.

## [0.1.8.dev5] - 2026-10-25

## [0.1.8.dev4] - 2026-10-24

Fixes the gaps found by the 0.1.8.dev3 validation run.

- Requires agent-connector-sdk 0.5.5, which declares edges on declared sources (ARBORA-FLEET-R041.15).
- A destructive `fleet.call` under a confirmed plan answers the connector's confirmation request, so the delete runs. An unconfirmed call still fails closed (ARBORA-FLEET-R015.2).
- `agents.plan_run` has a replay owner and reaches dispatch. Its step keys `plan-step:<record>:<task>:<idx>` derive from the run's own idempotency key, so a replay repeats no step effect (ARBORA-RELEASE-R003.4.11).
- `fleet.packs.status` reads a connector's engine pack status as a read (ARBORA-FLEET-R041.16).
- Explicit onboarding reads the `connector://resources` declaration and `ontology://` bodies from the SDK 0.5 pack archive. Connectors then register their ingest sources and access contracts (ARBORA-FLEET-R041.15).
- `ask` routes plain data, catalog, lineage and ownership questions to the engine planner. A raw engine method or an op that shares one word no longer wins (ARBORA-FLEET-R014.3).
- A relay read re-establishes a child session after the child restarts. The child supervisor keeps reconnecting after its first generation dies (ARBORA-FLEET-R040.2).
- A failed ingest job names its cause on the receipt, the job status and the log. A child result may be one SSE event of up to 8 MiB (ARBORA-OPS-R020.6.8).
- An `INVALID_ARGUMENT` refusal names its fields, and an engine refusal names its reason code. A prompt called without its arguments gets a refusal that names them, not `INTERNAL` (ARBORA-OPS-R009.4).

## [0.1.8.dev3] - 2026-10-24

Pre-release for the validation deploy.

- Requires epistemic-graph 2.27.6 (and its server 2.27.6), agent-connector-sdk 0.5.4 and, for the `webui` extra, agent-webui 2.8.2.
- `agents.plan_run` steps call named governed fleet tools through the invoke pipeline, with the idempotency key `plan-step:<record>:<task>:<idx>` (ARBORA-RELEASE-R003.4.5).
- The version configuration parses `.devN` pre-release versions, so the documented bump command works on a pre-release.
- Requires the agent-connector-sdk connector resources cutover. Onboarding reads the connector resources declaration over MCP. Change detection uses content digests and `list_changed`. No manifest file is read (ARBORA-FLEET-R041.11, ARBORA-FLEET-R041.13, ARBORA-FLEET-R041.14).

## [0.1.8.dev2] - 2026-10-23

- `fleet.prompts.list`, `fleet.prompts.get`, `fleet.resources.list` and `fleet.resources.read` work on the served gateway (ARBORA-FLEET-R040.1).
- Request verbs such as "show" no longer route a question to an op (ARBORA-FLEET-R014.2).
- Pack capture paces its requests under the connector rate limit, so a large pack onboards (ARBORA-FLEET-R041.12).

## [0.1.8.dev1] - 2026-10-23

Pre-release for acceptance on the fresh-store `platform/arbora-prerelease` Deployment.

- Requires epistemic-graph 2.27.5. Connector sources declared by a connector list, sync through the SDK sync runner as a WorkItem, and land in EG (ARBORA-OPS-R020.6).
- `ask` no longer routes a plain question to an operation that needs a missing parameter. It falls back to the engine natural-language planner (ARBORA-FLEET-R014.1).
- Catalog, lineage and source questions route through `ask` to the engine planner, and a governed source executor serves virtual-graph reads (#432).
- A connector onboards when its MCP handshake completes (#433).
- Object-valued arguments to a governed `fleet.call` write no longer end INDETERMINATE (#434).

## [0.1.7] - 2026-10-18

- Creates unique records through the atomic create operation and refuses bindings that only support upsert.
- Preserves answered inbox state, retry attempts, and original timestamps when inbound messages are replayed.
- Corrects the inbox node-label call shape. The older-store producer refusal remains under investigation; this release does not qualify fleet onboarding.

## [0.1.6] - 2026-10-18

- Corrects A2A bearer refusal and unsupported-stream responses, and preserves process authority across supported token renewal.
- Updates served HTTP client operations, memory operations, channel adapters, and alert intake.
- Bundles the local model fixture for wheel qualification and requires the published TUI client for headless acceptance.
- Repairs the release version configuration and preserves package-index dependency installation.

## [0.1.5] - 2026-10-18
- Fixed `fleet.call` from an MCP client sending a child no arguments, and a
  read-only child tool resolving as a write. The served catalog took a tool's
  input schema and annotations only from a live child probe, and the served
  path never probed. A `fleet.call`, or a `fleet.catalog.search` naming up to
  8 servers, now probes each named server once (cached, retried after 60 s).
  An unfiltered search still never sweeps the fleet.
- `fleet.call` no longer requires `idempotency_key` for a read-only child tool
  (`readOnlyHint`). For a write without a key, the key is derived from the
  verified caller, the target tool, the arguments and the MCP request
  (`mcp:<session>:<request id>`), so a retry of the same request dedupes.
- Fixed agent runs on the fleet agent running without tools and recording no
  RunEvents. The served `find`/`act` verbs are now native run tools that call
  the same invocation pipeline under the run's session. The run takes the A2A
  task id as its run id. The run event sink writes each event as a `RunEvent`
  node through the served engine's `AddNode`, plus one `tool_call` event per
  verb call that names the op and connector tool. A completed task carries
  the agent's reply as its `artifacts`.
- The Agent Card is also served at `/a2a/.well-known/agent-card.json`, next to
  the endpoint it names, so A2A clients learn `streaming: false` and use
  `message/send`.

## [0.1.4] - 2026-10-18
- Fixed `fleet.catalog.search` (INTERNAL) and `fleet.call` (UNAVAILABLE) on the
  HTTP operation API: the fleet gateway now runs each catalog read and call
  under the verified caller's own session.
- Fixed `fleet.call` to deployed connectors outliving the caller's 300 s bearer
  (INDETERMINATE). Admission re-read the whole fleet catalog (about 1 min on a
  65-server fleet) several times per call. A verified snapshot is now reused
  per read context for `ARBORA_FLEET_CATALOG_TTL_S` (default 120 s), and
  component content is reused per immutable revision.
- `ask eg.graph.NodeCount` (and any EG method without params) accepts `{}`;
  it was INVALID_ARGUMENT.
- A2A `message/send` and `agents.run` without `metadata.agent_name` run on the
  fleet's default agent (`arbora`) instead of answering 503.
- The agent run model comes from the configured `CHAT_MODELS` registry when no
  `MODEL_REGISTRY_PATH` is set.
- The served engine exposes EG WorkItems (`work_items`) on `__control__`;
  WorkItem submits send EG's typed request context and non-empty digests. A
  failed A2A run logs its cause.

## [0.1.3] - 2026-10-17
- Fixed every Keycloak service-account (client-credentials) caller being refused
  UNAUTHENTICATED on MCP verbs, the ops API and A2A. Such a token carries no
  email. The served boundary mints it as an `automated_service` actor. The
  caller projection accepted only `human`/`service`. `automated_service` is
  now a service principal. A refused caller logs its static reason.
- Fixed readiness never passing: the engine probe sent `RETURN 1 AS ok`, which
  the engine's Cypher refuses (it requires MATCH). It now sends
  `MATCH (n) RETURN n LIMIT 1`.
- `/health/ready` names the essential checks holding it down (`failing`).
- The real-boot gate now asserts `/health/ready` is 200 against the real engine.
  It also asserts that a no-email service token is an accepted caller.

## [0.1.2] - 2026-10-17
- Fixed readiness never passing on a 0.1 deployment. A fleet supervisory
  authority this release does not serve (goal rehydration, the control store,
  the worker registry: `AuthorityNotServed`) now reports `not_served` in the
  `fleet.health.v1` evidence and the readiness body. It is not applicable and
  does not gate readiness; only served authorities do. Autoscaling and
  convergence still require every authority served and healthy.
- A fleet refusal now carries the child's own error text as a redacted,
  bounded `detail` next to `server` and `tool`, so `CHILD_REFUSED` is no longer
  opaque. The real-boot gate inherits only process environment, never an
  Arbora setting leaked by an earlier test.

## [0.1.1] - 2026-10-17
- Fixed the installed wheel crash-looping at startup with `EgContractError:
  cannot read EG exclusions`: `api/registry/eg_exclusions.yaml` and
  `api/policy/default_policy.json` were not package data. Package data is now
  one extension glob per data type (`json`, `yaml`, `yml`, `toml`, `ttl`, `md`,
  `txt`) across the whole package.
- Added a release-blocking wheel boot gate. The build fails when a non-Python
  file tracked under `arbora/` is missing from the wheel. Every install leg then
  installs the wheel into a clean venv outside the source tree. There it builds
  the full operation registry and runs `arbora --help` and `--version`. The Linux x64 leg
  also runs `tests/test_real_boot.py` against the installed wheel and the
  `epistemic-graph-server` binary.
- `ArboraConfig()` accepts JSON-encoded list and mapping text from the
  environment and `config.json`. List settings still accept comma-separated
  text. Empty text is an unset setting.
- `setup-config generate` and `save_config_item` write only flat scalars:
  lists and mappings are JSON text, and unset settings are empty text. The
  runtime loader accepts every generated profile.
- `setup-config doctor --config` reads the document with the runtime loader and
  builds the configuration the way the runtime does, so the two agree.
- `arbora --help`, `-h` and `--version` answer without loading configuration or
  resolving secret references.

## [0.1.0] - 2026-09-12
- Renamed the project to **arbora** before its first release, because PyPI
  refuses `graph-os` as too similar to an existing project. The distribution,
  import package (`arbora`), console scripts (`arbora`, `arbora-*`),
  environment variables (`ARBORA_*`), configuration directory
  (`~/.config/arbora`) and repository URL change. For one release, a
  configuration file found only under the previous project's directory (or its
  `*_CONFIG_DIR` variable) and then under `~/.config/agent-utilities` is still
  read, with one deprecation warning. Spec IDs (`GRAPHOS-*`) are unchanged.
  The rename is reproducible with `scripts/rename_to_arbora.py`.
- Initial scaffold: package skeleton (`graph_os` + the six RF-ADR-009 §2 Phase 5
  submodules), `graph-os` console entry point, pre-commit suite, GitHub Actions
  release workflow, GitHub Pages skeleton (RF-ADR-009 W0 / W5-prep).
