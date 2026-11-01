# Harness host-facing module set

Import-boundary decision of `ARBORA-HOST-R007`; the harness repository owns the module list and Arbora owns the fence.

Provider: meristem MERISTEM-BOUNDARY-R057
Consumer: arbora ARBORA-HOST-R007

## Decision

The harness package `meristem` declares the modules a host may import in `meristem.PUBLIC_HOST_MODULES`. Arbora imports only those modules or their submodules and imports nothing from the retired `agent_utilities` name. The harness side of the seam is the `arbora.md` contract under `specs/run/contracts/` in the meristem repository.

## Wire shape

No runtime record crosses this seam. The surface is the module list: `meristem.host_ports`, `meristem.runtime`, `meristem.graph`, `meristem.rlm`, `meristem.harness.hosted_run`, `meristem.harness.plan` and `meristem.harness.conversation_history`.

## Refusal codes

None. An import of a module outside the list, or of `agent_utilities`, fails the fence test described by `ARBORA-HOST-R007.2.69`.

## Authoritative types

- `meristem.PUBLIC_HOST_MODULES`: the allowed module set.

## Test vectors

- `tests/test_package_layout.py::test_arbora_imports_no_au_serving_packages`
- Planned: the fence test named by `ARBORA-HOST-R007.2.69` in `test-spec.md`.
