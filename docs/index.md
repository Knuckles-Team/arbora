<section class="site-hero" aria-labelledby="arbora-title">
  <p class="site-hero__eyebrow">The governed runtime door</p>
  <h1 class="site-hero__title" id="arbora-title">Run the whole agent platform through one clear boundary.</h1>
  <p class="site-hero__summary">
    Arbora is the process the operator runs. It authenticates MCP, REST, A2A, browser,
    terminal, desktop, and messaging requests; composes the agent and graph
    services behind them; and supervises the connector fleet from one serving
    lifecycle.
  </p>
  <div class="site-hero__actions">
    <a class="md-button md-button--primary" href="get-started/">Start Arbora</a>
    <a class="md-button" href="architecture/">See how it fits</a>
  </div>
</section>

<figure class="site-architecture" markdown>
  [![Knuckles platform runtime architecture](https://raw.githubusercontent.com/Knuckles-Team/medulla/main/docs/assets/runtime-architecture.svg)](https://knuckles-team.github.io/.github/architecture/#arbora)

  [Explore the live architecture](https://knuckles-team.github.io/.github/architecture/#arbora)
  <figcaption>One request path, five deliberately separate authorities.</figcaption>
</figure>

<div class="site-card-grid" markdown>
  <article class="site-card" markdown>
    <p class="site-card__title">Try it</p>
    <p class="site-card__body">
      Install the bundled runtime, generate a local profile, and validate it.
    </p>
    <a href="get-started/">Open the quick start →</a>
  </article>
  <article class="site-card" markdown>
    <p class="site-card__title">Understand it</p>
    <p class="site-card__body">
      Follow a request from client to Arbora, agent control plane, durable
      graph, and connector service without blurring ownership.
    </p>
    <a href="architecture/">Explore the architecture →</a>
  </article>
  <article class="site-card" markdown>
    <p class="site-card__title">Operate it</p>
    <p class="site-card__body">
      Configure local or network transports, validate authority, inspect
      readiness, and run the release canary.
    </p>
    <a href="deployment/">Use the deployment guide →</a>
  </article>
</div>

## What Arbora owns

<div class="site-ownership">
  <div class="site-ownership__grid">
    <div class="site-ownership__item">
      <span class="site-ownership__label">Arbora owns</span>
      <span class="site-ownership__value">Process lifecycle, MCP and REST composition, unary A2A, request policy, fleet supervision, and optional WebUI hosting.</span>
    </div>
    <div class="site-ownership__item">
      <span class="site-ownership__label">Arbora delegates</span>
      <span class="site-ownership__value">In-run execution to Meristem, durable state and reasoning to Medulla, source effects to connector services, and presentation to Overstory.</span>
    </div>
  </div>
</div>

Arbora does not copy those authorities. It binds their public contracts into
one identity-aware application surface and reports unavailable authority
explicitly.

## Follow one request

<ol class="site-flow">
  <li class="site-flow__step">
    <span class="site-flow__title">Enter through one door</span>
    <span class="site-flow__body">An MCP, REST, A2A, or WebUI request reaches the Arbora serving process.</span>
  </li>
  <li class="site-flow__step">
    <span class="site-flow__title">Establish authority</span>
    <span class="site-flow__body">Arbora verifies identity, tenant, requested action, and the applicable policy.</span>
  </li>
  <li class="site-flow__step">
    <span class="site-flow__title">Route to the owner</span>
    <span class="site-flow__body">Agent work reaches agent-utilities; graph work reaches Medulla; source work reaches an admitted connector.</span>
  </li>
  <li class="site-flow__step">
    <span class="site-flow__title">Return evidence</span>
    <span class="site-flow__body">The caller receives a transport-specific response backed by the same governed result and provenance.</span>
  </li>
</ol>

## Choose the right room

| If the operator must… | Go to… |
|---|---|
| Run MCP, REST, A2A, WebUI, or fleet supervision | **Arbora** — the operator are here |
| Build agents, workflows, evaluations, or skills | [Meristem](https://knuckles-team.github.io/meristem/) |
| Store, query, reason over, or prove durable knowledge | [Medulla](https://knuckles-team.github.io/medulla/) |
| Build and certify a source connector | [Scion SDK](https://knuckles-team.github.io/scion-sdk/) |
| Use the platform in a browser | [Overstory](https://knuckles-team.github.io/overstory/) |

The [capability status](status.md) is the exact account of the surfaces shipped
by this package.

## Specification delivery

<a href="spec-delivery/">Open the specification delivery dashboard</a> for
current specification status, delivery timelines, velocity and burndown, and
open pull request and issue snapshots. It refreshes with each main-branch push.
Unknown completion dates and unavailable history are labeled explicitly;
individual requirements and pull requests do not count as completed specifications.

## Planned run observation

[ARBORA-OPS-R021.4](https://github.com/Knuckles-Team/arbora/tree/main/specs/hosted-api-operations) defines planned run reads and served acceptance.
The specification does not establish runtime availability.
