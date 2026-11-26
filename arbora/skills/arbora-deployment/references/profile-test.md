# Profile `test` (non-prod, kubernetes, namespace `platform-test`)

profile-sha256: b460ddb42146b5d6fb982363621051bdabf493ff5f35d4ca057aa5d7f549da0a

Support status: supported. The target cell of this profile has an adapter; a
cell without one is listed as `unsupported_target` and never as supported.

## Prerequisites

- A kubernetes cluster context with permission to create resources in namespace `platform-test`.
- `kubectl` and, for chart installs, `helm`, at versions the cluster supports.
- A private registry reachable from the cluster, and a pinned image digest.
- A secret store holding the references named in `deploy/environments/test.yaml`.

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

Create the secret references the profile names (`setup-config environments show test`
lists them). Secrets are references, never values. Register the process
principal with the identity provider, then check the profile with
`setup-config environments validate test`.

## Start

Render and apply the manifests for the profile, then wait for the rollout:
`kubectl -n platform-test rollout status deployment/arbora`.

## Probes

Check readiness at `/health/ready`, then run `setup-config doctor --profile test`
and one MCP `initialize` call against the served endpoint.

## Logs

Read container logs with `kubectl -n platform-test logs deployment/arbora`. Logs carry key
names and outcomes only, never a credential.

## Stop

Scale the deployment to zero with `kubectl -n platform-test scale deployment/arbora --replicas=0`.
Data volumes are retained; a stop never deletes the store.
