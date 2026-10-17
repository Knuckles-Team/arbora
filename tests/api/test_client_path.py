"""``graph_os.client`` is the public import path frontends use."""

from __future__ import annotations

import graph_os.client as client
from graph_os.client._generated_models import REGISTRY_DIGEST, schema_for
from graph_os.client.invoke import invoke


def test_generated_client_is_importable() -> None:
    assert REGISTRY_DIGEST
    assert callable(invoke)
    assert callable(schema_for)


def test_gateway_client_is_re_exported() -> None:
    from graph_os.gateway.client_sdk import GatewayClient

    assert client.GatewayClient is GatewayClient
