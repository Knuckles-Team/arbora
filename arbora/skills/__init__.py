"""Package-owned skills exposed through the skill-provider entry point.

``arbora-genesis`` provisions the Day-0 substrate (Docker Compose on one host,
Docker Swarm, or Kubernetes through the chart in ``deploy/helm/arbora``) and
hands off to ``arbora-deployment``, which installs, configures and verifies
the Arbora runtime. ``arbora-development`` documents how to develop a
feature in this or a sibling ecosystem repository. Discovered through arbora's
``scion_sdk.connector`` declaration (``skills/`` by convention).
"""

from __future__ import annotations

ARBORA_SKILLS: tuple[str, ...] = ("arbora-deployment", "arbora-genesis")

__all__ = ["ARBORA_SKILLS"]
