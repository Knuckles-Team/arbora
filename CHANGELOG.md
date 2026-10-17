# Changelog

## [Unreleased]

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
