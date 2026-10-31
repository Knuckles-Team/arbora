# Deployment and configuration

Arbora ships a local MCP server, an authenticated HTTP transport, configuration
and diagnostic tools, and guarded production operations. Choose a profile,
validate it, and run the release canary before changing live traffic.

## Install

Arbora supports Python 3.12 through 3.14.

```bash
python -m pip install arbora
```

Add the optional Overstory host integration when the same process will serve
the UI:

```bash
python -m pip install "arbora[webui]"
```

## Generate configuration

The configuration tool supports three deployment profiles:

| Profile | Intended use |
|---|---|
| `tiny` | Local evaluation and a minimal single-process environment |
| `single-node-prod` | A production host with externalized persistence and identity |
| `enterprise` | A managed multi-service deployment with production policy controls |

Generate and validate a profile:

```bash
setup-config generate --profile tiny
setup-config doctor --profile tiny
```

The default output follows the XDG configuration directories. Generated
configuration must contain secret references such as `vault://` or
`engine://__secrets__`; do not place plaintext credentials in configuration,
examples, command history, or source control.

Inspect the complete option reference without writing configuration:

```bash
setup-config reference
```

## Container and Kubernetes deployment

The source tree ships three deployment artifacts. Each consumes an
operator-built image that carries Arbora and the epistemic-graph engine
binary at matching releases, pinned by digest.

| Artifact | Use |
|---|---|
| `deploy/compose/compose.yaml` | One host with Docker Compose or Podman: a single Arbora service that supervises its engine, one data volume, ports published on loopback |
| `deploy/swarm/stack.yml` | Docker Swarm: the same single Arbora service as Compose, one replica with stop-first updates, pinned to the node that holds the data, secrets as Swarm secrets, overlay network |
| `deploy/helm/arbora` | Kubernetes: Arbora with its engine as a native sidecar (or child) in one pod, or a shared engine StatefulSet; namespaced RBAC, split API and web UI ingress, NetworkPolicy |

Validate before use:

```bash
docker compose -f deploy/compose/compose.yaml config --quiet
docker stack config -c deploy/swarm/stack.yml
helm lint deploy/helm/arbora --strict
helm template arbora deploy/helm/arbora --namespace <namespace> --values <values>
```

The engine store has exactly one writer: never scale the unified service, pod,
or Swarm service past one replica. The bundled operator skills `arbora-genesis`
(substrate) and `arbora-deployment` (installation, identity, verification) walk
through both the development (Compose) and production (Kubernetes/Helm)
profiles end to end, including the forced first administrator, default
authorization policy, secrets backend selection, and first-boot verification.

## Engine admission

A served Arbora process and its engine must agree on one verified request
context. Print the exact settings for a deployment:

```bash
setup-config engine-admission --principal <process principal> \
  --audience <audience> --tenant <tenant> --policy-version <policy version> \
  --allowed-private-host <connector host>
```

The engine needs these settings:

| Engine setting | Value |
|---|---|
| `GRAPH_SERVICE_AUTH_SECRET` | The shared HMAC secret; Arbora reads the same value |
| `EPISTEMIC_GRAPH_AUDIENCE` | Equal to the Arbora `AUTH_JWT_AUDIENCE` |
| `EPISTEMIC_GRAPH_TENANT` | The tenant of the Arbora process token |
| `EPISTEMIC_GRAPH_POLICY_VERSION` | Equal to the Arbora `KG_POLICY_VERSION` |
| `EPISTEMIC_GRAPH_SIGNER_KEYS_JSON` | One signer: the Arbora process principal, with `may_grant_system` true |
| `EPISTEMIC_GRAPH_CONNECTOR_PACK_IMPORTERS` | `*=principal:sha256:<SHA-256 of the process principal>` |

The Arbora process token must carry these roles: `kg:admin`,
`connector:catalog-attest`, `admin:connector-pack`, `agent:pack-control`,
`security:admin`, `mcp:discover`, `mcp:delegate`, `query:unified` and
`node:read`.

On a fresh store, do these steps in order before the first serve:

1. Bootstrap the process principal as the engine system identity. The process
   signs as itself with the `security:bootstrap` scope.
2. Run `arbora-production-ops provision-semantic-content --served-url <MCP URL>`.
3. Run `arbora-production-ops onboard-fleet --served-url <MCP URL>`. A fleet
   connector's pack import needs an `ACTION_POLICY_PATH` rule that admits it.

A fresh engine admits one principal only: the Arbora process principal. Each
other caller needs its own engine identity registration before its requests
pass engine authorization.

Arbora reaches a fleet connector over plain HTTP only on loopback, or on a
host that `MCP_HTTP_ALLOWED_PRIVATE_HOSTS` names. Set it in the Arbora
configuration when connectors run on private or cluster hosts:

| Arbora setting | Value |
|---|---|
| `MCP_HTTP_ALLOWED_PRIVATE_HOSTS` | The exact connector hostnames, comma-separated or as a JSON list, for example `connector-a.example,connector-b.example` |

Pass each host with `--allowed-private-host` and `setup-config
engine-admission` prints the value under `arbora_config`. A connector entry
can also name its own host in `allowed_private_hosts`.

`tests/test_real_boot.py` runs these steps against a real engine.

## Explicit local anonymous mode

Authenticated serving remains the default. For an isolated local instance,
`ARBORA_AUTH_MODE=none` selects true tokenless admission, not a generated
bootstrap JWT. Use a matching EG provider/server with explicit anonymous-mode
support and a new anonymous store. An existing authenticated store is not
eligible. Install a matching anonymous-capable EG client and server before enabling this
mode. The existing minimum dependency still serves authenticated deployments;
older clients receive `AnonymousClientUnavailableError` with an upgrade
instruction rather than a fallback. Older engines are not silently downgraded. The native build must include
`graph,server,security,redb,query,cypher,epistemic,blob,shacl` for official
provisioning and serving. Cypher supports the action-policy ledger, while blob
and SHACL support semantic-pack projection. A missing capability is an error,
not permission to bypass its policy or schema verification.

```bash
# In separate terminals, using the same isolated configuration and loopback:
# Explicitly admit only Arbora's own content pack, not unrelated connectors.
export EPISTEMIC_GRAPH_CONNECTOR_PACK_IMPORTERS="arbora=principal:sha256:2f183a4e64493af3f377f745eda502363cd3e7ef6e4d266d444758de0a85fcc8"
epistemic-graph-server --auth-mode none --tcp-only \
  --tcp-addr 127.0.0.1:19100 --persist-dir ./anonymous-store

export ARBORA_AUTH_MODE=none
export GRAPH_SERVICE_ENDPOINTS=tcp://127.0.0.1:19100
arbora-production-ops provision-semantic-content \
  --served-url http://127.0.0.1:18765/mcp
arbora-production-ops onboard-fleet \
  --served-url http://127.0.0.1:18765/mcp
arbora --transport streamable-http --host 127.0.0.1 --port 18765 --auth-type none
```

Do not configure JWT/OAuth credentials, engine signing secrets, static tokens,
or a reverse proxy for this mode. Arbora refuses nonliteral/nonloopback binds,
nonloopback request peers or Host values, and Origin, Cookie, or Authorization
headers. A startup warning and `X-Arbora-Auth-Mode: none` identify the unsecured
local instance. Changing modes requires a new serving process and a separately
provisioned store; settings reload cannot change authority on live connections.

Requests retain `authenticated=False`, the fixed principal `anonymous`, and
tenant `local`. The isolated instance grants its documented operation scopes
except approver scopes. It does not create human, service, MFA, delegation, or
verified-credential evidence. Existing operation policies, approval requirements,
PLAN seals, audit/effect journaling, and schema admission still apply. The
non-secret importer mapping above is an explicit local-instance policy binding
for Arbora's own semantic pack. Omission or a different connector remains
refused. It is not an authentication token or a wildcard approval. External
services such as OAuth-protected connectors and delegated Trino do not become
credential-free merely because the local graph is anonymous.

