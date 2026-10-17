"""The served Graph OS surface imports no external-service client library."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

_FORBIDDEN_DRIVERS = (
    "aiokafka",
    "confluent_kafka",
    "psycopg",
    "neo4j",
    "falkordb",
    "pystardog",
)

_PROBE = (
    "import json, os, sys\n"
    "for key in ('GRAPH_DB_CONNECTION_PROFILE_REF', 'STATE_DB_URI',"
    " 'KAFKA_BOOTSTRAP_SERVERS'):\n"
    "    os.environ.pop(key, None)\n"
    "import graph_os.gateway.graph_api\n"
    "import graph_os.mcp_server.server\n"
    f"print(json.dumps([m for m in {list(_FORBIDDEN_DRIVERS)!r} if m in sys.modules]))\n"
)


@pytest.mark.spec("GRAPHOS-DEPLOY-R009.2.1")
def test_cold_import_of_the_served_surface_loads_no_external_driver() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, the running interpreter
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []
