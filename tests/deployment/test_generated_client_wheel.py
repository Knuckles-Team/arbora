"""Exercise the real generated client from an installed distribution."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.spec("GRAPHOS-OPS-R025.2")
def test_committed_artifacts_match_the_pinned_provider() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/gen_api.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.spec("GRAPHOS-OPS-R025.2")
def test_generated_client_installs_with_its_registry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(
        ROOT / "graph_os",
        source / "graph_os",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    wheels = tmp_path / "wheels"
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from setuptools.build_meta import build_wheel; "
            "import sys; build_wheel(sys.argv[1])",
            str(wheels),
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = wheels.glob("*.whl")
    installed = tmp_path / "installed"
    uv = shutil.which("uv")
    assert uv is not None, "wheel acceptance requires the repository's uv tool"
    subprocess.run(
        [
            uv,
            "--no-config",
            "pip",
            "install",
            "--no-deps",
            "--python",
            sys.executable,
            "--target",
            str(installed),
            str(wheel),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _CONSUMER, str(installed)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for mode in ("missing", "pointer", "traversal", "remote"):
        refused = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _NEGATIVE_CONSUMER,
                str(installed),
                mode,
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert refused.returncode == 0, refused.stdout + refused.stderr


_CONSUMER = """
import asyncio
import importlib.resources
import importlib.util
import json
import hashlib
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec('epistemic_graph') is None
import graph_os.client._generated_models as models
from graph_os.client.invoke import invoke

assert pathlib.Path(models.__file__).is_relative_to(sys.argv[1])
registry = json.loads((importlib.resources.files('graph_os.api.generated') / 'registry.json').read_bytes())
assert registry['registry_digest'] == models.REGISTRY_DIGEST
assert models.schema_for('query.uql')['params']
assert len(registry['ops']) > 0
canonical = dict(registry)
canonical.pop('registry_digest')
resolved_count = 0
for operation in registry['ops']:
    resolved = models.schema_for(operation['id'])
    for side in ('params', 'result'):
        definition = operation[side]
        if 'schema_snapshot' in definition:
            assert definition['schema_snapshot']['$ref'].startswith('schemas/')
            assert resolved[side]['schema_snapshot']
            resolved_count += 1
            definition.pop('schema_snapshot')
assert resolved_count > 0
payload = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
assert hashlib.sha256(payload).hexdigest() == models.REGISTRY_DIGEST

class Transport:
    def __init__(self):
        self.calls = []
    async def invoke(self, op, params):
        self.calls.append((op, params))
        return {'ok': True, 'result': {'rows': []}, 'meta': {'registry_digest': models.REGISTRY_DIGEST}}

async def check():
    transport = Transport()
    params = {'text': 'RETURN 1', 'params': {}}
    response = await invoke(transport, 'query.uql', params)
    assert response['result'] == {'rows': []}
    assert transport.calls == [('query.uql', params)]
    try:
        await invoke(transport, 'absent.operation', {})
    except KeyError:
        pass
    else:
        raise AssertionError('unknown operation reached transport')
    assert len(transport.calls) == 1

asyncio.run(check())
"""


_NEGATIVE_CONSUMER = """
import importlib.resources
import importlib.util
import json
import sys
sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec('epistemic_graph') is None
import graph_os.client._generated_models as models
root = importlib.resources.files('graph_os.api.generated')
path = root / 'registry.json'
original = path.read_bytes()
registry = json.loads(original)
op = next(op for op in registry['ops'] if 'schema_snapshot' in op['params'])
reference = op['params']['schema_snapshot']['$ref']
mode = sys.argv[2]
if mode == 'missing':
    reference = 'schemas/missing.json#/missing'
elif mode == 'pointer':
    reference = reference.partition('#')[0] + '#/missing'
elif mode == 'traversal':
    reference = 'schemas/../registry.json#/ops'
elif mode == 'remote':
    reference = 'https://example.invalid/schema.json#/missing'
op['params']['schema_snapshot'] = {'$ref': reference}
op['params']['eg_schema'] = 'contract/' + reference
try:
    path.write_text(json.dumps(registry))
    try:
        models.schema_for(op['id'])
    except (ValueError, FileNotFoundError):
        pass
    else:
        raise AssertionError('invalid resource was accepted: ' + mode)
finally:
    path.write_bytes(original)
"""
