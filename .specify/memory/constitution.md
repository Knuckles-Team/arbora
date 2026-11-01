# Arbora constitution

This repository inherits the [organization constitution](https://github.com/Knuckles-Team/pipelines/blob/main/.specify/memory/constitution.md) version 1.3.0. Every article of that constitution applies here. This file adds only the rules and limits of this repository.

**Inherits**: organization constitution v1.3.0 | **Version**: 1.0.0 | **Ratified**: 2026-10-31 | **Last amended**: 2026-10-31

## Organization standards

- [Spec standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/spec-standard.md): one owning spec per capability, computed states, `Spec:` trailers.
- [Ecosystem boundaries](https://github.com/Knuckles-Team/pipelines/blob/main/reference/ecosystem-boundaries.md): which repository owns which requirement.
- [Wording standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/ste.md): sentences of 20 words or fewer, with `must`, `should`, or `may`.

## Repository articles

### R-I. One door, no policy table

Arbora serves every operation through its registry. It owns admission, auth, approvals, the MCP server, A2A serving and the platform configuration. It holds no policy table and no fact store.

**Enforced by:** [Ecosystem boundaries](https://github.com/Knuckles-Team/pipelines/blob/main/reference/ecosystem-boundaries.md), arbora row and forbidden-import matrix.

**Plan check:** Does the change serve an operation from the registry, with every decision read from the engine?

### R-II. Every module is reachable from a production caller

A module is imported by a served path. An orphan or unreachable module fails the gate.

**Enforced by:** Pre-commit hooks `check-orphan-modules` and `check-unreachable-modules` (`scripts/check_wiring.py`).

**Plan check:** Does a served entry point reach each new module?

### R-III. Public specs and core skills are complete and self-contained

Public specs hold no private reference. The core skills match the served operations.

**Enforced by:** Pre-commit hooks `check-public-specs` (`scripts/check_public_specs.py`) and `check-core-skills` (`scripts/check_core_skills.py`).

**Plan check:** Do the new specs and skills pass both checks?

## Repository limits

This repository sets no numeric limit of its own. It follows the organization limits table.

Amend this file in a reviewed change. State the impact in the pull request. When the organization constitution changes its version, update the **Inherits** line in the same change.
