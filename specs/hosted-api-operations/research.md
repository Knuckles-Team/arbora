# ARBORA-OPS — Research

Governing plan: [plan.md](plan.md). This file holds comparison notes for rows that extend the served surface. The rows themselves name no other project.

## Comparison with two public projects (rows R857.3 to R857.5, R923, R924, and ARBORA-RELEASE-R003.4.14 to R003.4.17)

TrustGraph is a graph and agent platform. Web-Algebra is an algebra of Linked Data operations that it exposes as tools over MCP and as an executable JSON program.

| Capability of the project | Spec row | State |
|---|---|---|
| MCP server with graph, SPARQL and embedding tools, bearer-token authentication | `ARBORA-OPS-R006` (one operation per wire-callable engine method), `ARBORA-FLEET-R044`, `ARBORA-IDENTITY-R008` | Covered |
| Every operation of the algebra exposed as an MCP tool | `ARBORA-OPS-R006`, `ARBORA-FLEET-R044` | Covered |
| stdio and streamable HTTP transports for the tool server | `SCION-CENTRALIZED-CAPABILITIES-R014` and the HTTP and MCP surfaces of `ARBORA-OPS` | Covered |
| A2A serving, streaming and task history | `ARBORA-A2A-R001`, `R010`, `R011` | Covered |
| REST gateway, OpenAPI document, WebSocket | `ARBORA-OPS` (served operations and OpenAPI), `ARBORA-IDENTITY-R004.4` (WebSocket close codes) | Covered |
| Users, API keys, workspaces, JWT, a no-auth mode | `ARBORA-IDENTITY-R003`, `R005`, `R008`, `R014`, `R017` | Covered |
| Model listing | `ARBORA-OPS-R878` | Covered |
| Token usage and cost reads | `ARBORA-OPS-R024.3` | Covered |
| Prompt and tool configuration as served operations | `ARBORA-OPS-R912`, `R922.4`, `MEDULLA-ORCH-VOCAB-R800` | Covered |
| Tool groups that limit which tools an agent sees | `ARBORA-FLEET-R059` (pins), `find` intent discovery, tool-subset admission in `ARBORA-A2A` | Covered by design: discovery is a ranked read, not a static group |
| Config push to running services | `ARBORA-FLEET-R059`, policy safe reload in `ARBORA-FLEET` | Covered |
| Graph Store protocol in many RDF serializations | `ARBORA-OPS-R857.3` to `R857.5` | New |
| Portable bundles for knowledge and workspaces | `ARBORA-OPS-R923` | New |
| Guided graph traversal with seeds and hop restrictions | `ARBORA-OPS-R924` | New |
| Compile a workflow into one JSON program that runs atomically | `ARBORA-RELEASE-R003.4.1` to `R003.4.13` (`agents.plan_run`) | Covered |
| Iterate a step over a computed list, with one write per target | `ARBORA-RELEASE-R003.4.14` to `R003.4.17` | New |
| Operations as `{"@op": ..., "args": ...}` forms with nested calls | `ARBORA-RELEASE-R003.4.6` (`$ref` bindings between tool steps) | Covered in a different shape: a validated DAG, not nested forms |
| No transactions: effects already done stay done | `ARBORA-RELEASE-R003.4.15` (earlier calls stay done) | Covered |

Decisions and rejected options.

- Rejected: nested operation forms. A nested expression tree hides the call order from the engine that validates the plan. A flat DAG with `$ref` bindings lets the engine check bindings, bounds and cycles before any call.
- Rejected: concurrent iteration. The project leaves the order of iterations open. Arbora runs items in order so the effect journal and replay stay exact.
- Not adopted: a bounded loop with carried state. The engine cannot prove a bound for it before the run.
