"""Deployment examples stay executable and synchronized (ARBORA-DEPLOY-R010.2, R010.3)."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from arbora.api.domain.deployment import genesis_environments as genesis
from arbora.deployment.host_config import UnknownServeFlag, parse_serve_argv

ROOT = Path(__file__).resolve().parents[2]
SURFACES = (
    ROOT / "README.md",
    ROOT / "docs" / "deployment.md",
    ROOT / "arbora" / "skills" / "arbora-deployment" / "references" / "examples.md",
)
REFERENCES = ROOT / "arbora" / "skills" / "arbora-deployment" / "references"
FIELDS = ("profile", "transport", "argv", "probe")
BLOCK = re.compile(r"```deployment-example\n(.*?)```", re.DOTALL)

PROFILE_PARTS = (
    "prerequisites",
    "install source",
    "package or pinned image",
    "editable development install",
    "authority setup",
    "start",
    "probes",
    "logs",
    "stop",
)


class ExampleError(ValueError):
    """A refusal named by its code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def extract(text: str) -> list[dict[str, str]]:
    """Every ``deployment-example`` block of a surface, fields checked."""
    out: list[dict[str, str]] = []
    for match in BLOCK.finditer(text):
        fields = yaml.safe_load(match.group(1))
        missing = [name for name in FIELDS if not (fields or {}).get(name)]
        if missing:
            raise ExampleError("EXAMPLE_FIELD_MISSING", ", ".join(missing))
        out.append({name: str(fields[name]) for name in FIELDS})
    return out


def example_digest(example: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(example, sort_keys=True).encode("utf-8")
    ).hexdigest()


def check_agreement(surfaces: dict[str, list[dict[str, str]]]) -> dict[str, str]:
    """The three surfaces must carry the same examples; return the digests."""
    names = list(surfaces)
    reference = surfaces[names[0]]
    for other in names[1:]:
        if surfaces[other] != reference:
            raise ExampleError("EXAMPLE_DIVERGES", f"{names[0]} and {other} differ")
    return {f"{e['profile']}/{e['transport']}": example_digest(e) for e in reference}


def parse_example(example: dict[str, str]) -> Any:
    try:
        parsed = parse_serve_argv(shlex.split(example["argv"]))
    except UnknownServeFlag as exc:
        raise ExampleError("EXAMPLE_FLAG_UNKNOWN", str(exc)) from exc
    if parsed.transport != example["transport"]:
        raise ExampleError("EXAMPLE_DIVERGES", "argv transport differs from the field")
    return parsed


# -- fixtures: one stdio and one streamable-http responder -------------------

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
}
_REPLY = {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "fixture"}}}
_STDIO_SERVER = (
    "import json,sys\n"
    "request=json.loads(sys.stdin.readline())\n"
    "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':request['id'],"
    "'result':{'serverInfo':{'name':'fixture'}}})+'\\n')\n"
)


def probe_stdio() -> dict[str, Any]:
    done = subprocess.run(  # noqa: S603  # nosec B603 - fixed interpreter and script
        [sys.executable, "-I", "-c", _STDIO_SERVER],
        input=json.dumps(_INITIALIZE) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return dict(json.loads(done.stdout))


def probe_http(port: int) -> dict[str, Any]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.dumps(_REPLY).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(  # noqa: S310
            f"http://127.0.0.1:{server.server_port}/mcp",
            data=json.dumps(_INITIALIZE).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310
            return dict(json.load(response))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def run_probe(example: dict[str, str]) -> None:
    parsed = parse_example(example)
    assert example["probe"] == "mcp-initialize"
    reply = probe_stdio() if parsed.transport == "stdio" else probe_http(0)
    assert reply["result"]["serverInfo"]["name"] == "fixture"


@pytest.mark.spec("ARBORA-DEPLOY-R010.2")
def test_examples_agree_parse_and_probe_on_both_transports() -> None:
    surfaces = {str(path.name): extract(path.read_text()) for path in SURFACES}
    digests = check_agreement(surfaces)
    examples = surfaces[SURFACES[0].name]
    assert {e["transport"] for e in examples} == {"stdio", "streamable-http"}
    assert len(digests) == len(examples)
    for example in examples:
        run_probe(example)


@pytest.mark.spec("ARBORA-DEPLOY-R010.2")
def test_obsolete_flag_and_missing_field_fail() -> None:
    good = {
        "profile": "tiny",
        "transport": "stdio",
        "argv": "arbora --transport stdio",
        "probe": "mcp-initialize",
    }
    with pytest.raises(ExampleError) as obsolete:
        parse_example({**good, "argv": "arbora --transport stdio --legacy-mode"})
    assert obsolete.value.code == "EXAMPLE_FLAG_UNKNOWN"
    block = "```deployment-example\nprofile: tiny\ntransport: stdio\n```\n"
    with pytest.raises(ExampleError) as missing:
        extract(block)
    assert missing.value.code == "EXAMPLE_FIELD_MISSING" and "argv" in str(
        missing.value
    )
    with pytest.raises(ExampleError) as diverges:
        check_agreement({"a": [good], "b": [{**good, "probe": "other"}]})
    assert diverges.value.code == "EXAMPLE_DIVERGES"


# -- supported-profile instruction sets (R010.3) -----------------------------


def check_profile_doc(doc: Path, profile_file: Path) -> None:
    """``PROFILE_DOC_INCOMPLETE`` names the missing part; the digest must match."""
    text = doc.read_text().lower()
    headings = {
        match.group(1).strip()
        for match in re.finditer(r"^#{1,6}\s+(.+)$", text, flags=re.MULTILINE)
    }
    missing = [part for part in PROFILE_PARTS if part not in headings]
    if missing:
        raise ExampleError("PROFILE_DOC_INCOMPLETE", ", ".join(missing))
    stamp = re.search(r"profile-sha256:\s*([0-9a-f]{64})", text)
    expected = hashlib.sha256(profile_file.read_bytes()).hexdigest()
    if stamp is None or stamp.group(1) != expected:
        raise ExampleError("PROFILE_DOC_INCOMPLETE", "profile-sha256 is not current")


@pytest.mark.spec("ARBORA-DEPLOY-R010.3")
def test_every_supported_profile_has_all_parts(tmp_path: Path) -> None:
    profiles = genesis.list_environment_profiles()
    builtin = {n: p for n, p in profiles.items() if p.is_relative_to(ROOT)}
    assert builtin
    for name, path in builtin.items():
        profile = genesis.load_environment_profile(name)
        refusal = genesis.target_cell_refusal(
            profile.target.orchestrator,
            profile.target.runtime,
            profile.target.privilege,
        )
        doc = REFERENCES / f"profile-{name}.md"
        if refusal is not None:
            # A target without an adapter is listed unsupported, never supported.
            assert "unsupported_target" in refusal
            continue
        check_profile_doc(doc, path)
        assert "Support status: supported" in doc.read_text()
    # An incomplete set names its missing part.
    broken = tmp_path / "profile-dev.md"
    broken.write_text(
        (REFERENCES / "profile-dev.md").read_text().replace("## Logs", "## Notes")
    )
    with pytest.raises(ExampleError) as raised:
        check_profile_doc(broken, builtin["dev"])
    assert raised.value.code == "PROFILE_DOC_INCOMPLETE" and "logs" in str(raised.value)
