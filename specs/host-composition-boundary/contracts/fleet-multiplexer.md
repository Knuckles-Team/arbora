# Fleet multiplexer ownership

Ownership decision of `ARBORA-HOST-R006`; the row keeps the layout gate.

Provider: arbora ARBORA-HOST-R006
Consumer: meristem MERISTEM-RUN-R016

## Decision

`arbora/fleet/` holds the only multiplexer (`arbora/fleet/multiplexer.py::MCPMultiplexer`). Host bootstrap `arbora/mcp_server/server.py::mcp_server` builds it and mounts the hosted modules. The OAuth broker (`arbora/mcp_server/backing/remote_oauth_broker.py`), task extension, principal, harness host (`arbora/mcp_server/harness_host.py`), middleware and skill coverage stay in `arbora/mcp_server/backing/`. Meristem defines no multiplexer and arbora imports none from it.

## Wire shape

No runtime record crosses this seam. The surface is the class set: any class whose name holds `multiplexer` is defined under `arbora/fleet/` only.

## Refusal codes

None. A multiplexer class outside `arbora/fleet/`, or an import of a harness multiplexer by a serving package, fails the layout test.

## Authoritative types

- `arbora/fleet/multiplexer.py`: `MCPMultiplexer`.

## Test vectors

- `tests/architecture/test_fleet_sole_multiplexer.py`
- `tests/test_package_layout.py::test_serving_packages_do_not_import_legacy_au_multiplexer`
- `tests/architecture/test_fleet_sole_multiplexer.py::test_no_multiplexer_class_is_defined_outside_arbora_fleet`
