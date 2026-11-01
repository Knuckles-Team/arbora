# Project mode

Seam between `ARBORA-OPS` and Medulla. Owner rows: `MEDULLA-SDLC-R012.8` in the medulla repository and `ARBORA-OPS-R896.2` in this repository.

Provider: medulla MEDULLA-SDLC-R012.8
Consumer: arbora ARBORA-OPS-R896.2

## Wire shape

Arbora serves the project mode that the engine reports. The request names the project. The response carries the mode and the rows it governs.

## Refusal codes

Arbora forwards the engine's stable refusal code unchanged. It never replaces the code with empty text. An unreachable engine returns `ENGINE_UNAVAILABLE` and writes no state.

## Owner rows

- Medulla: `MEDULLA-SDLC-R012.8`.
- Arbora: `ARBORA-OPS-R896.2`.
