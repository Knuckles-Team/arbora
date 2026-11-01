# ARBORA-INGRESS — Quickstart

Show a profile render with the public-ingress values:

```bash
uv run setup-config environments show <profile-name>
```

Run the focused tests of the written slice:

```bash
uv run pytest tests/deployment/test_public_ingress_identity.py
```

Run the served cutover proof against a disposable instance (a hot-path proof; it needs a browser and a loopback identity provider):

```bash
uv run pytest tests/qualification/identity/test_cutover_probe.py
```
