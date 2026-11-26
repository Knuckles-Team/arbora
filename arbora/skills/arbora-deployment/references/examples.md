# Deployment examples

Each example is a fenced block tagged `deployment-example`. The same blocks appear in
`README.md` and `docs/deployment.md`; a freshness test keeps the three surfaces
identical, parses every `argv`, and runs every `probe` against a fixture.

```deployment-example
profile: tiny
transport: stdio
argv: arbora --transport stdio
probe: mcp-initialize
```

```deployment-example
profile: tiny
transport: streamable-http
argv: arbora --transport streamable-http --host 127.0.0.1 --port 18765 --auth-type none
probe: mcp-initialize
```
