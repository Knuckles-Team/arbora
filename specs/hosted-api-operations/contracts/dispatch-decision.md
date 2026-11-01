# Dispatch decision

Seam between `ARBORA-OPS` and Medulla. Owner rows: `MEDULLA-SDLC-R015` in the medulla repository and `ARBORA-OPS-R896.3` and `ARBORA-OPS-R896.4` in this repository.

Provider: medulla MEDULLA-SDLC-R015
Consumer: arbora ARBORA-OPS-R896.3

## Wire shape

Arbora starts a run from the engine dispatch decision and observes it. The request carries the task and the decision identifier. The response carries the run identifier and its state. `MERISTEM-SCM` holds the task-run row.

## Refusal codes

Arbora forwards the engine's stable refusal code unchanged. It never replaces the code with empty text. An unreachable engine returns `ENGINE_UNAVAILABLE` and writes no state.

## Owner rows

- Medulla: `MEDULLA-SDLC-R015`.
- Arbora: `ARBORA-OPS-R896.3` and `ARBORA-OPS-R896.4`.
