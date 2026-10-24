"""Inspect a trusted local revision's canonical attended-route refusals."""

import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

SOURCE_PATH = "arbora/webui_host/session_boundary.py"
REQUESTS = (
    ("http", "/api/browser-control/attended-arm", "GET"),
    ("http", "/api/browser-control/attended-arm", "POST"),
    ("http", "/api/browser-control/attended-arm/finalize", "POST"),
    ("http", "/api/browser-control/attended-arm", "DELETE"),
    ("websocket", "/ws/browser-control", "GET"),
)


def read_revision(repository: Path, revision: str) -> tuple[str, str]:
    """Resolve a commit before selecting its exact module path."""
    commit = subprocess.check_output(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()
    source = subprocess.check_output(
        ["git", "-C", str(repository), "show", f"{commit}:{SOURCE_PATH}"],
        text=True,
        stderr=subprocess.PIPE,
    )
    return commit, source


async def unreachable(_scope: Any, _receive: Any, _send: Any) -> None:
    raise AssertionError("Protected request reached a downstream application")


def load_boundary(source: str) -> Any:
    """Execute trusted repository source; never replace its admission decision."""
    namespace: dict[str, Any] = {"__name__": "qualification_session_boundary"}
    exec(compile(source, SOURCE_PATH, "exec"), namespace)
    factory = namespace["CanonicalWebUIBoundaryMiddleware"]
    return factory(unreachable, operations=unreachable, auth_app=unreachable)


async def probe_request(
    boundary: Any, kind: str, path: str, method: str
) -> dict[str, Any]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(
            {
                key: value.decode() if isinstance(value, bytes) else value
                for key, value in message.items()
                if key != "headers"
            }
        )

    scope = {
        "type": kind,
        "path": path,
        "method": method,
        "scheme": "http",
        "headers": [],
        "query_string": b"",
        "root_path": "",
    }
    await boundary(scope, receive, send)
    expected = 4401 if kind == "websocket" else 503
    if not sent or sent[0].get("status", sent[0].get("code")) != expected:
        raise AssertionError(f"Refusal contract changed for {method} {path}: {sent}")
    return {"kind": kind, "path": path, "method": method, "response": sent}


async def qualify(repository: Path, revision: str) -> dict[str, Any]:
    commit, source = read_revision(repository, revision)
    boundary = load_boundary(source)
    return {
        "source_revision": commit,
        "source_path": SOURCE_PATH,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "claim": "Actual middleware refusal only; no browser or execution acceptance",
        "requests": [await probe_request(boundary, *request) for request in REQUESTS],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--revision",
        default="HEAD",
        help="Trusted local Git revision containing the canonical middleware",
    )
    parser.add_argument(
        "--output", type=Path, help="Evidence destination; stdout when omitted"
    )
    args = parser.parse_args()
    try:
        evidence = asyncio.run(qualify(args.repository, args.revision))
    except (subprocess.CalledProcessError, ImportError, KeyError) as error:
        parser.error(
            f"Required canonical source or installed dependency unavailable: {type(error).__name__}"
        )
    rendered = json.dumps(evidence, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
