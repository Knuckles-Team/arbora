# Start Arbora

Arbora is the process the operator runs when clients need the Knuckles platform over MCP,
REST, A2A, or Overstory. The `tiny` profile provides the smallest complete
local composition and keeps its generated configuration in the standard XDG
location.

## Install the bundled runtime

Arbora supports Python 3.12 through 3.14. Install the runtime and the optional
WebUI host with `uv`:

```bash
uv tool install "arbora[overstory]"
```

## Create the local profile

```bash
setup-config generate --profile tiny
setup-config doctor --profile tiny
```

The generated profile uses secret references instead of plaintext credentials.
The doctor reports any authority that is unavailable before Arbora accepts
traffic.

## Connect a client

Clients that launch Arbora as a child process configure this launcher:

```bash
arbora --transport stdio
```

When Arbora hosts HTTP routes or Overstory, those surfaces use the same
composed services rather than starting another Arbora instance.

Continue with [Deployment and configuration](deployment.md) for authenticated
network serving, production profiles, diagnostics, and release canaries.
