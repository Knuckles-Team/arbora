# Proposal review

Seam between `ARBORA-OPS` and Medulla. Owner rows: `MEDULLA-SDLC-R017` in the medulla repository and `ARBORA-OPS-R896.5` in this repository.

Provider: medulla MEDULLA-SDLC-R017
Consumer: arbora ARBORA-OPS-R896.5

## Wire shape

Arbora lists and reviews the proposals that the engine holds. The request carries the proposal identifier and the reviewer decision. The response carries the updated proposal state. The medulla contract is `specs/development-lifecycle/contracts/proposal-review.md`.

## Refusal codes

Arbora forwards the engine's stable refusal code unchanged. It never replaces the code with empty text. An unreachable engine returns `ENGINE_UNAVAILABLE` and writes no state.

## Owner rows

- Medulla: `MEDULLA-SDLC-R017`.
- Arbora: `ARBORA-OPS-R896.5`.
