# Start Arbora

Arbora is the process the operator runs when clients need the Knuckles platform over MCP,
REST, A2A, or Agent WebUI. The `tiny` profile provides the smallest complete
local composition and keeps its generated configuration in the standard XDG
location.

## Install the bundled runtime

Arbora supports Python 3.12 through 3.14. Install the runtime and the optional
WebUI host with `uv`:

```bash
uv tool install "arbora[webui]"
```

## Create the local profile

```bash
setup-config generate --profile tiny
setup-config doctor --profile tiny
```

The generated profile uses secret references instead of plaintext credentials.
The doctor reports any authority that is unavailable before Arbora accepts
traffic.

## Register the local door

```bash
setup-config codex
```

Arbora now appears as the `arbora` MCP server in Codex. It runs as a local
child process and keeps protocol output isolated on `stdio`.

For another MCP client, configure the same launcher directly:

```bash
arbora --transport stdio
```

When Arbora hosts HTTP routes or Agent WebUI, those surfaces use the same
composed services rather than starting another Arbora instance.

Continue with [Deployment and configuration](deployment.md) for authenticated
network serving, production profiles, diagnostics, and release canaries.