## Local MCP transport

Use `stdio` when an MCP client launches Arbora as a child process:

```bash
arbora --transport stdio
```

Configure `arbora --transport stdio` using the client's
standard MCP server configuration format.

## Network transport

Serve the streamable HTTP transport on a private listener with a real token
verifier. This example uses the JWT boundary:

```bash
AUTH_JWT_AUDIENCE=arbora KG_POLICY_VERSION=baseline-v1 \
  arbora --transport streamable-http \
  --host 127.0.0.1 --port 8000 \
  --auth-type jwt \
  --token-jwks-uri https://identity.example/.well-known/jwks.json \
  --token-issuer https://identity.example/ \
  --token-audience arbora
```

Network serving is a security boundary. Configure validated identity, tenant
isolation, TLS, and the deployment's authorization policy before exposing the
listener beyond loopback. Arbora refuses network serving without a built
authentication provider and rejects required authority that is absent or
invalid.

## Validate a candidate

Validate a signed candidate and emit a deterministic dependency plan with:

```bash
setup-config release-plan candidate.json --trusted-digest 'sha256:<digest>' --profile prod
```

The digest selects the canonical unsigned JSON document; it cannot establish
trust. AU's configured
`CERT_EVIDENCE_VERIFIER_COMMAND` independently authenticates the signed document
before Arbora consumes it. The signature envelope follows AU's public
`verify_signed_evidence` contract, including algorithm, key ID, signature and
subject digest. Missing or failed verification blocks planning.

Candidate schema version 1 contains `candidate_id`, timezone-aware `created_at`,
`artifacts`, `stages`, and one Arbora `profiles` binding. Each artifact declares
its component ID, repository, full source revision, immutable digest, image or
wheel kind, exact API/schema contract identifiers, and public GitHub build
receipt. Each stage declares whether it is optional/enabled, its probe contract,
and predecessors with matching API/schema identifiers. Unknown fields,
duplicate IDs, missing enabled predecessors, incompatible contracts and cycles
are refused. Disabled optional stages are excluded from the plan. Ordering uses
declared edges with component-ID tie-breaking.

The profile binding pins both the profile file's SHA256 bytes and its Arbora
artifact digest. The existing profile loader validates that same material; its
release must be digest-pinned to the selected artifact. The output includes
manifest/profile digests, ordered artifact revisions and public receipts. It
reports `executed: false` and `acceptance: not_audited`: this command does not
apply artifacts, evaluate live readiness, perform rollback, or qualify a
release. Those remaining requirements are tracked by the
[dependency-ordered release specification](https://github.com/Knuckles-Team/arbora/tree/main/specs/dependency-ordered-release).

Run diagnostics against the resolved deployment configuration, then run the
bounded release canary in the candidate environment:

```bash
arbora-doctor
arbora-release-canary
```

The canary reports aggregate readiness probes and does not print paths,
credentials, identities, or graph contents. A failed canary is a release
failure, not a warning to suppress.

## Production operations

`arbora-production-ops` provides guarded backup and restore-validation
commands. They require the deployment's workload identity, graph coordinator,
encryption, and mounted storage policy. Review the command help in the exact
candidate environment before use:

```bash
arbora-production-ops --help
```

Production operations emit opaque digests and aggregate counts rather than
endpoints, principals, storage paths, credentials, or graph contents. Backup
and restore validation are operational changes; run them through the normal
change-control and recovery procedure for the target deployment.

## Release model

The release workflow builds the wheel after the quality and scanner jobs pass.
PyPI publication runs only for an explicit version tag and uses the protected
`pypi-publish` environment. A push to `main` updates source and documentation;
it does not publish a package.

See [Capability status](status.md) before deployment. Capability-gated paths
remain unavailable whenever their required authority is absent or unverified.
