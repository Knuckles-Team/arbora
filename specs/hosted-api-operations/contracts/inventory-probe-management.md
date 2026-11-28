# Inventory management probe

Seam between the fan-manager inventory tool and the arbora inventory build.

Provider: fan-manager CONN-FAN-MANAGER-R006.2
Consumer: arbora ARBORA-OPS-R959.4

## Wire shape

Arbora calls `act(op="fleet.call", params={"server":"fan-manager-mcp","tool":"fan_manager_ipmi_inventory","arguments":{"host":<management address>,"credential_ref":<reference>}})`.

```json
{"schema":"inventory-probe-management/v1","reachable":true,
 "controller":{"id":null,"address":null,"firmware_version":null,"model":null},
 "power":"on","thermal_critical":false}
```

`controller.id` is the lower-case colon form of the controller MAC address. `power` is `on`, `off` or `unknown`. `thermal_critical` is true when any sensor reports a critical or non-recoverable status. An unreachable controller returns `{"schema":"inventory-probe-management/v1","reachable":false}`.

## Refusal codes

- `PROBE_SHAPE_INVALID`: the object fails the model.
- `HOST_UNREACHABLE`: `reachable` is false or the call failed in transport.
- `CONNECTOR_MISSING`: `fan-manager-mcp` is not in the fleet catalog.

## Authoritative types

- `arbora/inventory/probes.py`: `ManagementProbe`.
- `fan_manager/ipmi_api.py`: the producer, in fan-manager.

## Test vectors

- `tests/inventory/test_probes.py::test_probes_call_the_named_tools_with_the_named_arguments`.
- `tests/test_ipmi_inventory.py::test_inventory_payload_matches_the_probe_schema` in fan-manager.
