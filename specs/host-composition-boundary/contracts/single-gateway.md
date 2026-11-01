# Single gateway

Package-layout decision of `ARBORA-HOST-R004`; the row keeps the layout gate.

Provider: arbora ARBORA-HOST-R004
Consumer: arbora ARBORA-DEPLOY-R006
Consumer: arbora ARBORA-DEPLOY-R007.3

## Decision

Arbora holds one gateway implementation, `arbora/gateway/`. The harness keeps no duplicate gateway package. The `arbora-daemon` console script and the `gateway-widgets` package extra do not exist; widgets reach the fleet only through the served fleet tools.

## Wire shape

No runtime record crosses this seam. The manifest is the surface: `pyproject.toml` `[project.scripts]` has no `arbora-daemon` key, `[project.optional-dependencies]` has no `gateway-widgets` key, and `arbora.gateway.daemon` has no `main`.

## Refusal codes

None. A second top-level package whose name holds `gateway`, a retired script or a retired extra fails the layout test.

## Authoritative types

- `arbora/gateway/__init__.py`: the sole gateway package.
- `pyproject.toml`: the published scripts and extras.

## Test vectors

- `tests/architecture/test_single_gateway_implementation.py::test_single_gateway_and_retired_script_and_extra_absent`
- `tests/architecture/test_single_gateway_implementation.py::test_base_dependencies_pin_connector_sdk_floor_without_widgets_extra`
