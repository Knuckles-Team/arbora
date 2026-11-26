# Host service and log provider

Seam between `ARBORA-OPS` and the connector SDK. Owner row: `SCION-CENTRALIZED-CAPABILITIES-R056` in the scion-sdk repository. Arbora defines no service or log interface of its own; the earlier Arbora row is retired.

Provider: scion-sdk SCION-CENTRALIZED-CAPABILITIES-R056
Consumer: arbora ARBORA-OPS-R014.1

## Wire shape

| Operation | Provider call | Answer |
|---|---|---|
| list services | `HostServiceProvider.list_services` | typed service rows |
| service status | `HostServiceProvider.service_status` | one typed status |
| start, stop, restart | `HostServiceProvider.start_service`, `stop_service`, `restart_service` | typed outcome |
| read logs | `HostServiceProvider.read_logs(service, since, limit)` | redacted log lines |

An operating system with no adapter refuses `UNSUPPORTED_PLATFORM`.

## Refusal codes

| Code | Meaning |
|---|---|
| `UNSUPPORTED_PLATFORM` | The operating system has no adapter for the operation. |

## Owner rows

- Provider: `SCION-CENTRALIZED-CAPABILITIES-R056` (scion-sdk).
- Consumer: `ARBORA-OPS-R014.1` (retired; the host-management operations consume the SDK interface once a release serves it).

## Authoritative types

The provider owns `HostServiceProvider`, the per-operation result models and the closed `HostServiceError` category set in scion-sdk. Arbora declares none of them.

## Test vectors

- scion-sdk: `tests/test_host_services.py::test_each_platform_maps_every_operation_and_refuses_the_unmapped`.
