# REST gateway

`arbora.gateway` owns HTTP routing, dashboard aggregation, the widget
registry, and the in-process host lifecycle. Agent, graph, ontology,
catalog, and policy behavior remains owned by meristem or
medulla.

The application boundary is `GatewayApplicationPort`. The composition root
installs one implementation with `configure_gateway_application()` before it
calls `register_graph_routes()`.

<ol class="site-flow" aria-label="REST gateway composition">
  <li class="site-flow__step"><span class="site-flow__title">Receive HTTP</span><span class="site-flow__body">The Arbora gateway receives an authenticated application request.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Cross one port</span><span class="site-flow__body">GatewayApplicationPort connects routes to the shared application runtime.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Use one composition</span><span class="site-flow__body">MCP and fleet services share the same Arbora process state.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Reach the owner</span><span class="site-flow__body">Typed meristem and medulla contracts perform domain work.</span></li>
</ol>

There is no fallback import of AU's `kg_server` or multiplexer. An unconfigured
process fails closed before routes are served, so the active authority remains
observable rather than silently selecting another implementation.

## Connector widgets

The dashboard holds no widget code and no widget registry. Each configured
service names a connector of the fleet census as its widget, and the dashboard
reads it through the served operation `ecosystem.widgets.read` under the
caller's own identity. The operation projects the connector's read-only tool
that needs no argument, from the catalog the connector's pack imported, and
calls it through `fleet.call`. Credentials, TLS, child lifecycle and tool
admission therefore stay at the connector and fleet boundary.

A connector that is down gives a `degraded` widget with the last rows read for
that tenant. A connector absent from the census gives an error widget. Arbora
never falls back to package importability or direct vendor credentials.
