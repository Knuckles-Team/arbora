# Inventory orchestrator membership probe

Seam between the container-manager membership actions and the arbora inventory build.

Provider: container-manager-mcp CONN-CONTAINER-MANAGER-MCP-R025.2
Consumer: arbora ARBORA-OPS-R959.4

## Wire shape

Arbora calls `act(op="fleet.call", params={"server":"container-manager-mcp","tool":"cm_swarm_operations","arguments":{"action":"membership"}})`, then the same call with tool `cm_k8s_cluster`.

```json
{"schema":"inventory-probe-membership/v1","orchestrator":"swarm","configured":true,
 "nodes":[{"name":"","addresses":[""],"system_uuid":null,"ready":true,
           "availability":"active","workload_count":0,
           "disk_pressure":false,"memory_pressure":false}]}
```

`orchestrator` is `swarm` or `kubernetes`. `availability` is `active`, `drain`, `pause` or `cordoned`. `workload_count` counts the tasks or pods scheduled on the node, not counting node-level daemons. An unconfigured orchestrator returns `configured` false and no nodes.

## Refusal codes

- `PROBE_SHAPE_INVALID`: the object fails the model.
- `CONNECTOR_MISSING`: `container-manager-mcp` is not in the fleet catalog.

## Authoritative types

- `arbora/inventory/probes.py`: `MembershipProbe`.
- `container_manager_mcp/mcp_server.py`: the producer, in container-manager-mcp.

## Test vectors

- `tests/inventory/test_probes.py::test_probes_call_the_named_tools_with_the_named_arguments`.
- `tests/test_cluster_membership.py::test_membership_payload_matches_the_probe_schema` in container-manager-mcp.
