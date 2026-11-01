# Spec artifact graph reads

Seam between `ARBORA-OPS` and Medulla. Owner rows: `MEDULLA-SDLC-R014.6` and `MEDULLA-SDLC-R014.7` in the medulla repository and `ARBORA-OPS-R896.1` in this repository.

Provider: medulla MEDULLA-SDLC-R014.6
Consumer: arbora ARBORA-OPS-R896.1

## Wire shape

Arbora runs the competency queries by name through the served engine read. The request carries the query name and the project scope. The response carries the artifact nodes and their edges. The medulla contract is `specs/development-lifecycle/contracts/spec-artifact-graph.md`.

## Refusal codes

Arbora forwards the engine's stable refusal code unchanged. It never replaces the code with empty text. An unreachable engine returns `ENGINE_UNAVAILABLE` and writes no state.

## Owner rows

- Medulla: `MEDULLA-SDLC-R014.6` and `MEDULLA-SDLC-R014.7`.
- Arbora: `ARBORA-OPS-R896.1`.
