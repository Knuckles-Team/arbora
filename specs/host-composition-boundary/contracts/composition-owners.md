# Composition owners

Seam between the Arbora served host and the three repositories it composes. This contract holds the ownership decision of `ARBORA-HOST-R001`; the row keeps the census gate.

Provider: medulla MEDULLA-DECISION-ENGINE-R134
Provider: scion-sdk SCION-CONNECTOR-CONTROL-R067
Provider: meristem MERISTEM-RUN-R008
Consumer: arbora ARBORA-HOST-R001

## Decision

| Owner | Owns | Arbora reaches it through |
|---|---|---|
| medulla | Ontology, SHACL, durable records, decisions | The generated engine client and served `eg.*` operations |
| scion-sdk | Connector transport, manifests, certification, governed HTTP and credentials | `scion_sdk.mcp`, `.manifest`, `.certify`, `.contracts` |
| meristem | The in-run harness | `meristem.PUBLIC_HOST_MODULES` (`MERISTEM-RUN-R008`) and the host ports |
| overstory (web UI) | Browser presentation | Arbora hosts it in `arbora/webui_host/` and consumes no internal package |

Arbora owns the served runtime: `arbora/mcp_server`, `arbora/fleet`, `arbora/gateway`, `arbora/control_plane`, `arbora/webui_host`, `arbora/a2a`, `arbora/api`, `arbora/messaging`.

## Wire shape

The seam is an import boundary. `architecture/component-registry-boundary-seeds.yml` (`ARBORA-HOST-R012`) lists each owner as `{owner_repository, owned_module_paths, public_import_surface}`. `arbora/architecture/boundary_census.py::ImportBoundaryCensus.census` walks every file under `arbora/` and returns one `BoundaryViolation{source_file, imported, owner_repository}` for each import that an owner owns (`BoundaryOwner.owns`) but its public surface does not cover (`BoundaryOwner.covers`).

## Refusal codes

The census has no wire code. A violation fails `pytest tests/architecture` with the file, the imported dotted name and the owner repository.

## Authoritative types

- `arbora/architecture/boundary_census.py`: `BoundaryOwner`, `BoundaryViolation`, `ImportBoundaryCensus`.
- `architecture/component-registry-boundary-seeds.yml`: the owner seeds.

## Test vectors

- `tests/architecture/test_boundary_census.py::test_census_refuses_an_import_outside_the_public_surface`
- `tests/architecture/test_host_import_boundaries.py::test_webui_host_imports_only_public_runtime_surfaces`
