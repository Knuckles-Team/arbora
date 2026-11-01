# ARBORA-DEPLOY data model

## ComponentDeploymentChoice

Model in `arbora/deployment/component_choices.py` (new, `ARBORA-DEPLOY-R013.2`). It records the operator's decision for one component of the deployment manifest.

| Field | Type | Meaning |
|---|---|---|
| `component_id` | `str` | Component id from the deployment manifest. |
| `action` | `str` | `deploy` or `reuse`. |
| `endpoint_ref` | `str` or none | Reference to the endpoint of a reused component; required when `action` is `reuse`. |
| `profile` | `str` | Name of the target profile under `deploy/environments/`. |
| `confirmed` | `bool` | True once the operator confirmed the inventory. |

The canonical SHA-256 of the confirmed inventory is `choices_digest`. No credential is stored in any field.

## EngineEndpoint and EngineProbe

Types in `arbora/mcp_server/composition.py` (existing module, new types, `ARBORA-DEPLOY-R018.1` and `ARBORA-DEPLOY-R018.2`). `EngineEndpoint` holds `kind` (`unix` or `tcp`) and `address`; `parse_engine_endpoint` builds it. `EngineProbe` holds `state` (`running` or `absent`) from one engine `Health` call. The probe starts nothing.

## Engine lock record

The lock file `engine-<sha8>.lock` under the user runtime directory (existing, `arbora/deployment/engine_lock.py`, `ARBORA-DEPLOY-R600.5`) holds `pid`, `acquired_at` and an opaque 16-hex `transport_digest`. It holds no hostname and no path. The marker `engine-<sha8>.owner` holds the pid of the engine this process started.

## Plan digests

The operation `deploy.placement.plan` returns four digests that `deploy.placement.apply` must reproduce (`ARBORA-DEPLOY-R013.5`): `plan_digest` over the engine graph, `choices_digest` over the confirmed inventory, `config_digest` over the resolved configuration and `artifact_digest` over the selected artifact. The engine owns the decision and graph; Arbora stores none of them.
