"""Generated GraphOS client surface.

``graph_os.client`` is the public import path for frontends: the digest-bound
operation client (``invoke``, ``_generated_models``) and, lazily, the surface
gateway ``GatewayClient`` from ``graph_os.gateway.client_sdk``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["DEFAULT_GATEWAY_URL", "GatewayClient"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from graph_os.gateway import client_sdk

        return getattr(client_sdk, name)
    raise AttributeError(name)
