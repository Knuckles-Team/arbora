# Changelog

## [Unreleased]

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
