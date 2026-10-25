# How Arbora fits

Arbora is the platform edge and composition root. It is the running process
that receives requests, establishes authority, and projects independently owned
capabilities through one governed surface.

[![Knuckles platform runtime architecture](https://raw.githubusercontent.com/Knuckles-Team/epistemic-graph/main/docs/assets/runtime-architecture.svg)](https://knuckles-team.github.io/.github/architecture/#arbora)

[Explore the live architecture](https://knuckles-team.github.io/.github/architecture/#arbora)

## One request, five clear owners

Product display names map to existing repositories: Overstory (`agent-webui`),
Tuig (`agent-terminal-ui`), Scion SDK (`agent-connector-sdk`), Meristem
(`agent-utilities`) and Axil (`geniusbot`). Package/import names and
repository URLs are unchanged. The Tuig executable remains `tuig`.

| Layer | Owns | Arbora relationship |
|---|---|---|
| [Overstory](https://knuckles-team.github.io/agent-webui/) | Browser interaction, chat presentation, and local UI state | Hosted by Arbora with an injected application composer. |
| Tuig | Terminal and headless operation | Uses Arbora REST today; conversational ACP chat remains an explicit contract gap. |
| Axil | Desktop GUI for chat, graph, fleet, and health | Uses the governed Arbora gateway for its primary panels. |
| Messaging channels | Chat and voice entrypoints | Hosted and supervised by Arbora; adapters and the inbound router remain owned by agent-utilities. |
| **Arbora** | MCP, REST, A2A, request policy, fleet supervision, and process lifecycle | Authenticates, composes, routes, supervises, and projects. |
| [Meristem](https://knuckles-team.github.io/agent-utilities/) | In-run execution harness | Applies run plans supplied by Arbora through typed harness contracts. |
| [epistemic-graph](https://knuckles-team.github.io/epistemic-graph/) | Durable multimodal state, RDF/OWL/SHACL reasoning, queries, and provenance | Provides generated storage and reasoning contracts. |
| [Scion SDK](https://knuckles-team.github.io/agent-connector-sdk/) | Connector servers, source synchronization, and governed source effects | Defines the services admitted into the Arbora fleet. |

## Runtime flow

<ol class="site-flow" aria-label="Arbora request flow">
  <li class="site-flow__step"><span class="site-flow__title">Receive</span><span class="site-flow__body">Overstory, Tuig, Axil, messaging, MCP, REST, and A2A converge on one Arbora process.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Authorize</span><span class="site-flow__body">Arbora verifies identity, tenant, scope, and action policy.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Delegate</span><span class="site-flow__body">Agent work reaches agent-utilities. Source work reaches an admitted connector.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Commit</span><span class="site-flow__body">Governed reads and mutations reach epistemic-graph with evidence and provenance.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Project</span><span class="site-flow__body">Arbora returns the same result through the caller's selected transport.</span></li>
</ol>

## Composition invariants

- MCP and REST handlers meet at the same application boundary.
- The serving lifecycle owns one FastMCP loop and one fleet multiplexer.
- Durable state and semantic reasoning are performed by epistemic-graph.
- Meristem applies supplied decisions inside each run; it does not own policy or planning.
- Connector credentials and vendor effects stay inside connector services.
- Browser presentation and local interaction state stay inside Overstory.
- Missing authority produces an explicit unavailable result; Arbora does not
  manufacture state or success receipts.

Use the [capability status](status.md) page for the exact surface exposed by the
current package.
