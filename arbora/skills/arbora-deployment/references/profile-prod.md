# Profile `prod` (prod, kubernetes, namespace `platform`)

profile-sha256: f0640b0d3791ac0eb716a32b4b9dadc890bccbf526181ab6c50f450fe08608aa

Support status: supported. The target cell of this profile has an adapter; a
cell without one is listed as `unsupported_target` and never as supported.

## Prerequisites

- A kubernetes cluster context with permission to create resources in namespace `platform`.
- `kubectl` and, for chart installs, `helm`, at versions the cluster supports.
- A private registry reachable from the cluster, and a pinned image digest.
- A secret store holding the references named in `deploy/environments/prod.yaml`.

## Install source

### Package or pinned image

Install the published package (`python -m pip install arbora`) for the
command-line tools, and deploy the pinned image digest that carries the matching
engine. Never deploy a floating tag to a production profile.

### Editable development install

From a source checkout run `scripts/bootstrap.sh`, which installs the locked
environment for development only. A development install is never the deployed
artifact.

## Authority setup

Create the secret references the profile names (`setup-config environments show prod`
lists them). Secrets are references, never values. Register the process
principal with the identity provider, then check the profile with
`setup-config environments validate prod`.

## Start

Render and apply the manifests for the profile, then wait for the rollout:
`kubectl -n platform rollout status deployment/arbora`.

## Probes

Check readiness at `/health/ready`, then run `setup-config doctor --profile prod`
and one MCP `initialize` call against the served endpoint.

## Logs

Read container logs with `kubectl -n platform logs deployment/arbora`. Logs carry key
names and outcomes only, never a credential.

## Stop

Scale the deployment to zero with `kubectl -n platform scale deployment/arbora --replicas=0`.
Data volumes are retained; a stop never deletes the store.
