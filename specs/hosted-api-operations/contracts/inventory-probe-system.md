# Inventory system probe

Seam between the tunnel-manager hardware tool and the arbora inventory build.

Provider: tunnel-manager CONN-TUNNEL-MANAGER-INVENTORY-R008.2
Consumer: arbora ARBORA-OPS-R959.4

## Wire shape

Arbora calls `act(op="fleet.call", params={"server":"tunnel-manager-mcp","tool":"tm_system_get_hardware","arguments":{"remote_host":<first address>,"credential_ref":<reference>}})`. The tool result is one object, or an object that holds it under `data`.

```json
{"schema":"inventory-probe-system/v1","reachable":true,
 "system":{"uuid":null,"serial":null,"manufacturer":null,"model":null},
 "os":{"name":"","version":"","kernel":"","architecture":""},
 "processors":[{"socket":0,"model":"","cores":0,"threads":0}],
 "memory_modules":[{"slot":"","size_bytes":0,"serial":null,"model":null}],
 "memory_total_bytes":0,
 "storage_devices":[{"name":"","model":null,"serial":null,"size_bytes":0,"medium":"ssd"}],
 "network_interfaces":[{"name":"","mac":null,"speed_megabits":null,"link":"up"}],
 "network_bonds":[{"name":"","mode":"","members":[""]}],
 "accelerators":[{"kind":"gpu","model":"","memory_bytes":null,"serial":null,"pci_address":null}]}
```

`medium` is `hdd`, `ssd`, `nvme` or `other`. `link` is `up`, `down` or `unknown`. `kind` is `gpu`, `fpga`, `tpu`, `dpu` or `other`. A host that cannot be reached returns `{"schema":"inventory-probe-system/v1","reachable":false}`.

## Refusal codes

- `PROBE_SHAPE_INVALID`: the object fails the model. Arbora refuses the build.
- `HOST_UNREACHABLE`: `reachable` is false or the call failed in transport.
- `CONNECTOR_MISSING`: `tunnel-manager-mcp` is not in the fleet catalog.

## Authoritative types

- `arbora/inventory/probes.py`: `SystemProbe`.
- `tunnel_manager/system_intelligence.py`: the producer, in tunnel-manager.

## Test vectors

- `tests/inventory/test_probes.py::test_probes_call_the_named_tools_with_the_named_arguments`.
- `tests/test_system_hardware.py::test_hardware_payload_matches_the_probe_schema` in tunnel-manager.
